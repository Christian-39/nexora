/**
 * Authentication refresh and public-config behavior in the central HTTP layer.
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { setNavigator } from './helpers/browser-env.mjs';

const API_ORIGIN = 'https://api.example.test';
let counter = 0;

function memoryStorage() {
  const values = new Map();
  return {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
}

function installEnvironment(fetchImpl) {
  globalThis.location = {
    hostname: 'frontend.example.test',
    port: '',
    protocol: 'https:',
    origin: 'https://frontend.example.test',
    href: 'https://frontend.example.test/chat.html',
  };
  globalThis.window = {
    location: globalThis.location,
    addEventListener() {},
    removeEventListener() {},
  };
  globalThis.document = {
    cookie: '',
    querySelector: () => null,
    getElementById: () => null,
  };
  setNavigator({ onLine: true });
  globalThis.sessionStorage = memoryStorage();
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: API_ORIGIN };
  globalThis.fetch = fetchImpl;
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function envelope(data = {}) {
  return { success: true, message: '', data };
}

async function loadApi(fetchImpl, runtime = {}) {
  installEnvironment(fetchImpl);
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: API_ORIGIN, ...runtime };
  return import(new URL(`../assets/js/api.js?auth-case=${counter++}`, import.meta.url).href);
}

const pathOf = (url) => new URL(String(url)).pathname;

test('concurrent authenticated 401s share one refresh and replay safe GETs', async () => {
  const counts = new Map();
  let refreshes = 0;
  const apiModule = await loadApi(async (url) => {
    const path = pathOf(url);
    counts.set(path, (counts.get(path) || 0) + 1);
    if (path === '/api/auth/csrf/') return jsonResponse(envelope({ csrf_token: 'csrf-test' }));
    if (path === '/api/auth/refresh/') {
      refreshes += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return jsonResponse(envelope({}));
    }
    if (path === '/api/me/a/' || path === '/api/me/b/') {
      const attempt = counts.get(path);
      return attempt === 1
        ? jsonResponse({ success: false, message: 'Expired', code: 'INVALID_SESSION', errors: {} }, 401)
        : jsonResponse(envelope({ path }));
    }
    throw new Error(`Unexpected URL: ${path}`);
  });

  const [a, b] = await Promise.all([
    apiModule.request('/api/me/a/', { retries: 0 }),
    apiModule.request('/api/me/b/', { retries: 0 }),
  ]);
  assert.equal(a.path, '/api/me/a/');
  assert.equal(b.path, '/api/me/b/');
  assert.equal(refreshes, 1);
  assert.equal(counts.get('/api/me/a/'), 2);
  assert.equal(counts.get('/api/me/b/'), 2);
});

test('a rejected refresh is latched and does not repeat on every request', async () => {
  const counts = new Map();
  let refreshes = 0;
  const apiModule = await loadApi(async (url) => {
    const path = pathOf(url);
    counts.set(path, (counts.get(path) || 0) + 1);
    if (path === '/api/auth/csrf/') return jsonResponse(envelope({ csrf_token: 'csrf-test' }));
    if (path === '/api/auth/refresh/') {
      refreshes += 1;
      return jsonResponse({ success: false, message: 'Expired', code: 'INVALID_SESSION', errors: {} }, 401);
    }
    return jsonResponse({ success: false, message: 'Expired', code: 'INVALID_SESSION', errors: {} }, 401);
  });

  for (const path of ['/api/me/a/', '/api/me/b/']) {
    await assert.rejects(apiModule.request(path, { retries: 0 }), (error) => error.status === 401);
  }
  assert.equal(refreshes, 1);
  assert.equal(counts.get('/api/auth/csrf/'), 1);
});

test('an unreachable refresh is not treated as logout and can be retried later', async () => {
  let refreshes = 0;
  let meCalls = 0;
  const apiModule = await loadApi(async (url) => {
    const path = pathOf(url);
    if (path === '/api/auth/csrf/') return jsonResponse(envelope({ csrf_token: 'csrf-test' }));
    if (path === '/api/auth/refresh/') {
      refreshes += 1;
      if (refreshes === 1) throw new TypeError('network down');
      return jsonResponse(envelope({}));
    }
    if (path === '/api/me/') {
      meCalls += 1;
      return meCalls < 3
        ? jsonResponse({ success: false, message: 'Expired', code: 'INVALID_SESSION', errors: {} }, 401)
        : jsonResponse(envelope({ id: 'verified-user' }));
    }
    throw new Error(`Unexpected URL: ${path}`);
  });

  let unauthorized = 0;
  apiModule.apiEvents.on('unauthorized', () => { unauthorized += 1; });
  await assert.rejects(apiModule.request('/api/me/', { retries: 0 }), (error) => error.code === 'AUTH_REFRESH_UNAVAILABLE');
  assert.equal(unauthorized, 0);
  const profile = await apiModule.request('/api/me/', { retries: 0 });
  assert.equal(profile.id, 'verified-user');
  assert.equal(refreshes, 2);
  assert.equal(unauthorized, 0);
});

test('a CSRF 403 during refresh is retryable and does not latch logout', async () => {
  let csrfCalls = 0;
  let refreshCalls = 0;
  let meCalls = 0;
  const apiModule = await loadApi(async (url, init) => {
    const path = pathOf(url);
    if (path === '/api/auth/csrf/') {
      csrfCalls += 1;
      return jsonResponse(envelope({ csrf_token: `csrf-${csrfCalls}` }));
    }
    if (path === '/api/auth/refresh/') {
      refreshCalls += 1;
      assert.equal(init.headers['X-CSRFToken'], `csrf-${csrfCalls}`);
      return refreshCalls === 1
        ? jsonResponse({ success: false, message: 'CSRF validation failed.' }, 403)
        : jsonResponse(envelope({}));
    }
    if (path === '/api/me/') {
      meCalls += 1;
      return meCalls < 3
        ? jsonResponse({ success: false, message: 'Expired', code: 'INVALID_SESSION', errors: {} }, 401)
        : jsonResponse(envelope({ id: 'verified-user' }));
    }
    throw new Error(`Unexpected URL: ${path}`);
  });

  let unauthorized = 0;
  apiModule.apiEvents.on('unauthorized', () => { unauthorized += 1; });
  await assert.rejects(
    apiModule.request('/api/me/', { retries: 0 }),
    (error) => error.code === 'AUTH_REFRESH_UNAVAILABLE',
  );
  assert.equal(unauthorized, 0);
  const profile = await apiModule.request('/api/me/', { retries: 0 });
  assert.equal(profile.id, 'verified-user');
  assert.equal(refreshCalls, 2);
  assert.equal(csrfCalls, 2, 'a 403 invalidates the cached CSRF token before retry');
  assert.equal(unauthorized, 0);
});

test('public config is unauthenticated, deduplicated, and does not trigger session refresh', async () => {
  let calls = 0;
  const apiModule = await loadApi(async (_url, init) => {
    calls += 1;
    assert.equal(init.cache, 'default');
    assert.equal(init.credentials, 'include');
    assert.equal(Object.keys(init.headers).some((key) => key.toLowerCase() === 'authorization'), false);
    await new Promise((resolve) => setTimeout(resolve, 5));
    return jsonResponse(envelope({ brand: 'Nexora' }));
  });

  const [first, second] = await Promise.all([apiModule.api.publicConfig(), apiModule.api.publicConfig()]);
  assert.deepEqual(first, { brand: 'Nexora' });
  assert.deepEqual(second, { brand: 'Nexora' });
  assert.equal(calls, 1);
});

test('a public endpoint 401 does not trigger refresh or an auth logout event', async () => {
  let refreshes = 0;
  const apiModule = await loadApi(async (url) => {
    const path = pathOf(url);
    if (path === '/api/auth/refresh/') refreshes += 1;
    return jsonResponse({ success: false, message: 'Unavailable', code: 'PUBLIC_CONFIG_ERROR', errors: {} }, 401);
  });
  let unauthorized = 0;
  apiModule.apiEvents.on('unauthorized', () => { unauthorized += 1; });

  await assert.rejects(apiModule.api.publicConfig(), (error) => error.status === 401);
  assert.equal(refreshes, 0);
  assert.equal(unauthorized, 0);
});
