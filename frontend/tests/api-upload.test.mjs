/** XHR upload completion, retry, progress, and timeout regressions. */

import assert from 'node:assert/strict';
import test from 'node:test';

const API_MODULE = new URL('../assets/js/api.js', import.meta.url).href;
const API_ORIGIN = 'https://api.example.test';
let moduleCounter = 0;

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
  globalThis.document = { cookie: '', querySelector: () => null, getElementById: () => null };
  globalThis.navigator = { onLine: true };
  globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: API_ORIGIN };
  globalThis.fetch = fetchImpl;
}

function mockXhr(outcomes, attempts) {
  class EventTargetMock {
    constructor() { this.listeners = new Map(); }
    addEventListener(type, callback) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(callback);
    }
    dispatch(type, event = {}) {
      for (const callback of this.listeners.get(type) || []) callback(event);
    }
  }

  globalThis.XMLHttpRequest = class extends EventTargetMock {
    constructor() {
      super();
      this.upload = new EventTargetMock();
      this.requestHeaders = new Map();
      this.status = 0;
      this.responseText = '';
      this.responseURL = '';
      this.timeout = 0;
      this.withCredentials = false;
      attempts.push(this);
    }
    open(method, url, async) {
      this.method = method;
      this.url = url;
      this.async = async;
    }
    setRequestHeader(name, value) { this.requestHeaders.set(name.toLowerCase(), value); }
    getResponseHeader() { return null; }
    send(body) {
      this.body = body;
      const outcome = outcomes.shift();
      assert.ok(outcome, 'unexpected extra XHR upload attempt');
      this.status = outcome.status;
      this.responseText = outcome.body;
      this.responseURL = this.url;
      this.upload.dispatch('progress', { lengthComputable: true, loaded: 99, total: 100 });
      queueMicrotask(() => this.dispatch('load'));
    }
    abort() { this.dispatch('abort'); }
  };
}

function response(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

async function loadApi(fetchImpl) {
  installEnvironment(fetchImpl);
  return import(`${API_MODULE}?upload-case=${moduleCounter++}`);
}

test('upload confirms final response after a 401 refresh and preserves progress/body on one replay', async () => {
  const fetchCalls = [];
  const apiModule = await loadApi(async (url, init = {}) => {
    const path = new URL(String(url)).pathname;
    fetchCalls.push({ path, init });
    if (path === '/api/auth/csrf/') {
      return response({ success: true, message: '', data: { csrf_token: 'csrf-upload' } });
    }
    if (path === '/api/auth/refresh/') return response({ success: true, message: '', data: {} });
    throw new Error(`Unexpected fetch: ${path}`);
  });

  const attempts = [];
  mockXhr([
    { status: 401, body: JSON.stringify({ success: false, message: 'Expired', code: 'INVALID_SESSION', errors: {} }) },
    { status: 201, body: JSON.stringify({ success: true, message: 'created', data: { id: 'message-1' } }) },
  ], attempts);
  const formData = new FormData();
  formData.append('client_id', 'stable-upload-id');
  const progress = [];

  const result = await apiModule.upload('/api/conversations/1/messages/', formData, {
    timeout: 45000,
    onProgress: (event) => progress.push(event.percent),
  });

  assert.deepEqual(result, { id: 'message-1' });
  assert.equal(attempts.length, 2);
  assert.deepEqual(attempts.map((xhr) => xhr.method), ['POST', 'POST']);
  assert.equal(attempts[0].url, `${API_ORIGIN}/api/conversations/1/messages/`);
  assert.equal(attempts[0].body, formData);
  assert.equal(attempts[1].body, formData);
  assert.equal(attempts[0].withCredentials, true);
  assert.equal(attempts[0].timeout, 45000);
  assert.equal(attempts[0].requestHeaders.get('x-csrftoken'), 'csrf-upload');
  assert.deepEqual(fetchCalls.map(({ path }) => path), ['/api/auth/csrf/', '/api/auth/refresh/']);
  assert.equal(progress.at(-1), 100, '100% is emitted only after a successful final response');
});

test('upload timeout remains opt-in; a slow final server response is not cut off by a new default', async () => {
  const apiModule = await loadApi(async (url) => {
    assert.equal(new URL(String(url)).pathname, '/api/auth/csrf/');
    return response({ success: true, message: '', data: { csrf_token: 'csrf-no-default-timeout' } });
  });
  const attempts = [];
  mockXhr([
    { status: 201, body: JSON.stringify({ success: true, message: 'created', data: { id: 'message-2' } }) },
  ], attempts);

  await apiModule.upload('/api/conversations/1/messages/', new FormData());
  assert.equal(attempts[0].timeout, 0);
});

test('a server error after 99% is a failed upload, not a successful completion', async () => {
  const apiModule = await loadApi(async (url) => {
    assert.equal(new URL(String(url)).pathname, '/api/auth/csrf/');
    return response({ success: true, message: '', data: { csrf_token: 'csrf-failure-test' } });
  });
  const attempts = [];
  mockXhr([
    { status: 500, body: '<html><body>private stack trace must not reach the UI</body></html>' },
  ], attempts);
  const progress = [];

  await assert.rejects(
    () => apiModule.upload('/api/conversations/1/messages/', new FormData(), {
      timeout: 64000,
      onProgress: (event) => progress.push(event.percent),
    }),
    (error) => {
      assert.equal(error.status, 500);
      assert.equal(error.isServer, true);
      assert.match(error.message, /server encountered a problem/i);
      assert.doesNotMatch(error.message, /stack trace|private/i);
      return true;
    },
  );

  assert.equal(attempts.length, 1);
  assert.equal(attempts[0].timeout, 64000);
  assert.deepEqual(progress, [99], 'failed uploads must not emit a completion marker');
});
