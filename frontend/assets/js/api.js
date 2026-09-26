/**
 * NEXORA — api.js
 * The ONLY place HTTP talks to the backend. No raw fetch() anywhere else.
 *
 * Responsibilities:
 *  - base URL resolution + runtime configuration
 *  - credentialed session (HttpOnly cookies) OR bearer token, per backend contract
 *  - CSRF header injection for unsafe methods
 *  - timeouts via AbortController, caller-supplied signal composition
 *  - JSON + FormData bodies, upload progress (XHR) with cancellation
 *  - normalized success/error envelopes and ApiError
 *  - safe retry (idempotent methods / 429 / 502-504) with backoff
 *  - offline detection and network error classification
 *  - global events: 'unauthorized', 'forbidden', 'offline', 'online', 'ratelimit'
 */

import { Emitter, backoffDelay, getCookie, isSafeHttpUrl, sleep } from './utils.js';
import { config as runtimeConfig, resolveMediaUrl as resolveMedia, toWebSocketOrigin } from './config.js';

/* ============================================================
   Runtime configuration
   Resolved once, centrally, in config.js — never re-derived here.
   ============================================================ */

const API_ORIGIN = runtimeConfig.API_ORIGIN;
const API_PREFIX = runtimeConfig.API_PREFIX;

/** Auth transport: 'cookie' (HttpOnly session — preferred) or 'bearer'. */
const AUTH_MODE = runtimeConfig.AUTH_MODE;

const DEFAULTS = {
  timeout: runtimeConfig.REQUEST_TIMEOUT,
  uploadTimeout: runtimeConfig.UPLOAD_TIMEOUT, // 0 = no client timeout for uploads
  retries: 2,
  csrfCookie: runtimeConfig.CSRF_COOKIE,
  csrfHeader: runtimeConfig.CSRF_HEADER,
};

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
const RETRY_STATUSES = new Set([408, 425, 429, 502, 503, 504]);

export const apiEvents = new Emitter();

/* ============================================================
   In-memory access token (bearer mode only)
   Deliberately NOT persisted: no localStorage, no sessionStorage.
   A page reload re-establishes the session via the refresh cookie.
   ============================================================ */

let accessToken = null;
let refreshPromise = null;

export const tokenStore = {
  get: () => accessToken,
  set(value) {
    accessToken = value || null;
  },
  clear() {
    accessToken = null;
  },
  get isBearerMode() {
    return AUTH_MODE === 'bearer';
  },
};

/* ============================================================
   Errors
   ============================================================ */

export class ApiError extends Error {
  /**
   * @param {object} init
   * @param {string} init.message  user-safe message
   * @param {number} init.status   HTTP status (0 for transport failures)
   * @param {string} init.code     machine code, e.g. PERMISSION_DENIED
   * @param {object} init.errors   field errors keyed by field name
   */
  constructor({ message, status = 0, code = 'ERROR', errors = {}, retryAfter = null, cause = null }) {
    super(message || 'Something went wrong.');
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.errors = errors || {};
    this.retryAfter = retryAfter;
    if (cause) this.cause = cause;
  }

  get isNetwork() { return this.status === 0 && this.code !== 'TIMEOUT' && this.code !== 'ABORTED'; }
  get isTimeout() { return this.code === 'TIMEOUT'; }
  get isAborted() { return this.code === 'ABORTED'; }
  get isOffline() { return this.code === 'OFFLINE'; }
  get isAuth() { return this.status === 401; }
  get isForbidden() { return this.status === 403; }
  get isNotFound() { return this.status === 404; }
  get isConflict() { return this.status === 409; }
  get isValidation() { return this.status === 400 || this.status === 422; }
  get isRateLimited() { return this.status === 429; }
  get isServer() { return this.status >= 500; }

  /** First field error, useful for inline form display. */
  firstFieldError() {
    for (const [field, value] of Object.entries(this.errors)) {
      const text = Array.isArray(value) ? value[0] : value;
      if (text) return { field, message: String(text) };
    }
    return null;
  }
}

/** Human-readable fallbacks. Never leaks backend internals or stack traces. */
const STATUS_MESSAGES = {
  400: 'Some of the information provided is not valid.',
  401: 'Your session has ended. Please sign in again.',
  403: 'You are not authorized to perform this action.',
  404: 'That item could not be found.',
  409: 'This action conflicts with the current state. Refresh and try again.',
  413: 'That file is larger than the allowed limit.',
  415: 'That file type is not supported.',
  422: 'Some of the information provided is not valid.',
  429: 'Too many attempts. Please wait a moment and try again.',
  500: 'The server encountered a problem. Please try again shortly.',
  502: 'The service is temporarily unreachable. Please try again shortly.',
  503: 'The service is temporarily unavailable. Please try again shortly.',
  504: 'The server took too long to respond. Please try again.',
};

function messageForStatus(status) {
  return STATUS_MESSAGES[status] || 'Something went wrong. Please try again.';
}

/* ============================================================
   URL building
   ============================================================ */

function buildUrl(path, params) {
  let url;
  if (/^https?:\/\//i.test(path)) {
    url = new URL(path);
  } else {
    const prefixed = path.startsWith('/api/') || path.startsWith(`${API_PREFIX}/`)
      ? path
      : `${API_PREFIX}/${path.replace(/^\/+/, '')}`;
    url = new URL(`${API_ORIGIN}${prefixed}`, window.location.origin);
  }
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue;
      if (Array.isArray(value)) value.forEach((v) => url.searchParams.append(key, String(v)));
      else url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/** Absolute URL for a backend-provided (possibly relative) media path. */
export function resolveMediaUrl(value) {
  if (!value) return null;
  if (/^https?:\/\//i.test(value)) return isSafeHttpUrl(value) ? value : null;
  if (value.startsWith('blob:') || value.startsWith('data:')) return value;
  try {
    return resolveMedia(value) || new URL(value, `${API_ORIGIN || window.location.origin}/`).toString();
  } catch {
    return null;
  }
}

/* ============================================================
   Response normalization
   ============================================================ */

/**
 * Accepts either the documented envelope
 *   { success, message, data }  /  { success, message, code, errors }
 * or a plain DRF payload, and always returns the useful body.
 */
function unwrap(payload) {
  if (payload && typeof payload === 'object' && !Array.isArray(payload) && 'success' in payload) {
    return payload.data !== undefined ? payload.data : null;
  }
  return payload;
}

function extractErrors(payload) {
  if (!payload || typeof payload !== 'object') return {};
  if (payload.errors && typeof payload.errors === 'object') return payload.errors;
  // DRF field errors arrive at the top level.
  const reserved = new Set(['detail', 'success', 'message', 'code', 'data', 'non_field_errors']);
  const out = {};
  for (const [key, value] of Object.entries(payload)) {
    if (reserved.has(key)) continue;
    if (Array.isArray(value) || typeof value === 'string') out[key] = value;
  }
  if (payload.non_field_errors) out.__all__ = payload.non_field_errors;
  return out;
}

function extractMessage(payload, status) {
  if (payload && typeof payload === 'object') {
    if (typeof payload.message === 'string' && payload.message) return payload.message;
    if (typeof payload.detail === 'string' && payload.detail) return payload.detail;
    if (Array.isArray(payload.non_field_errors) && payload.non_field_errors[0]) {
      return String(payload.non_field_errors[0]);
    }
  }
  if (typeof payload === 'string' && payload && payload.length < 240 && !payload.includes('<')) {
    return payload;
  }
  return messageForStatus(status);
}

async function parseBody(response) {
  const status = response.status;
  if (status === 204 || status === 205) return null;
  const type = response.headers.get('content-type') || '';
  try {
    if (type.includes('application/json')) return await response.json();
    const text = await response.text();
    if (!text) return null;
    // Some proxies return JSON without the header.
    try { return JSON.parse(text); } catch { return text; }
  } catch {
    return null;
  }
}

function toApiError(status, payload, headers) {
  const retryAfterRaw = headers?.get?.('retry-after');
  const retryAfter = retryAfterRaw ? Number(retryAfterRaw) : null;
  const code =
    (payload && typeof payload === 'object' && typeof payload.code === 'string' && payload.code) ||
    `HTTP_${status}`;
  return new ApiError({
    message: extractMessage(payload, status),
    status,
    code,
    errors: extractErrors(payload),
    retryAfter: Number.isFinite(retryAfter) ? retryAfter : null,
  });
}

/* ============================================================
   Core request
   ============================================================ */

function composeSignal(externalSignal, timeout) {
  const controller = new AbortController();
  let timer = null;
  let timedOut = false;

  if (timeout > 0) {
    timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeout);
  }
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort();
    else externalSignal.addEventListener('abort', () => controller.abort(), { once: true });
  }
  return {
    signal: controller.signal,
    cleanup: () => timer && clearTimeout(timer),
    get timedOut() { return timedOut; },
  };
}

/* ------------------------------------------------------------
   CSRF bootstrap

   Django's CSRF cookie is only issued once a view has been reached with
   `ensure_csrf_cookie`. Cookie-mode clients therefore fetch it once, before
   their first unsafe request, so sign-in itself is CSRF-protected too.
   ------------------------------------------------------------ */

let csrfPromise = null;

export async function ensureCsrfToken() {
  if (AUTH_MODE !== 'cookie') return null;
  const existing = getCookie(DEFAULTS.csrfCookie);
  if (existing) return existing;
  if (!csrfPromise) {
    csrfPromise = fetch(buildUrl('/api/auth/csrf/'), {
      method: 'GET',
      credentials: 'include',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    })
      .then(() => getCookie(DEFAULTS.csrfCookie))
      .catch(() => null)
      .finally(() => {
        // Allow a later retry if the bootstrap failed.
        setTimeout(() => { csrfPromise = null; }, 0);
      });
  }
  return csrfPromise;
}

function authHeaders(method, extra = {}) {
  const headers = { Accept: 'application/json', ...extra };
  if (!SAFE_METHODS.has(method)) {
    const csrf = getCookie(DEFAULTS.csrfCookie);
    if (csrf) headers[DEFAULTS.csrfHeader] = csrf;
  }
  if (AUTH_MODE === 'bearer' && accessToken) {
    headers.Authorization = `Bearer ${accessToken}`;
  }
  headers['X-Requested-With'] = 'XMLHttpRequest';
  return headers;
}

/**
 * Attempt a single session refresh, de-duplicated across concurrent 401s.
 * Returns true when the session was renewed.
 */
async function tryRefreshSession() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const response = await fetch(buildUrl('/api/auth/refresh/'), {
        method: 'POST',
        credentials: 'include',
        headers: authHeaders('POST', { 'Content-Type': 'application/json' }),
        body: '{}',
      });
      if (!response.ok) return false;
      const payload = await parseBody(response);
      const data = unwrap(payload) || {};
      if (AUTH_MODE === 'bearer') {
        const token = data.access || data.access_token || data.token;
        if (!token) return false;
        tokenStore.set(token);
      }
      return true;
    } catch {
      return false;
    } finally {
      setTimeout(() => { refreshPromise = null; }, 0);
    }
  })();
  return refreshPromise;
}

/**
 * @param {string} path
 * @param {object} options
 * @param {'GET'|'POST'|'PUT'|'PATCH'|'DELETE'} [options.method]
 * @param {object} [options.params]      query string
 * @param {object|FormData} [options.body]
 * @param {AbortSignal} [options.signal]
 * @param {number} [options.timeout]
 * @param {number} [options.retries]
 * @param {boolean} [options.auth]       set false for public endpoints
 * @param {boolean} [options.raw]        resolve with { data, meta, response }
 * @param {boolean} [options.allowRefresh]
 */
export async function request(path, options = {}) {
  const {
    method = 'GET',
    params,
    body,
    signal: externalSignal,
    timeout = DEFAULTS.timeout,
    retries = SAFE_METHODS.has(method.toUpperCase()) ? DEFAULTS.retries : 0,
    headers: extraHeaders = {},
    raw = false,
    allowRefresh = true,
  } = options;

  const verb = method.toUpperCase();
  const url = buildUrl(path, params);

  if (typeof navigator !== 'undefined' && navigator.onLine === false) {
    apiEvents.emit('offline');
    throw new ApiError({
      message: "You're offline. Reconnect to continue.",
      code: 'OFFLINE',
      status: 0,
    });
  }

  if (!SAFE_METHODS.has(verb)) await ensureCsrfToken();

  let attempt = 0;
  let refreshed = false;

  for (;;) {
    attempt += 1;
    const { signal, cleanup, ...timer } = composeSignal(externalSignal, timeout);

    const init = {
      method: verb,
      signal,
      credentials: 'include',
      headers: authHeaders(verb, extraHeaders),
      cache: 'no-store',
      mode: 'cors',
    };

    if (body !== undefined && body !== null && !SAFE_METHODS.has(verb)) {
      if (body instanceof FormData || body instanceof Blob) {
        init.body = body; // browser sets the multipart boundary
      } else {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
    }

    let response;
    try {
      response = await fetch(url, init);
    } catch (err) {
      cleanup();
      if (externalSignal?.aborted) {
        throw new ApiError({ message: 'Request cancelled.', code: 'ABORTED', status: 0, cause: err });
      }
      if (timer.timedOut) {
        if (attempt <= retries) {
          await sleep(backoffDelay(attempt));
          continue;
        }
        throw new ApiError({
          message: 'The request timed out. Your connection may be unstable.',
          code: 'TIMEOUT',
          status: 0,
          cause: err,
        });
      }
      if (navigator.onLine === false) {
        apiEvents.emit('offline');
        throw new ApiError({ message: "You're offline. Reconnect to continue.", code: 'OFFLINE', status: 0, cause: err });
      }
      if (attempt <= retries) {
        await sleep(backoffDelay(attempt));
        continue;
      }
      throw new ApiError({
        message: 'Unable to reach the server. Check your connection and try again.',
        code: 'NETWORK_ERROR',
        status: 0,
        cause: err,
      });
    }
    cleanup();

    if (response.ok) {
      const payload = await parseBody(response);
      const data = unwrap(payload);
      if (raw) {
        return {
          data,
          message: payload?.message ?? null,
          meta: payload && typeof payload === 'object' ? payload.meta ?? null : null,
          status: response.status,
          headers: response.headers,
        };
      }
      return data;
    }

    // --- error path ---
    const payload = await parseBody(response);
    const error = toApiError(response.status, payload, response.headers);

    if (response.status === 401 && allowRefresh && !refreshed && !isAuthPath(path)) {
      refreshed = true;
      const ok = await tryRefreshSession();
      if (ok) continue;
      tokenStore.clear();
      apiEvents.emit('unauthorized', error);
      throw error;
    }

    if (response.status === 401) {
      tokenStore.clear();
      if (!isAuthPath(path)) apiEvents.emit('unauthorized', error);
      throw error;
    }

    if (response.status === 403) {
      apiEvents.emit('forbidden', error);
      throw error;
    }

    if (response.status === 429) {
      apiEvents.emit('ratelimit', error);
    }

    const retryable = RETRY_STATUSES.has(response.status) && SAFE_METHODS.has(verb);
    if (retryable && attempt <= retries) {
      const wait = error.retryAfter ? error.retryAfter * 1000 : backoffDelay(attempt);
      await sleep(Math.min(wait, 15000));
      continue;
    }

    throw error;
  }
}

function isAuthPath(path) {
  return /\/api\/auth\/(login|refresh|logout)\/?$/.test(path) || /^\/?auth\/(login|refresh|logout)\/?$/.test(path);
}

/* Convenience verbs */
export const get = (path, params, options = {}) => request(path, { ...options, method: 'GET', params });
export const post = (path, body, options = {}) => request(path, { ...options, method: 'POST', body });
export const put = (path, body, options = {}) => request(path, { ...options, method: 'PUT', body });
export const patch = (path, body, options = {}) => request(path, { ...options, method: 'PATCH', body });
export const del = (path, options = {}) => request(path, { ...options, method: 'DELETE' });

/* ============================================================
   Upload with progress + cancellation (XHR — fetch has no upload progress)
   ============================================================ */

/**
 * @param {string} path
 * @param {FormData} formData
 * @param {object} [options]
 * @param {(info:{loaded:number,total:number,percent:number})=>void} [options.onProgress]
 * @param {AbortSignal} [options.signal]
 * @returns {Promise<any>}
 */
export function upload(path, formData, options = {}) {
  const { onProgress, signal, timeout = DEFAULTS.uploadTimeout, method = 'POST' } = options;

  return ensureCsrfToken().then(() => new Promise((resolve, reject) => {
    if (navigator.onLine === false) {
      reject(new ApiError({ message: "You're offline. The upload will need to be retried.", code: 'OFFLINE', status: 0 }));
      return;
    }

    const xhr = new XMLHttpRequest();
    xhr.open(method, buildUrl(path), true);
    xhr.withCredentials = true;
    xhr.responseType = 'text';
    if (timeout > 0) xhr.timeout = timeout;

    for (const [key, value] of Object.entries(authHeaders(method))) {
      // Content-Type intentionally omitted: XHR sets the multipart boundary.
      xhr.setRequestHeader(key, value);
    }

    const abortHandler = () => xhr.abort();
    if (signal) {
      if (signal.aborted) {
        reject(new ApiError({ message: 'Upload cancelled.', code: 'ABORTED', status: 0 }));
        return;
      }
      signal.addEventListener('abort', abortHandler, { once: true });
    }
    const cleanup = () => signal?.removeEventListener('abort', abortHandler);

    if (onProgress && xhr.upload) {
      xhr.upload.addEventListener('progress', (event) => {
        if (!event.lengthComputable) return;
        onProgress({
          loaded: event.loaded,
          total: event.total,
          percent: Math.min(99, Math.round((event.loaded / event.total) * 100)),
        });
      });
    }

    xhr.addEventListener('load', () => {
      cleanup();
      let payload = null;
      try { payload = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch { payload = xhr.responseText || null; }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.({ loaded: 1, total: 1, percent: 100 });
        resolve(unwrap(payload));
      } else {
        const error = toApiError(xhr.status, payload, {
          get: (h) => xhr.getResponseHeader(h),
        });
        if (xhr.status === 401) { tokenStore.clear(); apiEvents.emit('unauthorized', error); }
        if (xhr.status === 403) apiEvents.emit('forbidden', error);
        reject(error);
      }
    });

    xhr.addEventListener('error', () => {
      cleanup();
      reject(new ApiError({
        message: 'The upload failed because the connection was interrupted.',
        code: 'NETWORK_ERROR',
        status: 0,
      }));
    });

    xhr.addEventListener('timeout', () => {
      cleanup();
      reject(new ApiError({ message: 'The upload timed out.', code: 'TIMEOUT', status: 0 }));
    });

    xhr.addEventListener('abort', () => {
      cleanup();
      reject(new ApiError({ message: 'Upload cancelled.', code: 'ABORTED', status: 0 }));
    });

    xhr.send(formData);
  }));
}

/* ============================================================
   Cursor pagination helper
   ============================================================ */

/**
 * Normalizes DRF cursor/limit-offset pagination and plain arrays into
 * { items, next, previous, count }.
 */
export function normalizePage(payload) {
  if (Array.isArray(payload)) return { items: payload, next: null, previous: null, count: payload.length };
  if (!payload || typeof payload !== 'object') return { items: [], next: null, previous: null, count: 0 };
  const items = payload.results ?? payload.items ?? payload.data ?? [];
  return {
    items: Array.isArray(items) ? items : [],
    next: payload.next ?? payload.next_cursor ?? null,
    previous: payload.previous ?? payload.previous_cursor ?? null,
    count: typeof payload.count === 'number' ? payload.count : (Array.isArray(items) ? items.length : 0),
  };
}

/** Follow a backend-provided absolute "next" URL safely. */
export function fetchPageUrl(url, options = {}) {
  if (!isSafeHttpUrl(url)) throw new ApiError({ message: 'Invalid pagination link.', code: 'BAD_LINK', status: 0 });
  return request(url, { ...options, method: 'GET' });
}

/* ============================================================
   Endpoint surface
   Thin, documented wrappers. Adjust paths here only — never inline.
   ============================================================ */

export const api = {
  /* ---- public (unauthenticated) ---- */
  publicConfig: (options) => request('/api/public/config/', { ...options, auth: false, allowRefresh: false, timeout: 10000 }),

  /* ---- auth ---- */
  auth: {
    login: (identifier, pin, options) =>
      post('/api/auth/login/', { identifier, phone: identifier, pin }, { ...options, allowRefresh: false, retries: 0 }),
    logout: (options) => post('/api/auth/logout/', {}, { ...options, allowRefresh: false, retries: 0 }),
    refresh: (options) => post('/api/auth/refresh/', {}, { ...options, allowRefresh: false, retries: 0 }),
    changePin: (payload, options) => post('/api/auth/change-pin/', payload, { ...options, retries: 0 }),
    sessions: (options) => get('/api/auth/sessions/', null, options),
    revokeSession: (id, options) => del(`/api/auth/sessions/${encodeURIComponent(id)}/`, options),
  },

  /* ---- identity ---- */
  me: {
    get: (options) => get('/api/me/', null, options),
    update: (payload, options) => patch('/api/me/', payload, options),
    updateAvatar: (formData, options) => upload('/api/me/avatar/', formData, options),
    preferences: (options) => get('/api/me/preferences/', null, options),
    savePreferences: (payload, options) => patch('/api/me/preferences/', payload, options),
  },

  /* ---- members (admin) ---- */
  members: {
    list: (params, options) => request('/api/members/', { ...options, method: 'GET', params, raw: true }),
    get: (id, options) => get(`/api/members/${encodeURIComponent(id)}/`, null, options),
    create: (payload, options) => post('/api/members/', payload, options),
    update: (id, payload, options) => patch(`/api/members/${encodeURIComponent(id)}/`, payload, options),
    setActive: (id, isActive, options) =>
      post(`/api/members/${encodeURIComponent(id)}/${isActive ? 'activate' : 'deactivate'}/`, {}, options),
    resetCredential: (id, options) => post(`/api/members/${encodeURIComponent(id)}/reset-pin/`, {}, options),
    activity: (id, params, options) => request(`/api/members/${encodeURIComponent(id)}/activity/`, { ...options, method: 'GET', params, raw: true }),
    conversation: (id, options) => get(`/api/members/${encodeURIComponent(id)}/conversation/`, null, options),
    selectable: (params, options) => request('/api/members/', { ...options, method: 'GET', params: { ...params, selectable: true }, raw: true }),
  },

  /* ---- conversations ---- */
  conversations: {
    list: (params, options) => request('/api/conversations/', { ...options, method: 'GET', params, raw: true }),
    get: (id, options) => get(`/api/conversations/${encodeURIComponent(id)}/`, null, options),
    messages: (id, params, options) =>
      request(`/api/conversations/${encodeURIComponent(id)}/messages/`, { ...options, method: 'GET', params, raw: true }),
    send: (id, payload, options) => post(`/api/conversations/${encodeURIComponent(id)}/messages/`, payload, { ...options, retries: 0 }),
    sendMedia: (id, formData, options) => upload(`/api/conversations/${encodeURIComponent(id)}/messages/`, formData, options),
    markRead: (id, payload, options) => post(`/api/conversations/${encodeURIComponent(id)}/read/`, payload || {}, options),
    typing: (id, isTyping, options) =>
      post(`/api/conversations/${encodeURIComponent(id)}/typing/`, { typing: !!isTyping }, { ...options, retries: 0 }),
    media: (id, params, options) => request(`/api/conversations/${encodeURIComponent(id)}/media/`, { ...options, method: 'GET', params, raw: true }),
    unreadSummary: (options) => get('/api/conversations/unread-summary/', null, options),
  },

  /* ---- messages ---- */
  messages: {
    get: (id, options) => get(`/api/messages/${encodeURIComponent(id)}/`, null, options),
    edit: (id, payload, options) => patch(`/api/messages/${encodeURIComponent(id)}/`, payload, { ...options, retries: 0 }),
    remove: (id, scope, options) =>
      request(`/api/messages/${encodeURIComponent(id)}/`, { ...options, method: 'DELETE', params: { scope } }),
    react: (id, reaction, options) => post(`/api/messages/${encodeURIComponent(id)}/reactions/`, { reaction }, { ...options, retries: 0 }),
    unreact: (id, reaction, options) =>
      request(`/api/messages/${encodeURIComponent(id)}/reactions/`, { ...options, method: 'DELETE', params: { reaction } }),
    status: (ids, options) => post('/api/messages/status/', { ids }, options),
  },

  /* ---- groups ---- */
  groups: {
    list: (params, options) => request('/api/groups/', { ...options, method: 'GET', params, raw: true }),
    get: (id, options) => get(`/api/groups/${encodeURIComponent(id)}/`, null, options),
    create: (payload, options) => post('/api/groups/', payload, options),
    update: (id, payload, options) => patch(`/api/groups/${encodeURIComponent(id)}/`, payload, options),
    updateImage: (id, formData, options) => upload(`/api/groups/${encodeURIComponent(id)}/image/`, formData, options),
    removeImage: (id, options) => del(`/api/groups/${encodeURIComponent(id)}/image/`, options),
    remove: (id, options) => del(`/api/groups/${encodeURIComponent(id)}/`, options),
    archive: (id, archived, options) => post(`/api/groups/${encodeURIComponent(id)}/${archived ? 'archive' : 'unarchive'}/`, {}, options),
    members: (id, params, options) => request(`/api/groups/${encodeURIComponent(id)}/members/`, { ...options, method: 'GET', params, raw: true }),
    addMembers: (id, memberIds, options) =>
      post(`/api/groups/${encodeURIComponent(id)}/members/`, { member_ids: memberIds }, options),
    removeMember: (id, memberId, options) =>
      del(`/api/groups/${encodeURIComponent(id)}/members/${encodeURIComponent(memberId)}/`, options),
    removeMembers: (id, memberIds, options) =>
      request(`/api/groups/${encodeURIComponent(id)}/members/`, { ...options, method: 'DELETE', body: { member_ids: memberIds } }),
    leave: (id, options) => post(`/api/groups/${encodeURIComponent(id)}/leave/`, {}, options),
    activity: (id, params, options) => request(`/api/groups/${encodeURIComponent(id)}/activity/`, { ...options, method: 'GET', params, raw: true }),
  },

  /* ---- media ---- */
  media: {
    get: (id, options) => get(`/api/media/${encodeURIComponent(id)}/`, null, options),
    /** Ask the backend for a fresh authorized/signed URL when one expires. */
    resolve: (id, options) => get(`/api/media/${encodeURIComponent(id)}/url/`, null, options),
    create: (formData, options) => upload('/api/media/', formData, options),
  },

  /* ---- notifications ---- */
  notifications: {
    list: (params, options) => request('/api/notifications/', { ...options, method: 'GET', params, raw: true }),
    unreadCount: (options) => get('/api/notifications/unread-count/', null, options),
    markRead: (ids, options) => post('/api/notifications/read/', ids ? { ids } : {}, options),
    markAllRead: (options) => post('/api/notifications/read/', { all: true }, options),
  },

  /* ---- push ---- */
  push: {
    config: (options) => get('/api/push/', null, options),
    subscribe: (payload, options) => post('/api/push/subscribe/', payload, options),
    unsubscribe: (endpoint, options) => post('/api/push/unsubscribe/', { endpoint }, options),
  },

  /* ---- settings (admin) ---- */
  settings: {
    get: (options) => get('/api/settings/', null, options),
    update: (payload, options) => patch('/api/settings/', payload, options),
    uploadAsset: (kind, formData, options) => upload(`/api/settings/assets/${encodeURIComponent(kind)}/`, formData, options),
    policies: (options) => get('/api/settings/policies/', null, options),
    updatePolicies: (payload, options) => patch('/api/settings/policies/', payload, options),
  },

  /* ---- admin analytics / security ---- */
  dashboard: (params, options) => get('/api/dashboard/', params, options),
  audit: (params, options) => request('/api/audit/', { ...options, method: 'GET', params, raw: true }),
  security: {
    events: (params, options) => request('/api/security/events/', { ...options, method: 'GET', params, raw: true }),
    settings: (options) => get('/api/security/', null, options),
    updateSettings: (payload, options) => patch('/api/security/', payload, options),
  },

  /* ---- search ---- */
  search: (params, options) => get('/api/search/', params, options),
};

/* ============================================================
   Connectivity signalling
   ============================================================ */

window.addEventListener('online', () => apiEvents.emit('online'));
window.addEventListener('offline', () => apiEvents.emit('offline'));

export const apiConfig = {
  origin: API_ORIGIN,
  prefix: API_PREFIX,
  authMode: AUTH_MODE,
  isLocalDev: runtimeConfig.IS_LOCAL_DEV,
  socketPath: runtimeConfig.SOCKET_PATH,
  /**
   * WebSocket origin. Always derived from the resolved API origin so the
   * scheme can never be mismatched (http↔ws, https↔wss).
   */
  get wsOrigin() {
    return runtimeConfig.WS_ORIGIN || toWebSocketOrigin(window.location.origin);
  },
};

export default api;
