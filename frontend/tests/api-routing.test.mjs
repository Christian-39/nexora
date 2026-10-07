/** Routing/error diagnostics for same-origin and configured API origins. */

import assert from 'node:assert/strict';
import test from 'node:test';
import { setNavigator } from './helpers/browser-env.mjs';

const API_MODULE = new URL('../assets/js/api.js', import.meta.url).href;

function installEnvironment(fetchImpl) {
  globalThis.location = {
    hostname: '127.0.0.1',
    port: '5500',
    protocol: 'http:',
    origin: 'http://127.0.0.1:5500',
    href: 'http://127.0.0.1:5500/login.html',
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
  globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: '' };
  globalThis.fetch = fetchImpl;
}

test('POST login remains a POST, reports same-origin 405 with safe route diagnostics, and does not redirect', async () => {
  const calls = [];
  installEnvironment(async (url, init = {}) => {
    const requestUrl = new URL(String(url));
    calls.push({ url: requestUrl, init });
    if (requestUrl.pathname === '/api/auth/csrf/') {
      return new Response(JSON.stringify({ success: true, data: { csrf_token: 'test-csrf' } }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }
    assert.equal(requestUrl.pathname, '/api/auth/login/');
    return new Response('<html><body>method not allowed</body></html>', {
      status: 405,
      headers: { 'content-type': 'text/html' },
    });
  });

  const apiModule = await import(`${API_MODULE}?routing-case=localhost-405`);
  await assert.rejects(
    () => apiModule.api.auth.login('08000000000', 'wrong-pin'),
    (error) => {
      assert.equal(error.status, 405);
      assert.equal(error.method, 'POST');
      assert.equal(error.endpoint, 'http://127.0.0.1:5500/api/auth/login/');
      assert.equal(error.host, '127.0.0.1:5500');
      assert.match(error.message, /API_BASE_URL|route configuration/i);
      assert.doesNotMatch(error.message, /<html>|method not allowed/i);
      return true;
    },
  );

  assert.deepEqual(calls.map(({ url }) => url.pathname), ['/api/auth/csrf/', '/api/auth/login/']);
  assert.equal(calls[1].init.method, 'POST');
  assert.equal(calls[1].url.origin, 'http://127.0.0.1:5500');
  assert.equal(globalThis.location.href, 'http://127.0.0.1:5500/login.html');
});
