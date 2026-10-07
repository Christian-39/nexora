/** A login-page session probe must not overwrite a newer successful sign-in. */

import assert from 'node:assert/strict';
import test from 'node:test';
import { setNavigator } from './helpers/browser-env.mjs';

const AUTH_MODULE = new URL('../assets/js/auth.js', import.meta.url).href;

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

test('a late aborted /api/me probe cannot erase or redirect after login succeeds', async () => {
  const calls = [];
  let meStarted = false;
  globalThis.location = {
    hostname: 'frontend.example.test', port: '', protocol: 'https:',
    origin: 'https://frontend.example.test', href: 'https://frontend.example.test/login.html',
    pathname: '/login.html', search: '', replace(path) { this.replaced = path; },
  };
  globalThis.window = {
    location: globalThis.location,
    addEventListener() {}, removeEventListener() {},
  };
  globalThis.document = {
    cookie: '', visibilityState: 'visible',
    getElementById: () => null, querySelector: () => null,
    addEventListener() {}, removeEventListener() {},
  };
  setNavigator({ onLine: true });
  const session = new Map();
  globalThis.sessionStorage = {
    getItem: (key) => session.get(key) ?? null,
    setItem: (key, value) => session.set(key, String(value)),
    removeItem: (key) => session.delete(key),
  };
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: 'https://api.example.test', AUTH_MODE: 'cookie' };
  globalThis.fetch = async (url, init = {}) => {
    const requestUrl = new URL(String(url));
    calls.push({ path: requestUrl.pathname, method: init.method || 'GET' });
    if (requestUrl.pathname === '/api/me/') {
      meStarted = true;
      return new Promise((_resolve, reject) => {
        init.signal.addEventListener('abort', () => {
          reject(new DOMException('probe aborted', 'AbortError'));
        }, { once: true });
      });
    }
    if (requestUrl.pathname === '/api/auth/csrf/') {
      return jsonResponse({ success: true, message: '', data: { csrf_token: 'csrf-race-test' } });
    }
    if (requestUrl.pathname === '/api/auth/login/') {
      return jsonResponse({
        success: true,
        message: 'Login successful',
        data: { id: 'admin-1', role: 'ADMIN', must_change_pin: false },
      });
    }
    throw new Error(`Unexpected request: ${requestUrl.pathname}`);
  };

  const auth = await import(`${AUTH_MODULE}?session-race=login-wins`);
  const probeController = new AbortController();
  const probe = auth.redirectIfAuthenticated({ signal: probeController.signal });
  assert.equal(meStarted, true);

  // login() advances the auth revision synchronously; the login page also
  // aborts its in-flight session probe as soon as submission starts.
  const signedIn = auth.login('08000000000', '123456');
  probeController.abort();
  const [loginResult, probeRedirected] = await Promise.all([signedIn, probe]);

  assert.equal(loginResult.user.id, 'admin-1');
  assert.equal(loginResult.user.role, 'admin');
  assert.equal(probeRedirected, false);
  assert.equal(auth.getUser().id, 'admin-1');
  assert.equal(globalThis.location.replaced, undefined);
  assert.deepEqual(calls.map(({ path }) => path), [
    '/api/me/',
    '/api/auth/csrf/',
    '/api/auth/login/',
  ]);
});
