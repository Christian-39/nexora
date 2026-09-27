/**
 * WebSocket handshake guard tests.
 *
 *   node --test tests/ws-connect-timeout.test.mjs
 *
 * A handshake that never completes (cold backend, silently dropped upgrade)
 * must be recycled into the normal bounded backoff — never leave the client
 * hanging in "connecting" forever.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

class StuckWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = StuckWebSocket.CONNECTING;
    this.listeners = new Map();
    this.closedWith = null;
    StuckWebSocket.instances.push(this);
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  close(code = 1000, reason = '') {
    if (this.readyState === StuckWebSocket.CLOSED) return;
    this.closedWith = { code, reason };
    this.readyState = StuckWebSocket.CLOSED;
  }
}

function installEnvironment() {
  const listeners = new Map();
  const target = {
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    removeEventListener() {},
    dispatch(type) {
      for (const handler of listeners.get(type) || []) handler();
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
  globalThis.WebSocket = StuckWebSocket;
  return target;
}

async function loadClient({ connectTimeoutMs = 60 } = {}) {
  const bus = installEnvironment();
  StuckWebSocket.instances.length = 0;
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ success: true, message: '', data: {} }),
    text: async () => '',
  });
  globalThis.NEXORA_RUNTIME = { connectTimeoutMs };
  const suffix = `?case=${Math.random().toString(36).slice(2)}`;
  const mod = await import(new URL(`../assets/js/websocket.js${suffix}`, import.meta.url).href);
  return { ...mod, bus };
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('a stuck handshake is closed and retried through the backoff path', async () => {
  const { realtime, socketEvents } = await loadClient({ connectTimeoutMs: 50 });
  const states = [];
  socketEvents.on('state', (state) => states.push(state));

  realtime.start('/ws/app/');
  assert.equal(realtime.state, 'connecting');
  const first = StuckWebSocket.instances[0];

  await wait(120); // connect timeout (50ms) fires
  assert.equal(first.closedWith?.code, 4000, 'the stuck socket is recycled');
  assert.ok(states.includes('reconnecting'), `recycled into backoff, saw: ${states.join(',')}`);
  // Exactly one socket exists at a time; the retry is scheduled with backoff.
  assert.ok(StuckWebSocket.instances.length <= 2);
  realtime.stop();
});

test('opening in time cancels the guard — no stray reconnects', async () => {
  const { realtime, socketEvents } = await loadClient({ connectTimeoutMs: 80 });
  let strayReconnects = 0;
  socketEvents.on('state', (state) => {
    if (state === 'reconnecting') strayReconnects += 1;
  });

  realtime.start('/ws/app/');
  const socket = StuckWebSocket.instances[0];
  socket.readyState = StuckWebSocket.OPEN;
  socket.listeners.get('open')[0]({});
  await wait(200); // well past the guard window
  assert.equal(strayReconnects, 0, 'a clean open must not produce reconnects');
  assert.equal(realtime.state, 'open');
  realtime.stop();
});

test('a foregrounded tab retries once, immediately, without resetting backoff', async () => {
  const { realtime, bus } = await loadClient({ connectTimeoutMs: 10_000 });
  try {
    realtime.start('/ws/app/');
    const first = StuckWebSocket.instances[0];
    // Simulate a refused handshake (never opened) falling into the
    // refresh-probe → backoff path.
    first.readyState = StuckWebSocket.CLOSED;
    for (const handler of first.listeners.get('close') || []) handler({ code: 1006 });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.ok(
      realtime.state === 'reconnecting' || realtime.state === 'connecting',
      `expected a recovery path, saw ${realtime.state}`
    );

    // The tab is backgrounded and foregrounded repeatedly. Each return may
    // trigger ONE controlled connect attempt, but the backoff position is
    // never reset by visibility — so this cannot become a retry storm.
    for (let i = 0; i < 3; i += 1) {
      globalThis.document.visibilityState = 'hidden';
      bus.dispatch('visibilitychange');
      globalThis.document.visibilityState = 'visible';
      bus.dispatch('visibilitychange');
    }
    const sockets = StuckWebSocket.instances.length;
    assert.ok(sockets <= 4, `at most one foreground attempt per cycle, saw ${sockets}`);
  } finally {
    realtime.stop();
  }
});
