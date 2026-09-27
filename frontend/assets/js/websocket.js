/**
 * NEXORA — websocket.js
 * Django Channels client: authenticated realtime transport with a strict
 * connection state machine, bounded exponential backoff + jitter, heartbeat,
 * lifecycle awareness and a single, non-looping authentication recovery path.
 *
 * It only transports events. Reconciliation with REST state is the caller's
 * responsibility (see messages.js / chat.js) because the backend is
 * authoritative and events may arrive duplicated or out of order.
 *
 * State machine
 * -------------
 *   idle ──start()──► connecting ──open──► open
 *                        │                  │
 *                        │ close/error      │ close
 *                        ▼                  ▼
 *                   reconnecting ◄──────────┘   (bounded backoff + jitter)
 *                        │
 *                        ├─ offline            (navigator offline; no retries)
 *                        └─ closed             (auth failure, logout, stop())
 *
 * Invariants deliberately enforced here:
 *   - at most ONE socket and ONE reconnect timer exist at any moment;
 *   - lifecycle listeners are bound exactly once for the life of the page;
 *   - a socket rejected for authentication triggers exactly ONE session
 *     refresh; if that fails the client stops for good and reports
 *     'unauthorized' (no infinite "Reconnecting…");
 *   - stop() is terminal until start()/restart() is called again.
 */

import { apiConfig, refreshSession, tokenStore } from './api.js';
import { config as runtimeConfig } from './config.js';
import { Emitter, backoffDelay } from './utils.js';

/** @typedef {'idle'|'connecting'|'open'|'reconnecting'|'offline'|'closed'} ConnectionState */

const HEARTBEAT_INTERVAL = 25000;
// A pong may legitimately be delayed by a slow radio or a busy server; 15s
// still recycles a zombie connection within one heartbeat cycle + margin.
const HEARTBEAT_TIMEOUT = 15000;
// A handshake that never completes (cold server, black-holed SYN) must not
// hang the client in "connecting" forever: recycle it into the normal
// bounded backoff instead. (Runtime-overridable for tests/deployment tuning.)
const CONNECT_TIMEOUT = Number(runtimeConfig.CONNECT_TIMEOUT) || 15000;
const MAX_RECONNECT_DELAY = 30000;
const MAX_QUEUE = 40;

/**
 * Close codes that mean "your credential is not valid", i.e. retrying the
 * socket unchanged can never succeed.
 *   4401 — NEXORA: authenticated handshake refused / access token expired
 *   4403 — NEXORA: origin or object authorization refused
 *   1008 — RFC 6455 policy violation
 *   4003 — legacy NEXORA rejection code (older backends)
 */
const AUTH_CLOSE_CODES = new Set([4401, 4403, 4003, 1008]);

/** Transport failures that are worth retrying, capped so we never hammer. */
const MAX_RECONNECT_ATTEMPTS = 10;

export const socketEvents = new Emitter();

function debug(...args) {
  // Never logs cookies, tokens, PINs or message bodies — transport facts only.
  if (runtimeConfig.DEBUG) console.debug('[nexora:ws]', ...args);
}

class RealtimeClient {
  #socket = null;
  #state = 'idle';
  #attempt = 0;
  #reconnectTimer = null;
  #connectTimer = null;
  #heartbeatTimer = null;
  #pongTimer = null;
  #queue = [];
  #path = '/ws/app/';
  #manualClose = false;
  #started = false;
  #lifecycleBound = false;
  #lastEventAt = 0;
  /** Monotonic connection id so late handlers can be ignored. */
  #epoch = 0;
  /** True once this socket generation has been open at least once. */
  #everOpened = false;
  /** Guards the "refresh the session once, then retry" recovery. */
  #recovering = false;
  #refreshUsed = false;

  get state() { return this.#state; }
  get isOpen() { return this.#socket?.readyState === WebSocket.OPEN; }
  get epoch() { return this.#epoch; }
  get lastEventAt() { return this.#lastEventAt; }

  /** @param {string} path e.g. '/ws/app/' */
  start(path) {
    if (path) this.#path = path;
    // start() is also the "sign-in again" entry point, so it must lift a
    // previous stop(); otherwise the socket would stay dead for the session.
    this.#manualClose = false;
    if (!this.#lifecycleBound) {
      this.#bindLifecycle();
      this.#lifecycleBound = true;
    }
    if (this.#started) {
      if (!this.isOpen) this.#connect();
      return;
    }
    this.#started = true;
    this.#attempt = 0;
    this.#refreshUsed = false;
    this.#connect();
  }

  /** Terminal stop: logout, session expiry, page teardown. */
  stop(reason = 'client-stop') {
    this.#manualClose = true;
    this.#clearTimers();
    this.#queue.length = 0;
    this.#epoch += 1; // orphan any in-flight handlers
    if (this.#socket) {
      try { this.#socket.close(1000, reason); } catch { /* already closing */ }
    }
    this.#socket = null;
    this.#everOpened = false;
    debug('stopped', reason);
    this.#setState('closed', { reason });
  }

  /** Force a fresh connection (e.g. after re-authentication). */
  restart() {
    this.#manualClose = false;
    this.#attempt = 0;
    this.#refreshUsed = false;
    this.#clearTimers();
    this.#epoch += 1;
    if (this.#socket) {
      try { this.#socket.close(4001, 'client-restart'); } catch { /* ignore */ }
    }
    this.#socket = null;
    this.#connect();
  }

  /**
   * Send an event. Queued (bounded) while reconnecting so typing/read
   * signals are not lost during brief interruptions.
   * @returns {boolean} true when written to the socket immediately
   */
  send(type, payload = {}, { queue = true } = {}) {
    const frame = JSON.stringify({ type, ...payload });
    if (this.isOpen) {
      try {
        this.#socket.send(frame);
        return true;
      } catch {
        /* fall through to queueing */
      }
    }
    if (queue && !this.#manualClose) {
      this.#queue.push(frame);
      while (this.#queue.length > MAX_QUEUE) this.#queue.shift();
    }
    return false;
  }

  /* ---------------- internals ---------------- */

  #url() {
    // Always derived from the centralized API origin (config.js), so the
    // scheme can never be mismatched and there is no second source of truth.
    const base = apiConfig.wsOrigin;
    const url = new URL(this.#path, base);
    // Bearer deployments cannot set Authorization headers on WebSockets;
    // the backend contract accepts a short-lived token query parameter.
    if (tokenStore.isBearerMode && tokenStore.get()) {
      url.searchParams.set('token', tokenStore.get());
    }
    return url.toString();
  }

  #setState(next, detail = null) {
    if (this.#state === next) return;
    this.#state = next;
    debug('state', next, detail || '');
    socketEvents.emit('state', next, detail);
  }

  #connect() {
    if (this.#manualClose) return;
    if (this.#socket && (this.#socket.readyState === WebSocket.OPEN || this.#socket.readyState === WebSocket.CONNECTING)) return;

    if (navigator.onLine === false) {
      this.#setState('offline');
      return;
    }

    this.#clearTimers();
    this.#setState(this.#attempt > 0 ? 'reconnecting' : 'connecting', { attempt: this.#attempt });

    const epoch = ++this.#epoch;
    this.#everOpened = false;
    const startedAt = Date.now();

    let socket;
    let url;
    try {
      url = this.#url();
      socket = new WebSocket(url);
    } catch (error) {
      debug('construction failed', error?.message || error);
      this.#scheduleReconnect();
      return;
    }
    debug('connecting', url);
    this.#socket = socket;

    // Safety net: a handshake stuck for CONNECT_TIMEOUT (cold backend,
    // silently dropped upgrade) is recycled into the normal backoff path
    // instead of leaving the client in "connecting" forever.
    this.#connectTimer = setTimeout(() => {
      if (epoch !== this.#epoch || !this.#socket) return;
      if (this.#socket.readyState !== WebSocket.CONNECTING) return;
      debug('handshake timeout after', CONNECT_TIMEOUT);
      try { this.#socket.close(4000, 'connect-timeout'); } catch { /* ignore */ }
      this.#socket = null;
      this.#epoch += 1; // orphan this generation's handlers
      this.#scheduleReconnect();
    }, CONNECT_TIMEOUT);

    socket.addEventListener('open', () => {
      if (epoch !== this.#epoch) return;
      clearTimeout(this.#connectTimer);
      this.#connectTimer = null;
      this.#attempt = 0;
      this.#everOpened = true;
      this.#recovering = false;
      // A connection that actually opened proves the credential works, so the
      // one-shot refresh budget is restored for the next outage.
      this.#refreshUsed = false;
      debug('open in', `${Date.now() - startedAt}ms`);
      this.#setState('open');
      this.#flushQueue();
      this.#startHeartbeat();
      socketEvents.emit('open', { epoch, durationMs: Date.now() - startedAt });
    });

    socket.addEventListener('message', (event) => {
      if (epoch !== this.#epoch) return;
      this.#lastEventAt = Date.now();
      this.#clearPongTimer();

      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch {
        return; // ignore malformed frames
      }
      if (!frame || typeof frame !== 'object') return;

      const type = frame.type || frame.event;
      if (!type) return;

      if (type === 'pong' || type === 'heartbeat.ack') return;
      if (type === 'ping') {
        this.send('pong', {}, { queue: false });
        return;
      }
      if (type === 'auth.error' || type === 'connection.unauthorized') {
        // The server told us exactly why. Try one session refresh; if that
        // fails the caller signs the user out. Never loop.
        debug('auth.error', frame.code || '');
        this.#handleAuthFailure(frame.code || 'AUTH_ERROR');
        return;
      }

      socketEvents.emit('event', { type, payload: frame.payload ?? frame.data ?? frame, raw: frame, epoch });
      socketEvents.emit(type, frame.payload ?? frame.data ?? frame, frame);
    });

    socket.addEventListener('error', () => {
      if (epoch !== this.#epoch) return;
      // The browser never explains WebSocket errors; the close event that
      // follows carries the only usable information.
      socketEvents.emit('error');
    });

    socket.addEventListener('close', (event) => {
      if (epoch !== this.#epoch) return;
      clearTimeout(this.#connectTimer);
      this.#connectTimer = null;
      this.#clearTimers();
      this.#socket = null;
      const openedThisTime = this.#everOpened;
      debug('close', { code: event.code, reason: event.reason || '', opened: openedThisTime, livedMs: Date.now() - startedAt });
      socketEvents.emit('close', { code: event.code, reason: event.reason });

      if (this.#manualClose) {
        this.#setState('closed');
        return;
      }

      if (AUTH_CLOSE_CODES.has(event.code)) {
        this.#handleAuthFailure(`CLOSE_${event.code}`);
        return;
      }

      // A handshake refused by the server reaches the browser as 1006 with no
      // reason: it is indistinguishable from a dropped network. If we never
      // opened, spend the single refresh attempt to find out which it was
      // instead of reconnecting forever against a dead session.
      if (!openedThisTime && !this.#refreshUsed) {
        this.#probeSession();
        return;
      }

      this.#scheduleReconnect();
    });
  }

  /**
   * Authentication was refused. Refresh the session exactly once; restart on
   * success, stop for good on failure.
   */
  async #handleAuthFailure(code) {
    if (this.#recovering) return;
    this.#recovering = true;

    this.#clearTimers();
    if (this.#socket) {
      try { this.#socket.close(1000, 'auth-recovery'); } catch { /* ignore */ }
      this.#socket = null;
    }

    if (this.#refreshUsed) {
      this.#giveUp(code);
      return;
    }
    this.#refreshUsed = true;
    this.#setState('connecting', { reason: 'session-refresh' });

    let result = { ok: false, reason: 'unreachable' };
    try {
      result = await refreshSession();
    } catch {
      result = { ok: false, reason: 'unreachable' };
    }
    this.#recovering = false;
    if (this.#manualClose) return;

    if (result.ok) {
      debug('session refreshed — restarting socket');
      this.#attempt = 0;
      this.#connect();
      return;
    }
    if (result.reason === 'unreachable') {
      // The backend could not be reached at all, so this is infrastructure,
      // not authentication: keep the session and retry with backoff.
      debug('refresh unreachable — treating as transport failure');
      this.#scheduleReconnect();
      return;
    }
    this.#giveUp(code);
  }

  /**
   * Closed before opening, for an unknown reason. One refresh call tells us
   * whether the session is still valid (retry) or gone (stop).
   */
  async #probeSession() {
    this.#refreshUsed = true;
    let result = { ok: false, reason: 'unreachable' };
    try {
      result = await refreshSession();
    } catch {
      result = { ok: false, reason: 'unreachable' };
    }
    if (this.#manualClose) return;

    if (result.ok || result.reason === 'unreachable') {
      // Session intact (or the backend is simply unreachable): this was a
      // transport failure, so retry with bounded backoff.
      debug('handshake failed but session is not rejected — retrying', result.reason);
      this.#scheduleReconnect();
      return;
    }
    debug('session rejected after handshake failure — stopping');
    this.#giveUp('SESSION_EXPIRED');
  }

  /** Stop permanently and tell the application the session is gone. */
  #giveUp(code) {
    this.#manualClose = true;
    this.#recovering = false;
    this.#clearTimers();
    this.#queue.length = 0;
    this.#setState('closed', { code });
    socketEvents.emit('unauthorized', { code });
  }

  #scheduleReconnect() {
    if (this.#manualClose) return;
    clearTimeout(this.#reconnectTimer);
    this.#reconnectTimer = null;

    if (navigator.onLine === false) {
      this.#setState('offline');
      return; // the 'online' listener resumes us — no aggressive looping
    }

    if (this.#attempt >= MAX_RECONNECT_ATTEMPTS) {
      // Bounded: stop spinning and let the user retry explicitly. The banner
      // renders a Reconnect button for this state.
      debug('reconnect attempts exhausted');
      this.#setState('closed', { code: 'RETRY_LIMIT' });
      return;
    }

    this.#attempt += 1;
    const delay = backoffDelay(this.#attempt, { base: 1000, max: MAX_RECONNECT_DELAY });
    this.#setState('reconnecting', { attempt: this.#attempt, delay });
    this.#reconnectTimer = setTimeout(() => {
      this.#reconnectTimer = null;
      this.#connect();
    }, delay);
  }

  #flushQueue() {
    while (this.#queue.length && this.isOpen) {
      const frame = this.#queue.shift();
      try {
        this.#socket.send(frame);
      } catch {
        this.#queue.unshift(frame);
        break;
      }
    }
  }

  #startHeartbeat() {
    this.#clearHeartbeat();
    this.#heartbeatTimer = setInterval(() => {
      if (!this.isOpen) return;
      this.send('ping', { t: Date.now() }, { queue: false });
      this.#clearPongTimer();
      // If the server never answers, the connection is a zombie — recycle it.
      this.#pongTimer = setTimeout(() => {
        if (this.isOpen) {
          try { this.#socket.close(4000, 'heartbeat-timeout'); } catch { /* ignore */ }
        }
      }, HEARTBEAT_TIMEOUT);
    }, HEARTBEAT_INTERVAL);
  }

  #clearHeartbeat() {
    clearInterval(this.#heartbeatTimer);
    this.#heartbeatTimer = null;
    this.#clearPongTimer();
  }

  #clearPongTimer() {
    clearTimeout(this.#pongTimer);
    this.#pongTimer = null;
  }

  #clearTimers() {
    clearTimeout(this.#reconnectTimer);
    this.#reconnectTimer = null;
    clearTimeout(this.#connectTimer);
    this.#connectTimer = null;
    this.#clearHeartbeat();
  }

  #bindLifecycle() {
    window.addEventListener('online', () => {
      if (this.#manualClose || !this.#started) return;
      // A genuine connectivity transition starts a fresh cycle (the offline
      // handler closed the socket and froze the timers). This is the only
      // place besides a successful open that resets the backoff.
      this.#attempt = 0;
      if (!this.isOpen) this.#connect(); // exactly one controlled attempt
    });

    window.addEventListener('offline', () => {
      this.#clearTimers();
      if (this.#socket) {
        try { this.#socket.close(4002, 'offline'); } catch { /* ignore */ }
      }
      this.#socket = null;
      this.#setState('offline');
    });

    // PWA backgrounding / tab suspension: reconnect promptly on return and
    // let listeners resynchronise state from REST. The backoff position is
    // deliberately NOT reset here — repeatedly backgrounding/foregrounding a
    // tab against a struggling server must not restart the retry storm.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') return;
      if (this.#manualClose || !this.#started) return;
      if (!this.isOpen) {
        this.#connect();
      } else {
        socketEvents.emit('resume');
      }
    });

    window.addEventListener('pageshow', (event) => {
      if (!event.persisted) return;
      if (this.#manualClose || !this.#started) return;
      if (!this.isOpen) {
        this.#connect();
      }
    });

    window.addEventListener('pagehide', () => {
      this.#clearTimers();
    });
  }
}

export const realtime = new RealtimeClient();

/* ============================================================
   Convenience senders — thin wrappers over the event contract
   ============================================================ */

export const rt = {
  joinConversation(conversationId) {
    realtime.send('conversation.join', { conversation_id: conversationId });
  },
  leaveConversation(conversationId) {
    realtime.send('conversation.leave', { conversation_id: conversationId }, { queue: false });
  },
  typing(conversationId, isTyping) {
    realtime.send('typing', { conversation_id: conversationId, typing: !!isTyping }, { queue: false });
  },
  markRead(conversationId, lastMessageId) {
    realtime.send('message.read', { conversation_id: conversationId, message_id: lastMessageId });
  },
  markDelivered(messageIds) {
    if (!messageIds?.length) return;
    realtime.send('message.delivered', { message_ids: messageIds });
  },
  presencePing() {
    realtime.send('presence.ping', {}, { queue: false });
  },
};

/** Human-readable connection label for the status banner. */
export function connectionLabel(state) {
  switch (state) {
    case 'open': return 'Connected';
    case 'connecting': return 'Connecting…';
    case 'reconnecting': return 'Reconnecting…';
    case 'offline': return "You're offline";
    case 'closed': return 'Disconnected';
    default: return '';
  }
}

export default realtime;
