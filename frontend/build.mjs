import { createHash } from 'node:crypto';
import { cp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('.', import.meta.url));
const output = join(root, 'dist');

function parseEnv(text) {
  const result = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!match || match[1].startsWith('#')) continue;
    let value = match[2];
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    } else {
      value = value.replace(/\s+#.*$/, '').trim();
    }
    result[match[1]] = value;
  }
  return result;
}

async function readLocalEnv() {
  try {
    return parseEnv(await readFile(join(root, '.env'), 'utf8'));
  } catch {
    return {};
  }
}

function isLoopbackHost(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '');
  return host === 'localhost' || host === '::1' || host === '0.0.0.0' || /^127(?:\.\d{1,3}){3}$/.test(host);
}

function normalizePublicOrigin(rawValue) {
  const raw = String(rawValue || '').trim();
  if (!raw || /^same[-_]?origin$/i.test(raw)) return '';

  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error('API_BASE_URL must be an absolute HTTP(S) origin or empty for same-origin proxy deployments.');
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('API_BASE_URL must be a credential-free HTTP(S) origin.');
  }
  if (url.pathname !== '/' || url.search || url.hash) {
    throw new Error('API_BASE_URL must contain only the origin (no path, query, or fragment).');
  }
  if (process.env.VERCEL === '1' && isLoopbackHost(url.hostname)) {
    throw new Error('A Vercel production build cannot use a loopback API_BASE_URL.');
  }
  return url.origin;
}

const localEnv = await readLocalEnv();
const injectedValue = process.env.API_BASE_URL ?? localEnv.API_BASE_URL ?? '';
const explicitlySameOrigin = /^same[-_]?origin$/i.test(String(injectedValue).trim());
const apiBaseUrl = normalizePublicOrigin(injectedValue);
if (process.env.VERCEL === '1' && !apiBaseUrl && !explicitlySameOrigin) {
  throw new Error('Vercel deployment requires API_BASE_URL (an HTTP(S) origin, or explicit same-origin for a reverse proxy).');
}

// Include the API origin in the build identity so an environment-only change
// still installs a coherent new PWA cache containing the matching runtime config.
const commit = String(process.env.VERCEL_GIT_COMMIT_SHA || process.env.GITHUB_SHA || 'local').slice(0, 40);
const buildId = createHash('sha256').update(`${commit}\n${apiBaseUrl}`).digest('hex').slice(0, 12);

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });

const excludedTopLevel = new Set(['.git', '.vercel', 'dist', 'node_modules', 'tests']);
const excludedFiles = new Set(['.env.example', 'README.md', 'build.mjs', 'package.json', 'vercel.json']);
const topLevelEntries = await readdir(root, { withFileTypes: true });
const copyOptions = {
  recursive: true,
  filter(source) {
    const rel = relative(root, source).split(sep).join('/');
    if (!rel) return true;
    const [first, ...parts] = rel.split('/');
    if (excludedTopLevel.has(first) || first.startsWith('.env')) return false;
    if (parts.length === 0 && excludedFiles.has(first)) return false;
    return true;
  },
};
await Promise.all(topLevelEntries
  .filter((entry) => !excludedTopLevel.has(entry.name) && !excludedFiles.has(entry.name) && !entry.name.startsWith('.env'))
  .map((entry) => cp(join(root, entry.name), join(output, entry.name), copyOptions)));

const injectedConfig = JSON.stringify({ API_BASE_URL: apiBaseUrl });
for (const name of [
  'index.html', 'login.html', 'chat.html', 'groups.html', 'members.html',
  'settings.html', 'profile.html', 'admin.html', '403.html', '404.html',
  '500.html', 'offline.html',
]) {
  const htmlPath = join(output, name);
  const html = await readFile(htmlPath, 'utf8');
  const placeholder = /(<script\s+id=["']nexora-config["']\s+type=["']application\/json["']>)[\s\S]*?(<\/script>)/i;
  if (!placeholder.test(html)) {
    throw new Error(`Missing nexora-config injection point in ${name}.`);
  }
  await writeFile(htmlPath, html.replace(placeholder, `$1${injectedConfig}$2`), 'utf8');
}

const serviceWorkerPath = join(output, 'sw.js');
const serviceWorker = await readFile(serviceWorkerPath, 'utf8');
const versionedServiceWorker = serviceWorker.replace(
  /const VERSION = '[^']+';/,
  `const VERSION = 'build-${buildId}';`,
);
if (versionedServiceWorker === serviceWorker) {
  throw new Error('Could not inject the build identity into sw.js.');
}
await writeFile(serviceWorkerPath, versionedServiceWorker, 'utf8');

console.log(`Frontend build complete (build-${buildId}; API_BASE_URL ${apiBaseUrl ? 'configured' : 'same-origin'}).`);
