import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const sw = await readFile(new URL('../sw.js', import.meta.url), 'utf8');
const login = await readFile(new URL('../login.html', import.meta.url), 'utf8');
const auth = await readFile(new URL('../assets/js/auth.js', import.meta.url), 'utf8');
const chatCss = await readFile(new URL('../assets/css/chat.css', import.meta.url), 'utf8');
const mainCss = await readFile(new URL('../assets/css/main.css', import.meta.url), 'utf8');
const build = await readFile(new URL('../build.mjs', import.meta.url), 'utf8');
const push = await readFile(new URL('../assets/js/push.js', import.meta.url), 'utf8');


test('installed shell navigation and code are served cache-first', () => {
  assert.match(sw, /request\.mode === 'navigate'[\s\S]*handleNavigation/);
  assert.match(sw, /isCodeAsset[\s\S]*cacheFirstCode/);
  assert.match(sw, /const cached = await cache\.match\(page/);
  assert.doesNotMatch(sw, /event\.respondWith\(networkFirst\(request\)\)/);
});


test('only explicit public config/branding routes can use service-worker cache', () => {
  assert.match(sw, /PUBLIC_CACHEABLE_PATH\s*=\s*\/\^\\\/api\\\/public\\\//);
  assert.match(sw, /staleWhileRevalidatePublic\(request, event\)/);
  assert.match(sw, /request\.cache === 'reload' \|\| request\.cache === 'no-store'/);
  assert.match(sw, /response\.type === 'basic' && isPublic && !isPrivate/);
  assert.match(sw, /\['cookie', 'authorization'\]/);
});

test('all other API traffic remains network-only and outside Cache Storage', () => {
  assert.match(sw, /PRIVATE_PATH[\s\S]*networkOnlyApi/);
  const networkOnly = sw.split('async function networkOnlyApi', 2)[1].split('async function handleNavigation', 1)[0];
  assert.doesNotMatch(networkOnly, /caches?\.(?:open|put|match)/);
});


test('login installs the shell and sixth PIN digit requests submit', () => {
  assert.match(login, /registerServiceWorker\(\)/);
  assert.match(login, /onComplete:[\s\S]*form\.requestSubmit\(\)/);
  assert.match(login, /if \(loginInFlight\) return/);
});


test('successful login consumes the profile returned by login without mandatory me request', () => {
  assert.match(auth, /payload\?\.id \? payload : null/);
});


test('deployment fingerprints client source and offers an explicit safe service-worker activation path', () => {
  assert.match(build, /async function hashTree/);
  assert.match(build, /sourceHash\.digest\('hex'\)/);
  assert.match(build, /apiBaseUrl/);
  assert.match(push, /updateViaCache:\s*'none'/);
  assert.match(push, /reg\.waiting.*promptUpdate/s);
  assert.match(push, /NEXORA_SKIP_WAITING/);
  assert.match(push, /worker\.postMessage\(\{ type: 'NEXORA_SKIP_WAITING' \}\)/);
  assert.match(push, /controllerchange/);
});


test('chat uses visual viewport sizing and natural message wrapping', () => {
  assert.match(mainCss, /--app-viewport-height, 100dvh/);
  const bubble = chatCss.split('.bubble {', 2)[1].split('}', 1)[0];
  assert.match(bubble, /overflow-wrap: break-word/);
  assert.match(bubble, /word-break: normal/);
  assert.doesNotMatch(bubble, /overflow-wrap: anywhere/);
});
