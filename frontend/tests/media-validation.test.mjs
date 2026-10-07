/** Browser MIME parameters and protected cross-origin media URL behavior. */

import assert from 'node:assert/strict';
import test from 'node:test';
import { setNavigator } from './helpers/browser-env.mjs';

const MEDIA_MODULE = new URL('../assets/js/media.js', import.meta.url).href;
const API_ORIGIN = 'https://api.example.test';

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
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener() {},
    removeEventListener() {},
  };
  globalThis.document = {
    cookie: '',
    documentElement: { dataset: {} },
    getElementById: () => null,
    querySelector: () => ({ setAttribute() {} }),
  };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
  setNavigator({ onLine: true });
  globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: API_ORIGIN };
  globalThis.fetch = fetchImpl;
}

function jsonResponse(data) {
  return new Response(JSON.stringify({ success: true, message: '', data }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

test('video MIME values with codec parameters are checked by MIME essence', async () => {
  installEnvironment(async () => { throw new Error('unexpected network request'); });
  const media = await import(`${MEDIA_MODULE}?validation-case=codec-essence`);

  assert.deepEqual(
    media.validateFile({ type: 'video/webm;codecs=vp9,opus', size: 1024 }, 'video'),
    { ok: true, kind: 'video' },
  );
  assert.deepEqual(
    media.validateFile({ type: 'audio/mp4;codecs=mp4a.40.2', size: 1024 }, 'voice'),
    { ok: true, kind: 'voice' },
  );
  assert.equal(
    media.validateFile({ type: 'video/x-matroska', size: 1024 }, 'video').ok,
    false,
  );
});

test('media resolution returns a signed storage URL the browser uses directly', async () => {
  const calls = [];
  installEnvironment(async (url, init = {}) => {
    const requestUrl = new URL(String(url));
    calls.push({ requestUrl, init });
    assert.equal(requestUrl.pathname, '/api/media/attachment-77/url/');
    return jsonResponse({
      url: `${API_ORIGIN}/api/media/attachment-77/`,
      expires_at: new Date(Date.now() + 5 * 60 * 1000).toISOString(),
    });
  });
  const media = await import(`${MEDIA_MODULE}?validation-case=protected-media`);
  const item = { id: 'attachment-77', url: '/api/media/attachment-77/' };

  const [first, second] = await Promise.all([
    media.getMediaUrl(item, 'full'),
    media.getMediaUrl(item, 'full'),
  ]);
  assert.equal(first, `${API_ORIGIN}/api/media/attachment-77/`);
  assert.equal(second, first);
  // The URL is whatever the backend returned; it is an authenticated route
  // path that the browser can hit with credentialed CORS. A real signed
  // storage URL would point at the object store; either way the frontend
  // must NEVER construct its own host.
  assert.equal(calls.length, 1, 'parallel renders share one authorization request');
  assert.equal(calls[0].requestUrl.origin, API_ORIGIN);
  assert.equal(calls[0].init.credentials, 'include');
  assert.equal(calls[0].init.cache, 'no-store');
});
