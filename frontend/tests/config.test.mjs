/**
 * Build-injected frontend configuration contract.
 *
 *   node --test frontend/tests/
 *
 * The API origin is supplied by deployment configuration, not inferred from
 * the page host or a development/production fallback.
 */

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const MODULE = new URL('../assets/js/config.js', import.meta.url).href;
const API_ORIGIN = 'https://api.example.test';
let counter = 0;

async function loadConfig({
  origin = 'https://frontend.example.test',
  inline = null,
  runtime = null,
} = {}) {
  const url = new URL(origin);
  globalThis.location = {
    hostname: url.hostname,
    port: url.port,
    protocol: url.protocol,
    origin: url.origin,
    href: `${url.origin}/chat.html`,
  };
  globalThis.window = globalThis;
  if (runtime) globalThis.NEXORA_RUNTIME = runtime;
  else delete globalThis.NEXORA_RUNTIME;
  globalThis.document = {
    getElementById(id) {
      if (id !== 'nexora-config' || inline === null) return null;
      return { textContent: JSON.stringify(inline) };
    },
  };
  return import(`${MODULE}?case=${counter++}`);
}

test('deployment-injected API_BASE_URL drives REST and derives the WebSocket origin', async () => {
  const { config, buildApiUrl, buildSocketUrl } = await loadConfig({ inline: { API_BASE_URL: API_ORIGIN } });
  assert.equal(config.API_BASE_URL, API_ORIGIN);
  assert.equal(buildApiUrl('/api/me/'), `${API_ORIGIN}/api/me/`);
  assert.equal(config.WS_ORIGIN, 'wss://api.example.test');
  assert.equal(buildSocketUrl('/ws/app/'), 'wss://api.example.test/ws/app/');
});

test('a runtime override is read centrally and wins over inline build config', async () => {
  const { config } = await loadConfig({
    inline: { API_BASE_URL: API_ORIGIN },
    runtime: { API_BASE_URL: 'https://staging-api.example.test' },
  });
  assert.equal(config.API_ORIGIN, 'https://staging-api.example.test');
  assert.equal(config.WS_ORIGIN, 'wss://staging-api.example.test');
});

test('missing or empty API_BASE_URL stays same-origin and never infers a backend', async () => {
  for (const origin of ['https://frontend.example.test', 'http://dev.example.test:5500']) {
    const { config, buildApiUrl, buildSocketUrl } = await loadConfig({ origin, inline: { API_BASE_URL: '' } });
    assert.equal(config.API_ORIGIN, '');
    assert.equal(buildApiUrl('/api/me/'), '/api/me/');
    assert.equal(buildSocketUrl('/ws/app/'), `${origin.startsWith('https:') ? 'wss' : 'ws'}://${new URL(origin).host}/ws/app/`);
  }
});

test('an explicitly configured development origin is used as-is', async () => {
  const { config, buildApiUrl, buildSocketUrl } = await loadConfig({
    origin: 'http://frontend.example.test:5500',
    inline: { API_BASE_URL: 'http://api.example.test:8000' },
  });
  assert.equal(config.API_ORIGIN, 'http://api.example.test:8000');
  assert.equal(buildApiUrl('/api/me/'), 'http://api.example.test:8000/api/me/');
  assert.equal(buildSocketUrl('/ws/app/'), 'ws://api.example.test:8000/ws/app/');
});

test('API_BASE_URL rejects credentials, paths, queries and non-HTTP schemes', async () => {
  for (const value of [
    'https://user:pass@api.example.test',
    'https://api.example.test/api',
    'https://api.example.test?secret=value',
    'file:///api',
  ]) {
    await assert.rejects(
      () => loadConfig({ inline: { API_BASE_URL: value } }),
      /Invalid API_BASE_URL/,
    );
  }
});

test('paths normalize to exactly one API prefix; explicit absolute URLs pass through', async () => {
  const { buildApiUrl } = await loadConfig({ inline: { API_BASE_URL: API_ORIGIN } });
  assert.equal(buildApiUrl('/api/conversations/'), `${API_ORIGIN}/api/conversations/`);
  assert.equal(buildApiUrl('conversations/'), `${API_ORIGIN}/api/conversations/`);
  assert.equal(buildApiUrl('https://other.example.test/x'), 'https://other.example.test/x');
});

test('credentialed CORS mode is limited to protected API media and avatar endpoints', async () => {
  const { isApiMediaUrl, configureApiMediaElement } = await loadConfig({ inline: { API_BASE_URL: API_ORIGIN } });
  const mediaUrl = `${API_ORIGIN}/api/media/attachment-1/?variant=thumbnail`;
  const avatarUrl = `${API_ORIGIN}/api/members/member-1/avatar/`;
  assert.equal(isApiMediaUrl(mediaUrl), true);
  assert.equal(isApiMediaUrl(avatarUrl), true);
  assert.equal(isApiMediaUrl('https://storage.example.test/bucket/object?signature=secret'), false);
  assert.equal(isApiMediaUrl('https://other.example.test/api/media/attachment-1/'), false);

  const image = {};
  configureApiMediaElement(image, mediaUrl);
  assert.equal(image.crossOrigin, 'use-credentials');
  const storageImage = {};
  configureApiMediaElement(storageImage, 'https://storage.example.test/bucket/object');
  assert.equal(storageImage.crossOrigin, undefined);
});

test('the runtime resolver contains no deployment host or loopback/port fallback', async () => {
  const source = await readFile(new URL('../assets/js/config.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /onrender\.com|localhost|127\.0\.0\.1|0\.0\.0\.0|::1|LOCAL_API_PORT|PRODUCTION_API_ORIGIN|8000/);
  const env = await readFile(new URL('../.env.example', import.meta.url), 'utf8');
  assert.match(env, /^API_BASE_URL=http:\/\/127\.0\.0\.1:8000$/m);
  assert.match(env, /Vercel: set API_BASE_URL in Project/);
  assert.match(env, /^# API_BASE_URL=same-origin$/m);
});
