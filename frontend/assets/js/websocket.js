/**
 * NEXORA — websocket.js
 * Django Channels client: authenticated realtime transport with reconnection,
 * exponential backoff + jitter, heartbeat, and lifecycle awareness.
 *
 * It only transports events. Reconciliation with REST state is the caller's
 * responsibility (see messages.js / chat.js) because the backend is
 * authoritative and events may arrive duplicated or out of order.
 */

import { apiConfig, tokenStore } from './api.js';
import { Emitter, backoffDelay } from './utils.js';

/** @typedef {'idle'|'connecting'|'open'|'reconnecting'|'offline'|'closed'} ConnectionState */

const HEARTBEAT_INTERVAL = 25000;
const HEARTBEAT_TIMEOUT = 12000;
const MAX_RECONNECT_DELAY = 30000;
const MAX_QUEUE = 40;

export const socketEvents = new Emitter();

class RealtimeClient {
  #socket = null;
  #state = 'idle';
  #attempt = 0;
  #reconnectTimer = null;
  #heartbeatTimer = null;
  #pongTimer = null;
  #queue = [];
  #path = '/ws/app/';
  #manualClose = false;
  #started = false;
  #lastEventAt = 0;
  /** Monotonic connection id so late handlers can be ignored. */
  #epoch = 0;

  get state() { return this.#state; }
  get isOpen() { return this.#socket?.readyState === WebSocket.OPEN; }
  get epoch() { return this.#epoch; }
  get lastEventAt() { return this.#lastEventAt; }

  /** @param {string} path e.g. '/ws/app/' */
  start(path) {
    if (path) this.#path = path;
    if (this.#started) {
      if (!this.isOpen) this.#connect();
      return;
    }
    this.#started = true;
    this.#bindLifecycle();
    this.#connect();
  }

  stop() {
    this.#manualClose = true;
    this.#clearTimers();
    this.#queue.length = 0;
    if (this.#socket) {
      try { this.#socket.close(1000, 'client-stop'); } catch { /* already closing */ }
    }
    this.#socket = null;
    this.#setState('closed');
  }

  /** Force a fresh connection (e.g. after re-authentication). */
  restart() {
    this.#manualClose = false;
    this.#attempt = 0;
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
    if (queue) {
      this.#queue.push(frame);
      while (this.#queue.length > MAX_QUEUE) this.#queue.shift();
    }
    return false;
  }

  /* ---------------- internals ---------------- */

  #url() {
    const base = apiConfig.wsOrigin || window.location.origin.replace(/^http/i, 'ws');
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
    this.#setState(this.#attempt > 0 ? 'reconnecting' : 'connecting');

    const epoch = ++this.#epoch;
    let socket;
    try {
      socket = new WebSocket(this.#url());
    } catch (error) {
      this.#scheduleReconnect();
      return;
    }
    this.#socket = socket;

    socket.addEventListener('open', () => {
      if (epoch !== this.#epoch) return;
      this.#attempt = 0;
      this.#setState('open');
      this.#flushQueue();
      this.#startHeartbeat();
      socketEvents.emit('open', { epoch });
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
        socketEvents.emit('unauthorized', frame);
        this.#manualClose = true;
        try { socket.close(4003, 'unauthorized'); } catch { /* ignore */ }
        this.#setState('closed');
        return;
      }

      socketEvents.emit('event', { type, payload: frame.payload ?? frame.data ?? frame, raw: frame, epoch });
      socketEvents.emit(type, frame.payload ?? frame.data ?? frame, frame);
    });

    socket.addEventListener('error', () => {
      if (epoch !== this.#epoch) return;
      socketEvents.emit('error');
    });

    socket.addEventListener('close', (event) => {
      if (epoch !== this.#epoch) return;
      this.#clearTimers();
      this.#socket = null;
      socketEvents.emit('close', { code: event.code, reason: event.reason });
      if (this.#manualClose) {
        this.#setState('closed');
        return;
      }
      // 4003 = server rejected authentication; do not hammer it.
      if (event.code === 4003 || event.code === 1008) {
        socketEvents.emit('unauthorized', { code: event.code });
        this.#setState('closed');
        return;
      }
      this.#scheduleReconnect();
    });
  }

  #scheduleReconnect() {
    if (this.#manualClose) return;
    clearTimeout(this.#reconnectTimer);

    if (navigator.onLine === false) {
      this.#setState('offline');
      return; // the 'online' listener resumes us — no aggressive looping
    }

    this.#attempt += 1;
    const delay = backoffDelay(this.#attempt, { base: 1000, max: MAX_RECONNECT_DELAY });
    this.#setState('reconnecting', { attempt: this.#attempt, delay });
    this.#reconnectTimer = setTimeout(() => this.#connect(), delay);
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
    this.#clearHeartbeat();
  }

  #bindLifecycle() {
    window.addEventListener('online', () => {
      this.#attempt = 0;
      if (!this.#manualClose && !this.isOpen) this.#connect();
    });

    window.addEventListener('offline', () => {
      this.#setState('offline');
      this.#clearTimers();
      if (this.#socket) {
        try { this.#socket.close(4002, 'offline'); } catch { /* ignore */ }
      }
      this.#socket = null;
    });

    // PWA backgrounding / tab suspension: reconnect promptly on return and
    // let listeners resynchronise state from REST.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') return;
      if (this.#manualClose) return;
      if (!this.isOpen) {
        this.#attempt = 0;
        this.#connect();
      } else {
        socketEvents.emit('resume');
      }
    });

    window.addEventListener('pageshow', (event) => {
      if (event.persisted && !this.#manualClose && !this.isOpen) {
        this.#attempt = 0;
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
