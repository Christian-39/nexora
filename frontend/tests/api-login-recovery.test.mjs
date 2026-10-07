/** Successful login clears a stale negative session-refresh latch; failures do not. */

import assert from 'node:assert/strict';
import test from 'node:test';
import { setNavigator } from './helpers/browser-env.mjs';

const API_MODULE = new URL('../assets/js/api.js', import.meta.url).href;
const REFRESH_REJECTED_KEY = 'nexora.auth.refresh-rejected.v1';

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

test('failed PIN login leaves auth recovery state alone; successful login clears the rejected-refresh latch', async () => {
  const session = new Map([[REFRESH_REJECTED_KEY, '1']]);
  globalThis.location = {
    hostname: 'frontend.example.test', port: '', protocol: 'https:',
    origin: 'https://frontend.example.test', href: 'https://frontend.example.test/login.html',
  };
  globalThis.window = { location: globalThis.location, addEventListener() {}, removeEventListener() {} };
  globalThis.document = { cookie: '', querySelector: () => null, getElementById: () => null };
  setNavigator({ onLine: true });
  globalThis.sessionStorage = {
    getItem: (key) => session.get(key) ?? null,
    setItem: (key, value) => session.set(key, String(value)),
    removeItem: (key) => session.delete(key),
  };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: 'https://api.example.test', AUTH_MODE: 'cookie' };

  let logins = 0;
  globalThis.fetch = async (url) => {
    const path = new URL(String(url)).pathname;
    if (path === '/api/auth/csrf/') {
      return jsonResponse({ success: true, message: '', data: { csrf_token: 'csrf-login-recovery' } });
    }
    assert.equal(path, '/api/auth/login/');
    logins += 1;
    if (logins === 1) {
      return jsonResponse({
        success: false, message: 'Invalid phone number or PIN.', code: 'INVALID_CREDENTIALS', errors: {},
      }, 401);
    }
    return jsonResponse({ success: true, message: 'ok', data: { id: 'user-1' } });
  };

  const apiModule = await import(`${API_MODULE}?login-recovery=reset-latch`);
  await assert.rejects(
    apiModule.api.auth.login('08000000000', 'wrong-pin'),
    (error) => error.status === 401 && error.code === 'INVALID_CREDENTIALS',
  );
  assert.equal(session.get(REFRESH_REJECTED_KEY), '1');

  const profile = await apiModule.api.auth.login('08000000000', 'correct-pin');
  assert.deepEqual(profile, { id: 'user-1' });
  assert.equal(session.has(REFRESH_REJECTED_KEY), false);
  assert.equal(logins, 2);
});
