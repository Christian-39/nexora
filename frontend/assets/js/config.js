/**
 * NEXORA — the single browser-side configuration authority.
 *
 * Static hosting cannot read .env files in the browser. `build.mjs` injects
 * the public API_BASE_URL into each page's `nexora-config` JSON block at build
 * time; this module is the only place that reads that generated value. An empty value deliberately
 * means same-origin (for deployments using a reverse proxy), never a guessed
 * production or loopback URL.
 */

function readInlineConfig() {
  if (typeof document === 'undefined') return {};
  const node = document.getElementById('nexora-config');
  if (!node) return {};
  try {
    const parsed = JSON.parse(node.textContent || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

const RUNTIME = {
  ...readInlineConfig(),
  ...(globalThis.NEXORA_RUNTIME && typeof globalThis.NEXORA_RUNTIME === 'object'
    ? globalThis.NEXORA_RUNTIME
    : {}),
};

const trimSlashes = (value) => String(value || '').trim().replace(/\/+$/, '');

function normalizeApiBase(value) {
  const raw = trimSlashes(value);
  if (!raw || /^same[-_]?origin$/i.test(raw)) return '';

  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new TypeError('Invalid API_BASE_URL: expected an absolute HTTP(S) origin or an empty value.');
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new TypeError('Invalid API_BASE_URL: only credential-free HTTP(S) origins are allowed.');
  }
  if (url.pathname !== '/' || url.search || url.hash) {
    throw new TypeError('Invalid API_BASE_URL: provide the API origin only, without a path, query, or fragment.');
  }
  return url.origin;
}

/** Swap an HTTP(S) origin to its WS(S) equivalent without changing TLS. */
export function toWebSocketOrigin(httpOrigin) {
  const origin = trimSlashes(httpOrigin);
  if (origin.startsWith('https://')) return `wss://${origin.slice(8)}`;
  if (origin.startsWith('http://')) return `ws://${origin.slice(7)}`;
  if (origin.startsWith('wss://') || origin.startsWith('ws://')) return origin;
  return origin;
}

/** Swap a WS(S) origin back to HTTP(S). */
export function toHttpOrigin(wsOrigin) {
  const origin = trimSlashes(wsOrigin);
  if (origin.startsWith('wss://')) return `https://${origin.slice(6)}`;
  if (origin.startsWith('ws://')) return `http://${origin.slice(5)}`;
  return origin;
}

/** Resolve only deployment-injected configuration; the page hostname is not a backend hint. */
export function resolveApiOrigin(_location = globalThis.location, runtimeOverride = null) {
  const runtime = runtimeOverride && typeof runtimeOverride === 'object' ? runtimeOverride : RUNTIME;
  return normalizeApiBase(runtime.API_BASE_URL);
}

const API_ORIGIN = resolveApiOrigin();
const API_PREFIX = (() => {
  const raw = trimSlashes(RUNTIME.API_PREFIX || '/api');
  return raw ? (raw.startsWith('/') ? raw : `/${raw}`) : '';
})();
const PAGE_ORIGIN = globalThis.location?.origin || '';
const WS_ORIGIN = trimSlashes(
  RUNTIME.WS_BASE_URL || RUNTIME.WS_ORIGIN || toWebSocketOrigin(API_ORIGIN || PAGE_ORIGIN),
);

/** Whether a URL targets the authenticated API media route (not object storage). */
export function isApiMediaUrl(value) {
  if (!value) return false;
  try {
    const url = new URL(String(value), PAGE_ORIGIN || undefined);
    const expectedOrigin = API_ORIGIN || PAGE_ORIGIN;
    const protectedAsset = url.pathname.startsWith('/api/media/') || /^\/api\/members\/[^/]+\/avatar\/?$/.test(url.pathname);
    return !!expectedOrigin && url.origin === expectedOrigin && protectedAsset;
  } catch {
    return false;
  }
}

/** Set credentialed CORS mode for cross-origin, cookie-authorized API media. */
export function configureApiMediaElement(element, value) {
  if (element && isApiMediaUrl(value)) element.crossOrigin = 'use-credentials';
  return element;
}

/**
 * Build a canonical API URL. Callers may provide `/api/x/`, `api/x/`, or `x/`.
 * When API_BASE_URL is empty, URLs remain root-relative for a same-origin proxy.
 */
export function buildApiUrl(path) {
  const raw = String(path || '');
  if (/^https?:\/\//i.test(raw)) return raw;

  let suffix = raw.startsWith('/') ? raw : `/${raw}`;
  if (API_PREFIX && !suffix.startsWith(`${API_PREFIX}/`) && suffix !== API_PREFIX) {
    if (!suffix.startsWith('/api/')) suffix = `${API_PREFIX}${suffix}`;
  }
  return `${API_ORIGIN}${suffix}`;
}

/** Absolute (or root-relative) URL for a WebSocket endpoint. */
export function buildSocketUrl(path = '/ws/app/') {
  const suffix = String(path || '/ws/app/');
  return `${WS_ORIGIN}${suffix.startsWith('/') ? suffix : `/${suffix}`}`;
}

/** Resolve a backend-returned media URL against the configured API origin. */
export function resolveMediaUrl(value) {
  if (!value) return null;
  const raw = String(value);
  if (/^(https?:|blob:|data:)/i.test(raw)) return raw;
  return `${API_ORIGIN}${raw.startsWith('/') ? raw : `/${raw}`}`;
}

export const config = Object.freeze({
  /** Empty means same-origin; the API origin is public deployment configuration, not a secret. */
  API_BASE_URL: API_ORIGIN,
  API_ORIGIN,
  API_PREFIX,
  WS_ORIGIN,
  AUTH_MODE: String(RUNTIME.AUTH_MODE || 'cookie').toLowerCase(),
  CSRF_COOKIE: RUNTIME.CSRF_COOKIE || 'csrftoken',
  CSRF_HEADER: RUNTIME.CSRF_HEADER || 'X-CSRFToken',
  REQUEST_TIMEOUT: Number(RUNTIME.REQUEST_TIMEOUT) || 15000,
  CONNECT_TIMEOUT: Number(RUNTIME.CONNECT_TIMEOUT_MS) || 15000,
  UPLOAD_TIMEOUT: Number(RUNTIME.UPLOAD_TIMEOUT) || 0,
  SOCKET_PATH: RUNTIME.SOCKET_PATH || '/ws/app/',
  IS_LOCAL_DEV: false,
  DEBUG: RUNTIME.DEBUG === true,
});

export default config;
