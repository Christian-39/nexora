/** A rejected login attempt must not clear an existing bearer session. */

import assert from 'node:assert/strict';
import test from 'node:test';

const API_MODULE = new URL('../assets/js/api.js', import.meta.url).href;
const API_ORIGIN = 'https://api.example.test';

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function envelope(data = {}) {
  return { success: true, message: '', data };
}

function readHeader(headers, name) {
  const entry = Object.entries(headers || {}).find(([key]) => key.toLowerCase() === name.toLowerCase());
  return entry?.[1] ?? null;
}

test('failed member login does not attach or clear the prior administrator bearer session', async () => {
  globalThis.location = {
    hostname: 'frontend.example.test',
    port: '',
    protocol: 'https:',
    origin: 'https://frontend.example.test',
    href: 'https://frontend.example.test/login.html',
  };
  globalThis.window = { location: globalThis.location, addEventListener() {}, removeEventListener() {} };
  globalThis.document = { cookie: '', querySelector: () => null, getElementById: () => null };
  globalThis.navigator = { onLine: true };
  globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: API_ORIGIN, AUTH_MODE: 'bearer' };

  let loginCalls = 0;
  globalThis.fetch = async (url, init = {}) => {
    const requestUrl = new URL(String(url));
    if (requestUrl.pathname === '/api/auth/login/') {
      loginCalls += 1;
      assert.equal(init.method, 'POST');
      assert.equal(readHeader(init.headers, 'Authorization'), null);
      return jsonResponse({
        success: false,
        message: 'Invalid phone number or PIN.',
        code: 'INVALID_CREDENTIALS',
        errors: {},
      }, 401);
    }
    if (requestUrl.pathname === '/api/me/') {
      assert.equal(readHeader(init.headers, 'Authorization'), 'Bearer prior-admin-session');
      return jsonResponse(envelope({ id: 'admin-1', role: 'ADMIN' }));
    }
    throw new Error(`Unexpected URL: ${requestUrl.pathname}`);
  };

  const apiModule = await import(`${API_MODULE}?login-isolation=bearer`);
  apiModule.tokenStore.set('prior-admin-session');

  await assert.rejects(
    apiModule.api.auth.login('08000000000', 'wrong-pin'),
    (error) => error.status === 401 && error.code === 'INVALID_CREDENTIALS',
  );
  assert.equal(apiModule.tokenStore.get(), 'prior-admin-session');

  const admin = await apiModule.api.me.get();
  assert.deepEqual(admin, { id: 'admin-1', role: 'ADMIN' });
  assert.equal(loginCalls, 1);
});
