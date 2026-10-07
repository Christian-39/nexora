/**
 * In-flight GET de-duplication tests for the shared HTTP layer.
 *
 *   node --test tests/api-dedup.test.mjs
 *
 * Two components requesting the same resource in the same tick must share
 * ONE network request; a caller that supplies its own AbortSignal keeps a
 * private lifecycle and is never deduplicated.
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { setNavigator } from './helpers/browser-env.mjs';

const API_ORIGIN = 'https://api.example.test';
let moduleCounter = 0;

function installEnvironment(fetchImpl) {
  const listeners = new Map();
  globalThis.location = {
    hostname: 'frontend.example.test',
    port: '',
    protocol: 'https:',
    origin: 'https://frontend.example.test',
  };
  globalThis.window = {
    location: globalThis.location,
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    removeEventListener() {},
  };
  globalThis.document = {
    cookie: '',
    querySelector: () => null,
    getElementById: () => null,
  };
  setNavigator({ onLine: true });
  globalThis.fetch = fetchImpl;
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: API_ORIGIN };
}

async function loadApi() {
  const suffix = `?case=${moduleCounter++}`;
  return import(new URL(`../assets/js/api.js${suffix}`, import.meta.url).href);
}

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

test('identical concurrent GETs share one network request', async () => {
  let calls = 0;
  installEnvironment(async () => {
    calls += 1;
    await new Promise((resolve) => setTimeout(resolve, 20));
    return jsonResponse({ success: true, message: 'ok', data: { value: calls } });
  });
  const { api } = await loadApi();

  const [a, b, c] = await Promise.all([api.me.get(), api.me.get(), api.me.get()]);
  assert.equal(calls, 1, 'exactly one network request');
  assert.deepEqual(a, { value: 1 });
  assert.equal(b.value, 1);
  assert.equal(c.value, 1);
});

test('GET calls with different response modes or headers are not shared', async () => {
  let calls = 0;
  installEnvironment(async () => {
    calls += 1;
    const seq = calls;
    await new Promise((resolve) => setTimeout(resolve, 10));
    return jsonResponse({ success: true, message: 'ok', data: { value: seq } });
  });
  const { api } = await loadApi();

  const [plain, raw] = await Promise.all([
    api.me.get(),
    api.me.get({ raw: true, headers: { 'X-View': 'audit' } }),
  ]);
  assert.equal(calls, 2, 'distinct consumer contracts must keep separate network requests');
  assert.ok(Number.isInteger(plain.value));
  assert.ok(Number.isInteger(raw.data.value));
  assert.equal(raw.status, 200);
});

test('sequential GETs are NOT deduplicated (freshness matters)', async () => {
  let calls = 0;
  installEnvironment(async () => {
    calls += 1;
    return jsonResponse({ success: true, message: 'ok', data: { value: calls } });
  });
  const { api } = await loadApi();

  const first = await api.me.get();
  const second = await api.me.get();
  assert.equal(calls, 2);
  assert.equal(first.value, 1);
  assert.equal(second.value, 2);
});

test('a GET with a caller signal is never shared', async () => {
  let calls = 0;
  installEnvironment(async () => {
    calls += 1;
    const seq = calls;
    await new Promise((resolve) => setTimeout(resolve, 10));
    return jsonResponse({ success: true, message: 'ok', data: { value: seq } });
  });
  const { api } = await loadApi();

  const controller = new AbortController();
  const [a, b] = await Promise.all([
    api.me.get({ signal: controller.signal }),
    api.me.get(),
  ]);
  assert.equal(calls, 2, 'the signal-carrying request stays private');
  assert.deepEqual([a.value, b.value].sort(), [1, 2], 'both callers each got their own response');
});

test('different URLs are separate requests', async () => {
  const seen = [];
  installEnvironment(async (input) => {
    seen.push(String(input));
    return jsonResponse({ success: true, message: 'ok', data: {} });
  });
  const { api } = await loadApi();

  await Promise.all([api.me.get(), api.notifications.unreadCount()]);
  assert.equal(seen.length, 2);
  assert.ok(seen.some((u) => u.endsWith('/api/me/')));
  assert.ok(seen.some((u) => u.endsWith('/api/notifications/unread-count/')));
});

test('pagination links cannot send authenticated requests to a foreign origin', async () => {
  let calls = 0;
  installEnvironment(async () => {
    calls += 1;
    return jsonResponse({ success: true, message: 'ok', data: { value: 'ok' } });
  });
  const { fetchPageUrl, ApiError } = await loadApi();

  assert.throws(
    () => fetchPageUrl('https://attacker.example/api/messages/?cursor=secret'),
    (error) => error instanceof ApiError && error.code === 'BAD_LINK',
  );
  assert.equal(calls, 0, 'a foreign URL is rejected before network access');

  const result = await fetchPageUrl(`${API_ORIGIN}/api/messages/?cursor=next`);
  assert.deepEqual(result, { value: 'ok' });
  assert.equal(calls, 1);
});

test('a failed shared GET does not poison the next one', async () => {
  let calls = 0;
  installEnvironment(async () => {
    calls += 1;
    if (calls === 1) return jsonResponse({ success: false, message: 'boom' }, 500);
    return jsonResponse({ success: true, message: 'ok', data: { value: 'ok' } });
  });
  const { api } = await loadApi();

  await assert.rejects(() => api.me.get());
  const second = await api.me.get();
  assert.equal(second.value, 'ok');
});
