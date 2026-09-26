/**
 * NEXORA — config.js
 * The single source of truth for runtime configuration on the frontend.
 *
 * Every other module (api.js, websocket.js, sw.js, theme.js) imports from
 * here. Nothing else may hardcode a host, a port or a scheme.
 *
 * Resolution order for the API origin — first match wins:
 *   1. explicit runtime config:  window.NEXORA_RUNTIME = { apiBase: '…' }
 *      (or a `nexora-config` JSON <script>), injected by the deployment
 *   2. meta tag:  <meta name="nexora-api-base" content="https://api.example.org">
 *   3. local-development detection: when the page is served from a known
 *      static-dev-server port (5500, 5501, 8080, 3000, 5173 …) the API is
 *      assumed to be Django on the SAME HOSTNAME at port 8000
 *   4. the deployed backend origin (PRODUCTION_API_ORIGIN below) — this
 *      deployment serves the frontend from Vercel and the API from Render, so
 *      "same origin" would resolve to the static host and every request would
 *      404 (and every WebSocket would fail forever)
 *   5. same origin — used only when the page is already being served by the
 *      backend itself (single reverse proxy / devserver.py layout)
 *
 * Step 3 deliberately preserves the hostname: browsers scope cookies by host
 * and ignore the port, so a page on http://127.0.0.1:5500 must talk to
 * http://127.0.0.1:8000 (not localhost:8000) for the session and CSRF cookies
 * to be sent at all. The same holds for localhost.
 *
 * The WebSocket origin is always derived from the resolved API origin by
 * swapping the scheme (http→ws, https→wss), so TLS can never be mismatched.
 */

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

/** Ports commonly used by static dev servers that are NOT the Django port. */
export const LOCAL_STATIC_PORTS = new Set(['3000', '4173', '5173', '5500', '5501', '8080', '8081', '']);

/** Hostnames that mean "this developer's machine". */
export const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1', '0.0.0.0']);

/** Default port Django/uvicorn listens on in development. */
export const LOCAL_API_PORT = String(RUNTIME.localApiPort || readMeta('nexora-local-api-port') || '8000');


export const PRODUCTION_API_ORIGIN = 'https://nexora-backend-ptsc.onrender.com';

export function isLocalHostname(hostname) {
  return LOCAL_HOSTS.has(String(hostname || '').toLowerCase());
}

/**
 * True when the current page looks like a local static dev server that is
 * NOT itself the Django server.
 */
export function isLocalDevFrontend(location = globalThis.location) {
  if (!location) return false;
  if (location.protocol === 'file:') return true;
  if (!isLocalHostname(location.hostname)) return false;
  const port = String(location.port || '');
  if (port === LOCAL_API_PORT) return false; // already served by Django
  return LOCAL_STATIC_PORTS.has(port);
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
   Resolution
   ============================================================ */

export function resolveApiOrigin(location = globalThis.location) {
  const explicit = trimSlashes(RUNTIME.apiBase || RUNTIME.apiOrigin || readMeta('nexora-api-base'));
  if (explicit) {
    // An explicit "same-origin" marker is how a reverse-proxy deployment opts
    // out of the hosted default below.
    return /^same[-_]?origin$/i.test(explicit) ? '' : explicit;
  }

  if (!location) return PRODUCTION_API_ORIGIN;

  if (isLocalDevFrontend(location)) {
    // The hostname is preserved on purpose: cookies are scoped by host and
    // ignore the port, so a page on http://127.0.0.1:5500 must talk to
    // http://127.0.0.1:8000 — never localhost:8000 — or the session and CSRF
    // cookies are simply not sent.
    const host = location.protocol === 'file:' ? '127.0.0.1' : location.hostname;
    const protocol = location.protocol === 'https:' ? 'https:' : 'http:';
    return `${protocol}//${host}:${LOCAL_API_PORT}`;
  }

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
  REQUEST_TIMEOUT: Number(RUNTIME.requestTimeout) || 20000,
  UPLOAD_TIMEOUT: Number(RUNTIME.uploadTimeout) || 0,
  SOCKET_PATH: RUNTIME.socketPath || readMeta('nexora-ws-path') || '/ws/app/',
  IS_LOCAL_DEV: isLocalDevFrontend(),
  /**
   * Verbose transport diagnostics (WebSocket URL, close codes, state changes).
   * On by default in local development only; a hosted deployment can turn it
   * on deliberately and temporarily with
   *   window.NEXORA_RUNTIME = { debug: true }
   * Diagnostics never include cookies, tokens, PINs or message content.
   */
  DEBUG: RUNTIME.debug === true || isLocalDevFrontend(),
});

export default config;
