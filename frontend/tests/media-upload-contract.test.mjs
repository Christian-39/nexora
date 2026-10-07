/** Message upload requires authoritative response data before reporting 100%. */

import assert from 'node:assert/strict';
import test from 'node:test';
import { setNavigator } from './helpers/browser-env.mjs';

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
  setNavigator({ onLine: true });
  globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: 'https://api.example.test' };
  globalThis.__MEDIA_UPLOAD_OUTCOMES = [];
  globalThis.__MEDIA_UPLOAD_ATTEMPTS = [];
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
      globalThis.__MEDIA_UPLOAD_ATTEMPTS.push(this);
    }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers.set(name.toLowerCase(), value); }
    getResponseHeader() { return null; }
    send(body) {
      this.body = body;
      const outcome = globalThis.__MEDIA_UPLOAD_OUTCOMES.shift() || {};
      this.status = outcome.status ?? 201;
      this.responseURL = this.url;
      this.responseText = JSON.stringify(outcome.payload ?? globalThis.__MEDIA_UPLOAD_RESPONSE_BODY);
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

test('a failed media attempt can retry the same retained File with the same client id', async () => {
  installEnvironment();
  globalThis.__MEDIA_UPLOAD_OUTCOMES = [
    { status: 503, payload: { success: false, message: 'temporary service failure', code: 'HTTP_503' } },
    { status: 201, payload: { success: true, message: 'created', data: { id: 'message-retried', client_id: 'stable-file-client-id' } } },
  ];
  const media = await import(`${MEDIA_MODULE}?upload-contract=same-file-retry`);
  const file = new Blob(['same-file-bytes'], { type: 'image/png' });
  const draft = {
    clientId: 'stable-file-client-id', kind: 'image', file, name: 'same-image.png', size: file.size,
    duration: 0, width: null, height: null, poster: null,
  };

  await assert.rejects(
    media.uploadDraft('conversation-retry', draft),
    (error) => error.status === 503 && error.isServer,
  );
  assert.equal(media.isUploading(draft.clientId), false);

  const message = await media.uploadDraft('conversation-retry', draft);
  const attempts = globalThis.__MEDIA_UPLOAD_ATTEMPTS;
  assert.equal(message.id, 'message-retried');
  assert.equal(attempts.length, 2);
  assert.strictEqual(draft.file, file, 'retry retains the original in-memory File/Blob object');
  for (const attempt of attempts) {
    assert.equal(attempt.body.get('client_id'), draft.clientId);
    assert.equal(attempt.body.get('file').name, draft.name);
    assert.deepEqual(
      [...new Uint8Array(await attempt.body.get('file').arrayBuffer())],
      [...new Uint8Array(await file.arrayBuffer())],
      'each retry sends the same file bytes',
    );
  }
});
