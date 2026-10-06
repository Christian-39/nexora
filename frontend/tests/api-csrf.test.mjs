/**
 * Cross-origin CSRF tests for the centralized HTTP layer.
 *
 * The simulated page is on Vercel, document.cookie is deliberately empty,
 * and the backend token is available only in the CSRF endpoint's JSON body.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

const API_MODULE = new URL('../assets/js/api.js', import.meta.url).href;
const API_ORIGIN = 'https://api.example.test';
let moduleCounter = 0;

function installEnvironment() {
  const listeners = new Map();
  globalThis.location = {
    hostname: 'frontend.example.test',
    port: '',
    protocol: 'https:',
    origin: 'https://frontend.example.test',
    href: 'https://frontend.example.test/login.html',
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
    // A Vercel page cannot see cookies scoped to the Render hostname.
    cookie: '',
    querySelector: () => null,
    getElementById: () => null,
  };
  globalThis.navigator = { onLine: true };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: API_ORIGIN };
}

function jsonResponse(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

async function loadApi(fetchImpl) {
  installEnvironment();
  globalThis.fetch = fetchImpl;
  return import(`${API_MODULE}?csrf-case=${moduleCounter++}`);
}

function pathname(call) {
  return new URL(call.url).pathname;
}

function header(call, name) {
  const entry = Object.entries(call.init.headers || {}).find(([key]) => key.toLowerCase() === name.toLowerCase());
  return entry?.[1] ?? null;
}

test('login reads the JSON CSRF token and reuses it for centralized unsafe requests', { concurrency: false }, async () => {
  const calls = [];
  const token = 'csrf-token-returned-in-json';
  const apiModule = await loadApi(async (url, init = {}) => {
    calls.push({ url: String(url), init });
    const path = new URL(url).pathname;
    if (path === '/api/auth/csrf/') {
      return jsonResponse(200, {
        success: true,
        message: 'CSRF cookie set',
        data: { csrf_token: token },
      });
    }
    return jsonResponse(200, { success: true, message: 'ok', data: { id: 'user-1' } });
  });

  await apiModule.api.auth.login('+2348012345678', '123456');
  await apiModule.api.me.update({ theme: 'dark' });

  assert.deepEqual(calls.map(pathname), [
    '/api/auth/csrf/',
    '/api/auth/login/',
    '/api/me/',
  ]);
  assert.equal(calls[0].init.credentials, 'include');
  assert.equal(calls[1].init.credentials, 'include');
  assert.equal(calls[2].init.credentials, 'include');
  assert.equal(header(calls[1], 'X-CSRFToken'), token);
  assert.equal(header(calls[2], 'X-CSRFToken'), token);
  assert.equal(globalThis.document.cookie, '', 'the test must not obtain the token from document.cookie');
});

test('a 401 refresh obtains CSRF before posting and keeps credentials enabled', { concurrency: false }, async () => {
  const calls = [];
  const token = 'csrf-before-initial-refresh';
  const apiModule = await loadApi(async (url, init = {}) => {
    calls.push({ url: String(url), init });
    const path = new URL(url).pathname;
    if (path === '/api/me/') {
      return jsonResponse(401, {
        success: false,
        message: 'Authentication credentials were not provided.',
        code: 'NOT_AUTHENTICATED',
        errors: {},
      });
    }
    if (path === '/api/auth/csrf/') {
      return jsonResponse(200, {
        success: true,
        message: 'CSRF cookie set',
        data: { csrf_token: token },
      });
    }
    if (path === '/api/auth/refresh/') {
      return jsonResponse(401, {
        success: false,
        message: 'Session expired.',
        code: 'INVALID_SESSION',
        errors: {},
      });
    }
    throw new Error(`Unexpected request: ${path}`);
  });

  await assert.rejects(() => apiModule.api.me.get({ retries: 0 }), (error) => error.status === 401);

  assert.deepEqual(calls.map(pathname), [
    '/api/me/',
    '/api/auth/csrf/',
    '/api/auth/refresh/',
  ]);
  const refresh = calls[2];
  assert.equal(refresh.init.credentials, 'include');
  assert.equal(header(refresh, 'X-CSRFToken'), token);
});

test('an unsafe request is not sent when CSRF bootstrap returns no usable token', { concurrency: false }, async () => {
  const calls = [];
  const apiModule = await loadApi(async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return jsonResponse(200, { success: true, message: 'CSRF cookie set', data: {} });
  });

  await assert.rejects(
    () => apiModule.api.auth.login('+2348012345678', '123456'),
    (error) => error.code === 'CSRF_UNAVAILABLE'
  );
  assert.deepEqual(calls.map(pathname), ['/api/auth/csrf/']);
});

test('a CSRF-specific 403 refreshes the token once and retries login without redirecting the API origin', { concurrency: false }, async () => {
  const calls = [];
  let csrfCalls = 0;
  let loginCalls = 0;
  const apiModule = await loadApi(async (url, init = {}) => {
    calls.push({ url: String(url), init });
    const path = new URL(url).pathname;
    if (path === '/api/auth/csrf/') {
      csrfCalls += 1;
      return jsonResponse(200, {
        success: true,
        message: 'CSRF cookie set',
        data: { csrf_token: `csrf-${csrfCalls}` },
      });
    }
    assert.equal(path, '/api/auth/login/');
    loginCalls += 1;
    assert.equal(new URL(url).origin, API_ORIGIN);
    assert.equal(header(calls.at(-1), 'X-CSRFToken'), `csrf-${loginCalls}`);
    return loginCalls === 1
      ? jsonResponse(403, { success: false, message: 'CSRF validation failed.', code: 'CSRF_FAILED', errors: {} })
      : jsonResponse(200, { success: true, message: 'ok', data: { id: 'user-1' } });
  });

  const user = await apiModule.api.auth.login('+2348012345678', '123456');
  assert.equal(user.id, 'user-1');
  assert.equal(csrfCalls, 2);
  assert.equal(loginCalls, 2);
  assert.deepEqual(calls.map(pathname), [
    '/api/auth/csrf/',
    '/api/auth/login/',
    '/api/auth/csrf/',
    '/api/auth/login/',
  ]);
});
