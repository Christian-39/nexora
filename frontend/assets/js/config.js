/**
 * NEXORA — config.js

/* ============================================================
   Sources
   ============================================================ */

function readMeta(name) {
  if (typeof document === 'undefined') return null;
  const node = document.querySelector(`meta[name="${name}"]`);
  const value = node?.getAttribute('content')?.trim();
  return value || null;
}

function readInlineConfig() {
  if (typeof document === 'undefined') return {};
  const node = document.getElementById('nexora-config');
  if (!node) return {};
  try {
    const parsed = JSON.parse(node.textContent || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    // A malformed deployment blob must never break boot; fall through.
    return {};
  }
}

const RUNTIME = { ...readInlineConfig(), ...(globalThis.NEXORA_RUNTIME || {}) };

/* ============================================================
   Helpers
   ============================================================ */

const trimSlashes = (value) => String(value || '').replace(/\/+$/, '');

/** Kept for backward compatibility with any module still importing it; no
 *  longer consulted by isLocalDevFrontend (see J1-style detection below). */
export const LOCAL_STATIC_PORTS = new Set(['3000', '4173', '5173', '5500', '5501', '8080', '8081', '']);

/** Hostnames that mean "this developer's machine" — same set J1 uses,
 *  plus the extra loopback literals Nexora already recognized. */
export const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1', '0.0.0.0', '']);

/** Default port Django/uvicorn listens on in development. */
export const LOCAL_API_PORT = String(RUNTIME.localApiPort || readMeta('nexora-local-api-port') || '8000');

export const PRODUCTION_API_ORIGIN = 'https://nexora-backend-ptsc.onrender.com';
const SANDBOX_PREVIEW_RE = /\.e2b\.app$/;

export function isLocalHostname(hostname) {
  return LOCAL_HOSTS.has(String(hostname || '').toLowerCase());
}

export function isSandboxPreview(hostname) {
  return SANDBOX_PREVIEW_RE.test(String(hostname || ''));
}

export function isLocalDevFrontend(location = globalThis.location) {
  if (!location) return false;
  if (location.protocol === 'file:') return true;
  return isLocalHostname(location.hostname);
}

/** Swap an http(s) origin to its ws(s) equivalent, preserving TLS. */
export function toWebSocketOrigin(httpOrigin) {
  const origin = trimSlashes(httpOrigin);
  if (!origin) return '';
  if (origin.startsWith('https://')) return `wss://${origin.slice(8)}`;
  if (origin.startsWith('http://')) return `ws://${origin.slice(7)}`;
  if (origin.startsWith('wss://') || origin.startsWith('ws://')) return origin;
  return origin;
}

/** Swap a ws(s) origin back to http(s). */
export function toHttpOrigin(wsOrigin) {
  const origin = trimSlashes(wsOrigin);
  if (origin.startsWith('wss://')) return `https://${origin.slice(6)}`;
  if (origin.startsWith('ws://')) return `http://${origin.slice(5)}`;
  return origin;
}

/* ============================================================
   Resolution — J1-style: same machine (dev) vs sandbox preview vs prod
   ============================================================ */

export function resolveApiOrigin(location = globalThis.location) {
  const explicit = trimSlashes(RUNTIME.apiBase || RUNTIME.apiOrigin || readMeta('nexora-api-base'));
  if (explicit) {
    // An explicit "same-origin" marker is how a reverse-proxy deployment opts
    // out of the hosted default below.
    return /^same[-_]?origin$/i.test(explicit) ? '' : explicit;
  }

  if (!location) return PRODUCTION_API_ORIGIN;

  const hostname = location.protocol === 'file:' ? '127.0.0.1' : location.hostname;

  
  if (isLocalDevFrontend(location)) {
    const protocol = location.protocol === 'https:' ? 'https:' : 'http:';
    return `${protocol}//${hostname}:${LOCAL_API_PORT}`;
  }

  // Sandboxed preview host — same-origin, proxied by the dev server.
  if (isSandboxPreview(hostname)) return '';

  // Already served BY the backend (reverse proxy / devserver.py): stay
  // relative — no CORS preflight, no cookie-domain surprises.
  if (location.origin && location.origin === PRODUCTION_API_ORIGIN) return '';

  // Hosted frontend (Vercel) with the API on its own origin (Render).
  return PRODUCTION_API_ORIGIN;
}

const API_ORIGIN = resolveApiOrigin();
const API_PREFIX = (() => {
  const value = trimSlashes(RUNTIME.apiPrefix || readMeta('nexora-api-prefix') || '/api');
  if (!value) return '';
  return value.startsWith('/') ? value : `/${value}`;
})();

const WS_ORIGIN = trimSlashes(
  RUNTIME.wsBase || RUNTIME.wsOrigin || readMeta('nexora-ws-base') || toWebSocketOrigin(API_ORIGIN || (globalThis.location?.origin ?? ''))
);

/**
 * Absolute (or root-relative) URL for an API path.
 * Accepts '/api/me/', 'api/me/' or 'me/' and always produces one canonical form.
 */
export function buildApiUrl(path) {
  const raw = String(path || '');
  if (/^https?:\/\//i.test(raw)) return raw;

  let suffix = raw.startsWith('/') ? raw : `/${raw}`;
  if (API_PREFIX && !suffix.startsWith(`${API_PREFIX}/`) && suffix !== API_PREFIX) {
    // Allow callers to pass either '/api/me/' or '/me/'.
    if (!suffix.startsWith('/api/')) suffix = `${API_PREFIX}${suffix}`;
  }
  return `${API_ORIGIN}${suffix}`;
}

/** Absolute URL for a WebSocket path such as '/ws/app/'. */
export function buildSocketUrl(path) {
  const suffix = String(path || '/ws/app/');
  return `${WS_ORIGIN}${suffix.startsWith('/') ? suffix : `/${suffix}`}`;
}

/**
 * Resolve a media URL returned by the backend. Relative paths are joined to
 * the API origin so media works when the API lives on another origin.
 */
export function resolveMediaUrl(value) {
  if (!value) return null;
  const raw = String(value);
  if (/^(https?:|blob:|data:)/i.test(raw)) return raw;
  return `${API_ORIGIN}${raw.startsWith('/') ? raw : `/${raw}`}`;
}

export const config = Object.freeze({
  /** '' means "same origin". */
  API_ORIGIN,
  API_PREFIX,
  WS_ORIGIN,
  /** 'cookie' (HttpOnly session, preferred) or 'bearer'. */
  AUTH_MODE: String(RUNTIME.authMode || readMeta('nexora-auth-mode') || 'cookie').toLowerCase(),
  CSRF_COOKIE: RUNTIME.csrfCookie || readMeta('nexora-csrf-cookie') || 'csrftoken',
  CSRF_HEADER: RUNTIME.csrfHeader || readMeta('nexora-csrf-header') || 'X-CSRFToken',
  REQUEST_TIMEOUT: Number(RUNTIME.requestTimeout) || 15000,
  /** WebSocket handshake guard: recycle a stuck CONNECTING socket (ms). */
  CONNECT_TIMEOUT: Number(RUNTIME.connectTimeoutMs) || 15000,
  UPLOAD_TIMEOUT: Number(RUNTIME.uploadTimeout) || 0,
  SOCKET_PATH: RUNTIME.socketPath || readMeta('nexora-ws-path') || '/ws/app/',
  IS_LOCAL_DEV: isLocalDevFrontend(),
  
  DEBUG: RUNTIME.debug === true || isLocalDevFrontend(),
});

export default config;