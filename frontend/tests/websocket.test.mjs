/**
 * NEXORA — realtime transport lifecycle tests.
 *
 *   node --test frontend/tests/
 *
 * The client is exercised against a fake WebSocket and a fake refresh
 * endpoint, so every branch of the reconnection state machine is observable
 * without a browser or a running backend. These tests exist because the bug
 * they guard against — an endless "Reconnecting…" banner — was invisible to
 * every other layer of testing.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

/* ------------------------------------------------------------------ stubs */

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.sent = [];
    this.listeners = new Map();
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  dispatch(type, event = {}) {
    for (const handler of this.listeners.get(type) || []) handler(event);
  }

  send(data) {
    this.sent.push(data);
  }

  close(code = 1000, reason = '') {
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.dispatch('close', { code, reason });
  }

  /* helpers */
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.dispatch('open', {});
  }

  serverClose(code, reason = '') {
    this.readyState = FakeWebSocket.CLOSED;
    this.dispatch('close', { code, reason });
  }

  message(payload) {
    this.dispatch('message', { data: JSON.stringify(payload) });
  }
}

/** Minimal window/document/navigator so the modules can bind lifecycle hooks. */
function installEnvironment() {
  const listeners = new Map();
  const target = {
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    removeEventListener() {},
    dispatch(type, event = {}) {
      for (const handler of listeners.get(type) || []) handler(event);
    },
  };

  globalThis.location = {
    hostname: 'nexora-eight-lilac.vercel.app',
    port: '',
    protocol: 'https:',
    origin: 'https://nexora-eight-lilac.vercel.app',
  };
  globalThis.window = Object.assign(target, { location: globalThis.location });
  globalThis.document = {
    visibilityState: 'visible',
    addEventListener: target.addEventListener,
    removeEventListener() {},
    querySelector: () => null,
    getElementById: () => null,
    createElement: () => ({ style: {}, setAttribute() {}, append() {} }),
  };
  globalThis.navigator = { onLine: true };
  globalThis.WebSocket = FakeWebSocket;
  globalThis.localStorage = {
    store: new Map(),
    getItem(k) { return this.store.has(k) ? this.store.get(k) : null; },
    setItem(k, v) { this.store.set(k, v); },
    removeItem(k) { this.store.delete(k); },
  };
  return target;
}

let moduleCounter = 0;

/**
 * Load a fresh copy of the realtime client with a scripted /api/auth/refresh/.
 * @param {Array<{ok:boolean,status?:number,network?:boolean}>} refreshResponses
 */
async function loadClient(refreshResponses = []) {
  installEnvironment();
  FakeWebSocket.instances.length = 0;

  const refreshCalls = [];
  globalThis.fetch = async (url) => {
    refreshCalls.push(String(url));
    const next = refreshResponses.shift() || { ok: false, status: 401 };
    if (next.network) throw new TypeError('Failed to fetch');
    return {
      ok: next.ok,
      status: next.status ?? (next.ok ? 200 : 401),
      headers: { get: () => 'application/json' },
      json: async () => ({ success: next.ok, message: '', data: {} }),
      text: async () => '',
    };
  };

  const suffix = `?case=${moduleCounter++}`;
  const ws = await import(new URL(`../assets/js/websocket.js${suffix}`, import.meta.url).href);
  return { ...ws, refreshCalls };
}

/** Let queued promise callbacks run. */
const flush = async (times = 6) => {
  for (let i = 0; i < times; i += 1) await Promise.resolve();
};

/* ------------------------------------------------------------------ tests */

test('the socket URL is the production Render endpoint', async () => {
  const { realtime } = await loadClient();
  realtime.start('/ws/app/');
  assert.equal(FakeWebSocket.instances.length, 1);
  assert.equal(FakeWebSocket.instances[0].url, 'wss://nexora-backend-ptsc.onrender.com/ws/app/');
  realtime.stop();
});

test('connecting → open clears the reconnecting state immediately', async () => {
  const { realtime, socketEvents } = await loadClient();
  const states = [];
  socketEvents.on('state', (state) => states.push(state));

  realtime.start('/ws/app/');
  assert.equal(realtime.state, 'connecting');
  FakeWebSocket.instances[0].open();

  assert.equal(realtime.state, 'open');
  assert.deepEqual(states, ['connecting', 'open']);
  realtime.stop();
});

test('an authentication failure refreshes once and reconnects on success', async () => {
  const { realtime, refreshCalls } = await loadClient([{ ok: true }]);
  realtime.start('/ws/app/');
  const first = FakeWebSocket.instances[0];
  first.open();
  first.message({ type: 'auth.error', code: 'TOKEN_EXPIRED' });
  await flush();

  assert.equal(refreshCalls.length, 1);
  assert.match(refreshCalls[0], /\/api\/auth\/refresh\/$/);
  assert.equal(FakeWebSocket.instances.length, 2, 'a new socket must be opened after the refresh');
  realtime.stop();
});

test('a rejected refresh stops the client instead of reconnecting forever', async () => {
  const { realtime, socketEvents, refreshCalls } = await loadClient([{ ok: false, status: 401 }]);
  let unauthorized = 0;
  socketEvents.on('unauthorized', () => { unauthorized += 1; });

  realtime.start('/ws/app/');
  const first = FakeWebSocket.instances[0];
  first.open();
  first.message({ type: 'auth.error', code: 'TOKEN_EXPIRED' });
  await flush();

  assert.equal(refreshCalls.length, 1, 'exactly one refresh attempt');
  assert.equal(unauthorized, 1);
  assert.equal(realtime.state, 'closed');
  assert.equal(FakeWebSocket.instances.length, 1, 'no further sockets are created');
});

test('a handshake refused before opening does not loop when the session is dead', async () => {
  const { realtime, socketEvents } = await loadClient([{ ok: false, status: 401 }]);
  let unauthorized = 0;
  socketEvents.on('unauthorized', () => { unauthorized += 1; });

  realtime.start('/ws/app/');
  FakeWebSocket.instances[0].serverClose(1006); // browser's opaque rejection
  await flush();

  assert.equal(unauthorized, 1);
  assert.equal(realtime.state, 'closed');
  assert.equal(FakeWebSocket.instances.length, 1);
});

test('a transport failure with a live session retries with backoff', async () => {
  const { realtime } = await loadClient([{ ok: true }]);
  realtime.start('/ws/app/');
  FakeWebSocket.instances[0].serverClose(1006);
  await flush();

  assert.equal(realtime.state, 'reconnecting');
  assert.equal(FakeWebSocket.instances.length, 1, 'the retry is scheduled, not immediate');
  realtime.stop();
});

test('an unreachable backend is never mistaken for an expired session', async () => {
  const { realtime, socketEvents } = await loadClient([{ network: true }]);
  let unauthorized = 0;
  socketEvents.on('unauthorized', () => { unauthorized += 1; });

  realtime.start('/ws/app/');
  FakeWebSocket.instances[0].serverClose(1006);
  await flush();

  assert.equal(unauthorized, 0, 'a network outage must not sign the user out');
  assert.equal(realtime.state, 'reconnecting');
  realtime.stop();
});

test('stop() is terminal: no reconnect, no queue, socket closed', async () => {
  const { realtime } = await loadClient();
  realtime.start('/ws/app/');
  const socket = FakeWebSocket.instances[0];
  socket.open();

  realtime.stop('logout');
  assert.equal(realtime.state, 'closed');
  assert.equal(socket.readyState, FakeWebSocket.CLOSED);

  // Late close events from the dead socket must not schedule a reconnect.
  socket.dispatch('close', { code: 1006 });
  await flush();
  assert.equal(realtime.state, 'closed');
  assert.equal(FakeWebSocket.instances.length, 1);

  // Queued sends are dropped while stopped.
  realtime.send('typing', { conversation_id: 'x' });
  assert.equal(FakeWebSocket.instances.length, 1);
});

test('start() after a stop (sign in again) revives the transport', async () => {
  const { realtime } = await loadClient();
  realtime.start('/ws/app/');
  FakeWebSocket.instances[0].open();
  realtime.stop('logout');

  realtime.start('/ws/app/');
  assert.equal(FakeWebSocket.instances.length, 2);
  assert.equal(realtime.state, 'connecting');
  realtime.stop();
});

test('going offline reports offline and does not hammer the backend', async () => {
  const { realtime } = await loadClient();
  const win = globalThis.window;
  realtime.start('/ws/app/');
  FakeWebSocket.instances[0].open();

  globalThis.navigator.onLine = false;
  win.dispatch('offline');
  assert.equal(realtime.state, 'offline');
  assert.equal(FakeWebSocket.instances.length, 1);

  globalThis.navigator.onLine = true;
  win.dispatch('online');
  assert.equal(FakeWebSocket.instances.length, 2, 'exactly one controlled attempt on resume');
  realtime.stop();
});

test('only one socket exists even if start() is called repeatedly', async () => {
  const { realtime } = await loadClient();
  realtime.start('/ws/app/');
  realtime.start('/ws/app/');
  realtime.start('/ws/app/');
  assert.equal(FakeWebSocket.instances.length, 1);
  realtime.stop();
});

test('connection labels never leave the UI stuck on a technical state', async () => {
  const { connectionLabel } = await loadClient();
  assert.equal(connectionLabel('open'), 'Connected');
  assert.equal(connectionLabel('reconnecting'), 'Reconnecting…');
  assert.equal(connectionLabel('offline'), "You're offline");
  assert.equal(connectionLabel('idle'), '');
});
