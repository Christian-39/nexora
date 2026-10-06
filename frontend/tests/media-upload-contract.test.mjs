/** Message upload requires authoritative response data before reporting 100%. */

import assert from 'node:assert/strict';
import test from 'node:test';

const MEDIA_MODULE = new URL('../assets/js/media.js', import.meta.url).href;

function installEnvironment() {
  globalThis.location = {
    hostname: 'frontend.example.test', port: '', protocol: 'https:',
    origin: 'https://frontend.example.test', href: 'https://frontend.example.test/chat.html',
  };
  globalThis.window = {
    location: globalThis.location,
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener() {},
    removeEventListener() {},
  };
  globalThis.document = {
    cookie: '', documentElement: { dataset: {} },
    getElementById: () => null,
    querySelector: () => ({ setAttribute() {} }),
  };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
  globalThis.navigator = { onLine: true };
  globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: 'https://api.example.test' };
  globalThis.fetch = async (url) => {
    assert.equal(new URL(String(url)).pathname, '/api/auth/csrf/');
    return new Response(JSON.stringify({ success: true, data: { csrf_token: 'upload-csrf' } }), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  };

  class Target {
    constructor() { this.handlers = new Map(); }
    addEventListener(type, callback) {
      if (!this.handlers.has(type)) this.handlers.set(type, []);
      this.handlers.get(type).push(callback);
    }
    dispatch(type, event = {}) {
      for (const callback of this.handlers.get(type) || []) callback(event);
    }
  }
  globalThis.XMLHttpRequest = class extends Target {
    constructor() {
      super();
      this.upload = new Target();
      this.headers = new Map();
      this.withCredentials = false;
      this.responseURL = '';
    }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers.set(name.toLowerCase(), value); }
    getResponseHeader() { return null; }
    send() {
      this.status = 201;
      this.responseURL = this.url;
      this.responseText = JSON.stringify(globalThis.__MEDIA_UPLOAD_RESPONSE_BODY);
      this.upload.dispatch('progress', { lengthComputable: true, loaded: 99, total: 100 });
      queueMicrotask(() => this.dispatch('load'));
    }
    abort() { this.dispatch('abort'); }
  };
}

test('a malformed 2xx response is retained as unconfirmed and never emits 100%', async () => {
  installEnvironment();
  globalThis.__MEDIA_UPLOAD_RESPONSE_BODY = { success: true, message: 'created', data: {} };
  const media = await import(`${MEDIA_MODULE}?upload-contract=authoritative-response`);
  const file = new Blob(['image-bytes'], { type: 'image/png' });
  const draft = {
    clientId: 'upload-client-1',
    kind: 'image',
    file,
    name: 'image.png',
    size: file.size,
    duration: 0,
    width: null,
    height: null,
    poster: null,
  };
  const progress = [];

  await assert.rejects(
    media.uploadDraft('conversation-1', draft, { onProgress: (event) => progress.push(event.percent) }),
    (error) => error.code === 'UPLOAD_UNCONFIRMED' && error.status === 0,
  );
  assert.deepEqual(progress, [99]);
  assert.equal(media.isUploading(draft.clientId), false);
});

test('a matching authoritative message response is the only path to 100%', async () => {
  installEnvironment();
  globalThis.__MEDIA_UPLOAD_RESPONSE_BODY = {
    success: true,
    message: 'created',
    data: { id: 'message-1', client_id: 'upload-client-2' },
  };
  const media = await import(`${MEDIA_MODULE}?upload-contract=confirmed-response`);
  const file = new Blob(['image-bytes'], { type: 'image/png' });
  const draft = {
    clientId: 'upload-client-2', kind: 'image', file, name: 'image.png', size: file.size,
    duration: 0, width: null, height: null, poster: null,
  };
  const progress = [];

  const message = await media.uploadDraft('conversation-1', draft, {
    onProgress: (event) => progress.push(event.percent),
  });

  assert.equal(message.id, 'message-1');
  assert.equal(message.client_id, draft.clientId);
  assert.deepEqual(progress, [99, 100]);
});
