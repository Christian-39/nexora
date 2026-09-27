/**
 * NEXORA — frontend configuration tests.
 *
 *   node --test frontend/tests/
 *
 * No test framework, no npm dependency: the frontend stays a static,
 * dependency-free ES-module project. Each case re-imports config.js with a
 * different simulated `location`, because the module resolves the API origin
 * once at import time (a cache-busting query gives us a fresh module).
 */

import assert from 'node:assert/strict';
import test from 'node:test';

const MODULE = new URL('../assets/js/config.js', import.meta.url).href;

const PRODUCTION_API = 'https://nexora-f397.onrender.com';
const PRODUCTION_FRONTEND = 'https://nexora-eight-lilac.vercel.app';

let counter = 0;

/** Import a pristine copy of config.js for the given page location. */
async function loadConfig({ hostname, port = '', protocol = 'https:', runtime = null } = {}) {
  const origin = port ? `${protocol}//${hostname}:${port}` : `${protocol}//${hostname}`;
  globalThis.location = { hostname, port, protocol, origin, href: `${origin}/chat.html` };
  globalThis.window = globalThis;
  if (runtime) globalThis.NEXORA_RUNTIME = runtime;
  else delete globalThis.NEXORA_RUNTIME;
  // config.js guards every `document` access, so leaving it undefined
  // exercises the no-meta-tag path used by the deployed pages.
  delete globalThis.document;

  const module = await import(`${MODULE}?case=${counter++}`);
  return module;
}

test('production: REST requests go to the Render backend, not the Vercel host', async () => {
  const { config, buildApiUrl } = await loadConfig({ hostname: 'nexora-eight-lilac.vercel.app' });
  assert.equal(config.API_ORIGIN, PRODUCTION_API);
  assert.equal(buildApiUrl('/api/me/'), `${PRODUCTION_API}/api/me/`);
  assert.ok(!buildApiUrl('/api/me/').startsWith(PRODUCTION_FRONTEND));
});

test('production: the WebSocket resolves to wss on the Render backend', async () => {
  const { config, buildSocketUrl } = await loadConfig({ hostname: 'nexora-eight-lilac.vercel.app' });
  assert.equal(config.WS_ORIGIN, 'wss://nexora-f397.onrender.com');
  assert.equal(buildSocketUrl('/ws/app/'), 'wss://nexora-f397.onrender.com/ws/app/');
  assert.ok(!buildSocketUrl('/ws/app/').includes('vercel.app'));
});

test('local: 127.0.0.1:5500 talks to 127.0.0.1:8000 over http/ws', async () => {
  const { config, buildApiUrl, buildSocketUrl } = await loadConfig({
    hostname: '127.0.0.1',
    port: '5500',
    protocol: 'http:',
  });
  assert.equal(config.API_ORIGIN, 'http://127.0.0.1:8000');
  assert.equal(buildApiUrl('/api/me/'), 'http://127.0.0.1:8000/api/me/');
  assert.equal(buildSocketUrl('/ws/app/'), 'ws://127.0.0.1:8000/ws/app/');
});

test('local: the hostname is never rewritten (cookies are scoped by host)', async () => {
  const { config } = await loadConfig({ hostname: 'localhost', port: '5500', protocol: 'http:' });
  assert.equal(config.API_ORIGIN, 'http://localhost:8000');
  assert.ok(!config.API_ORIGIN.includes('127.0.0.1'));
});

test('an explicit runtime override always wins', async () => {
  const { config } = await loadConfig({
    hostname: 'nexora-eight-lilac.vercel.app',
    runtime: { apiBase: 'https://staging-api.example.org' },
  });
  assert.equal(config.API_ORIGIN, 'https://staging-api.example.org');
  assert.equal(config.WS_ORIGIN, 'wss://staging-api.example.org');
});

test('a same-origin deployment can opt out of the hosted default', async () => {
  const { config, buildApiUrl } = await loadConfig({
    hostname: 'chat.example.org',
    runtime: { apiBase: 'same-origin' },
  });
  assert.equal(config.API_ORIGIN, '');
  assert.equal(buildApiUrl('/api/me/'), '/api/me/');
});

test('a page served BY the backend stays same-origin', async () => {
  const { config } = await loadConfig({ hostname: 'nexora-f397.onrender.com' });
  assert.equal(config.API_ORIGIN, '');
});

test('paths are normalised to exactly one /api prefix', async () => {
  const { buildApiUrl } = await loadConfig({ hostname: 'nexora-eight-lilac.vercel.app' });
  assert.equal(buildApiUrl('/api/conversations/'), `${PRODUCTION_API}/api/conversations/`);
  assert.equal(buildApiUrl('conversations/'), `${PRODUCTION_API}/api/conversations/`);
  assert.equal(buildApiUrl('https://other.example/x'), 'https://other.example/x');
});

test('toWebSocketOrigin never downgrades or upgrades the transport', async () => {
  const { toWebSocketOrigin } = await loadConfig({ hostname: 'nexora-eight-lilac.vercel.app' });
  assert.equal(toWebSocketOrigin('https://a.example'), 'wss://a.example');
  assert.equal(toWebSocketOrigin('http://127.0.0.1:8000'), 'ws://127.0.0.1:8000');
  assert.equal(toWebSocketOrigin('wss://a.example'), 'wss://a.example');
});
