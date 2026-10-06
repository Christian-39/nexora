import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFile, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const FRONTEND = fileURLToPath(new URL('..', import.meta.url));

function runBuild(apiBaseUrl) {
  const env = { ...process.env, VERCEL: '1' };
  // An explicit blank beats any developer-local .env file, keeping these
  // deployment checks deterministic on machines that have a real API origin.
  env.API_BASE_URL = apiBaseUrl ?? '';
  return spawnSync('node', ['build.mjs'], {
    cwd: FRONTEND,
    env,
    encoding: 'utf8',
  });
}

test('Vercel build injects API_BASE_URL into every page and versions the service worker', async () => {
  const result = runBuild('https://api.example.test');
  assert.equal(result.status, 0, result.stderr || result.stdout);

  const files = await readdir(`${FRONTEND}/dist`);
  const pages = files.filter((name) => name.endsWith('.html'));
  assert.equal(pages.length, 12);
  for (const name of pages) {
    const html = await readFile(`${FRONTEND}/dist/${name}`, 'utf8');
    assert.equal(
      (html.match(/<script id="nexora-config" type="application\/json">\{"API_BASE_URL":"https:\/\/api\.example\.test"\}<\/script>/g) || []).length,
      1,
      `${name} has exactly one injected public config block`,
    );
  }
  const serviceWorker = await readFile(`${FRONTEND}/dist/sw.js`, 'utf8');
  assert.match(serviceWorker, /const VERSION = 'build-[a-f0-9]{12}';/);
});

test('Vercel refuses missing, loopback, and insecure API origins but accepts explicit same-origin proxy mode', async () => {
  const missing = runBuild(undefined);
  assert.notEqual(missing.status, 0);
  assert.match(missing.stderr, /requires API_BASE_URL/);

  const loopback = runBuild('http://127.0.0.1:8000');
  assert.notEqual(loopback.status, 0);
  assert.match(loopback.stderr, /cannot use a loopback/);

  const localhostSubdomain = runBuild('https://api.localhost');
  assert.notEqual(localhostSubdomain.status, 0);
  assert.match(localhostSubdomain.stderr, /cannot use a loopback/);

  const insecure = runBuild('http://api.example.test');
  assert.notEqual(insecure.status, 0);
  assert.match(insecure.stderr, /requires HTTPS/);

  const sameOrigin = runBuild('same-origin');
  assert.equal(sameOrigin.status, 0, sameOrigin.stderr || sameOrigin.stdout);
  const html = await readFile(`${FRONTEND}/dist/index.html`, 'utf8');
  assert.match(html, /<script id="nexora-config" type="application\/json">\{"API_BASE_URL":""\}<\/script>/);
});
