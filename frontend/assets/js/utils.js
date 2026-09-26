/**
 * NEXORA — utils.js
 * Pure, dependency-free helpers. No DOM side effects, no network, no state.
 */

/* ============================================================
   DOM
   ============================================================ */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/**
 * Create an element. Text is always assigned via textContent — never innerHTML.
 * @param {string} tag
 * @param {object} [props] class/id/attrs/dataset/text/aria/on
 * @param {Array<Node|string>} [children]
 */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'html') throw new Error('utils.el: raw html is not permitted');
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key === 'on') for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
    else if (key in node && key !== 'list' && typeof value !== 'object') node[key] = value;
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function on(target, type, handler, options) {
  target.addEventListener(type, handler, options);
  return () => target.removeEventListener(type, handler, options);
}

/** Trap Tab focus inside a container (modals, sheets, lightbox). */
export function trapFocus(container) {
  const SELECTOR =
    'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]):not([type="hidden"]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';
  const handler = (e) => {
    if (e.key !== 'Tab') return;
    const items = $$(SELECTOR, container).filter((n) => n.offsetParent !== null || n === document.activeElement);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };
  container.addEventListener('keydown', handler);
  return () => container.removeEventListener('keydown', handler);
}

/* ============================================================
   Text safety
   ============================================================ */

/**
 * Render user text into a container safely, converting http(s) URLs into
 * links. Text nodes only — no HTML parsing of user content ever happens.
 * @returns {DocumentFragment}
 */
export function renderTextWithLinks(text) {
  const frag = document.createDocumentFragment();
  const source = String(text ?? '');
  // Conservative URL matcher: scheme-qualified only.
  const re = /\bhttps?:\/\/[^\s<>"')\]]+/gi;
  let last = 0;
  let match;
  while ((match = re.exec(source)) !== null) {
    if (match.index > last) frag.append(document.createTextNode(source.slice(last, match.index)));
    let url = match[0];
    // Don't swallow trailing sentence punctuation.
    let trail = '';
    while (/[.,;:!?]$/.test(url)) {
      trail = url.slice(-1) + trail;
      url = url.slice(0, -1);
    }
    if (isSafeHttpUrl(url)) {
      frag.append(
        el('a', {
          href: url,
          target: '_blank',
          rel: 'noopener noreferrer nofollow ugc',
          text: url,
        })
      );
    } else {
      frag.append(document.createTextNode(url));
    }
    if (trail) frag.append(document.createTextNode(trail));
    last = match.index + match[0].length;
  }
  if (last < source.length) frag.append(document.createTextNode(source.slice(last)));
  return frag;
}

/** Only http/https are ever treated as navigable. Blocks javascript:, data:, etc. */
export function isSafeHttpUrl(value) {
  if (typeof value !== 'string' || !value) return false;
  try {
    const u = new URL(value, window.location.origin);
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch {
    return false;
  }
}

/** Strict #RRGGBB validation for backend-provided brand colours. */
export function isHexColor(value) {
  return typeof value === 'string' && /^#[0-9a-fA-F]{6}$/.test(value.trim());
}

/** Initials for avatar fallbacks. */
export function initials(name, max = 2) {
  const parts = String(name || '')
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!parts.length) return '?';
  return parts
    .slice(0, max)
    .map((p) => [...p][0].toUpperCase())
    .join('');
}

/* ============================================================
   Formatting
   ============================================================ */

const LOCALE = undefined; // follow browser locale

export function parseDate(value) {
  if (!value) return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatTime(value) {
  const d = parseDate(value);
  if (!d) return '';
  return d.toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' });
}

export function formatDate(value) {
  const d = parseDate(value);
  if (!d) return '';
  return d.toLocaleDateString(LOCALE, { year: 'numeric', month: 'short', day: 'numeric' });
}

export function formatDateTime(value) {
  const d = parseDate(value);
  if (!d) return '';
  return `${formatDate(d)}, ${formatTime(d)}`;
}

export function isSameDay(a, b) {
  const da = parseDate(a);
  const db = parseDate(b);
  if (!da || !db) return false;
  return (
    da.getFullYear() === db.getFullYear() &&
    da.getMonth() === db.getMonth() &&
    da.getDate() === db.getDate()
  );
}

/** "Today" / "Yesterday" / weekday / date — for separators and lists. */
export function formatDayLabel(value) {
  const d = parseDate(value);
  if (!d) return '';
  const now = new Date();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (isSameDay(d, now)) return 'Today';
  if (isSameDay(d, yesterday)) return 'Yesterday';
  const diffDays = Math.floor((now - d) / 86400000);
  if (diffDays < 7 && diffDays >= 0) return d.toLocaleDateString(LOCALE, { weekday: 'long' });
  return formatDate(d);
}

/** Compact timestamp for conversation lists. */
export function formatListTime(value) {
  const d = parseDate(value);
  if (!d) return '';
  const now = new Date();
  if (isSameDay(d, now)) return formatTime(d);
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (isSameDay(d, yesterday)) return 'Yesterday';
  const diffDays = Math.floor((now - d) / 86400000);
  if (diffDays < 7 && diffDays >= 0) return d.toLocaleDateString(LOCALE, { weekday: 'short' });
  return d.toLocaleDateString(LOCALE, { day: '2-digit', month: '2-digit' });
}

export function formatRelative(value) {
  const d = parseDate(value);
  if (!d) return '';
  const seconds = Math.round((Date.now() - d.getTime()) / 1000);
  if (seconds < 45) return 'just now';
  const table = [
    [60, 'second'],
    [3600, 'minute'],
    [86400, 'hour'],
    [604800, 'day'],
    [2629800, 'week'],
    [31557600, 'month'],
    [Infinity, 'year'],
  ];
  const divisors = { second: 1, minute: 60, hour: 3600, day: 86400, week: 604800, month: 2629800, year: 31557600 };
  for (const [limit, unit] of table) {
    if (Math.abs(seconds) < limit) {
      const qty = Math.round(seconds / divisors[unit]);
      if (typeof Intl !== 'undefined' && Intl.RelativeTimeFormat) {
        return new Intl.RelativeTimeFormat(LOCALE, { numeric: 'auto' }).format(-qty, unit);
      }
      return `${qty} ${unit}${qty === 1 ? '' : 's'} ago`;
    }
  }
  return formatDate(d);
}

/** Last-seen text honouring backend privacy flags. */
export function formatLastSeen(value, { online = false, hidden = false } = {}) {
  if (hidden) return '';
  if (online) return 'Online';
  const d = parseDate(value);
  if (!d) return 'Offline';
  return `Last seen ${formatRelative(d)}`;
}

export function formatBytes(bytes, decimals = 1) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '';
  if (n === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  const value = n / 1024 ** i;
  return `${value.toFixed(i === 0 ? 0 : decimals)} ${units[i]}`;
}

/** Seconds → m:ss (or h:mm:ss). */
export function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (v) => String(v).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function formatCount(n) {
  const v = Number(n) || 0;
  if (v > 999) return '999+';
  return String(v);
}

/** Display-safe phone masking for lists where full numbers aren't needed. */
export function maskPhone(phone) {
  const s = String(phone || '');
  if (s.length <= 4) return s;
  return `${s.slice(0, Math.max(3, s.length - 6))}•••${s.slice(-2)}`;
}

/* ============================================================
   Timing
   ============================================================ */

export function debounce(fn, wait = 250) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  wrapped.flush = (...args) => {
    clearTimeout(timer);
    fn(...args);
  };
  return wrapped;
}

export function throttle(fn, wait = 250) {
  let last = 0;
  let timer = null;
  let pending = null;
  const invoke = (args) => {
    last = Date.now();
    fn(...args);
  };
  const wrapped = (...args) => {
    const now = Date.now();
    const remaining = wait - (now - last);
    pending = args;
    if (remaining <= 0) {
      clearTimeout(timer);
      timer = null;
      invoke(args);
    } else if (!timer) {
      timer = setTimeout(() => {
        timer = null;
        if (pending) invoke(pending);
      }, remaining);
    }
  };
  wrapped.cancel = () => {
    clearTimeout(timer);
    timer = null;
    pending = null;
  };
  return wrapped;
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Exponential backoff with full jitter, capped. */
export function backoffDelay(attempt, { base = 1000, max = 30000 } = {}) {
  const exp = Math.min(max, base * 2 ** Math.max(0, attempt - 1));
  return Math.round(exp / 2 + Math.random() * (exp / 2));
}

/* ============================================================
   Misc
   ============================================================ */

export function uid(prefix = 'id') {
  if (globalThis.crypto?.randomUUID) return `${prefix}_${crypto.randomUUID()}`;
  const rnd = globalThis.crypto?.getRandomValues
    ? Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, '0')).join('')
    : Math.random().toString(16).slice(2);
  return `${prefix}_${Date.now().toString(36)}_${rnd}`;
}

export function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

export function pick(obj, keys) {
  const out = {};
  for (const k of keys) if (obj && obj[k] !== undefined) out[k] = obj[k];
  return out;
}

/** Stable, shallow equality for render-skipping. */
export function shallowEqual(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => a[k] === b[k]);
}

/** Minimal event emitter used by stateful modules. */
export class Emitter {
  #map = new Map();

  on(type, handler) {
    if (!this.#map.has(type)) this.#map.set(type, new Set());
    this.#map.get(type).add(handler);
    return () => this.off(type, handler);
  }

  once(type, handler) {
    const off = this.on(type, (...args) => {
      off();
      handler(...args);
    });
    return off;
  }

  off(type, handler) {
    this.#map.get(type)?.delete(handler);
  }

  emit(type, ...args) {
    for (const handler of this.#map.get(type) ?? []) {
      try {
        handler(...args);
      } catch (err) {
        console.error(`[emitter:${type}]`, err);
      }
    }
    for (const handler of this.#map.get('*') ?? []) {
      try {
        handler(type, ...args);
      } catch (err) {
        console.error('[emitter:*]', err);
      }
    }
  }

  clear() {
    this.#map.clear();
  }
}

/** Bounded LRU used for message/conversation caches (never unbounded growth). */
export class LRU {
  constructor(limit = 500) {
    this.limit = limit;
    this.map = new Map();
  }
  get(key) {
    if (!this.map.has(key)) return undefined;
    const value = this.map.get(key);
    this.map.delete(key);
    this.map.set(key, value);
    return value;
  }
  set(key, value) {
    if (this.map.has(key)) this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.limit) this.map.delete(this.map.keys().next().value);
    return value;
  }
  has(key) { return this.map.has(key); }
  delete(key) { return this.map.delete(key); }
  clear() { this.map.clear(); }
  get size() { return this.map.size; }
}

/** Read a cookie value (used only for CSRF token, never for secrets). */
export function getCookie(name) {
  const target = `${name}=`;
  for (const part of document.cookie ? document.cookie.split(';') : []) {
    const c = part.trim();
    if (c.startsWith(target)) return decodeURIComponent(c.slice(target.length));
  }
  return null;
}

/** Namespaced, failure-tolerant localStorage for NON-SENSITIVE preferences only. */
export const prefs = {
  key: (k) => `nexora.${k}`,
  get(k, fallback = null) {
    try {
      const raw = localStorage.getItem(this.key(k));
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(k, value) {
    try {
      localStorage.setItem(this.key(k), JSON.stringify(value));
      return true;
    } catch {
      return false;
    }
  },
  remove(k) {
    try {
      localStorage.removeItem(this.key(k));
    } catch { /* storage unavailable */ }
  },
};

/** Media type classification from a MIME string. */
export function mediaKind(mime = '') {
  const m = String(mime).toLowerCase();
  if (m.startsWith('image/')) return 'image';
  if (m.startsWith('video/')) return 'video';
  if (m.startsWith('audio/')) return 'audio';
  return 'file';
}

/** Extract a video poster frame client-side (avoids downloading whole file later). */
export function captureVideoPoster(file, { seekTo = 0.5, maxWidth = 640 } = {}) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement('video');
    video.preload = 'metadata';
    video.muted = true;
    video.playsInline = true;
    const done = (result) => {
      URL.revokeObjectURL(url);
      video.removeAttribute('src');
      resolve(result);
    };
    const fail = () => done(null);
    video.addEventListener('error', fail, { once: true });
    video.addEventListener('loadeddata', () => {
      try {
        video.currentTime = Math.min(seekTo, Math.max(0, (video.duration || 1) - 0.1));
      } catch {
        fail();
      }
    }, { once: true });
    video.addEventListener('seeked', () => {
      try {
        const scale = Math.min(1, maxWidth / (video.videoWidth || maxWidth));
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round((video.videoWidth || 0) * scale));
        canvas.height = Math.max(1, Math.round((video.videoHeight || 0) * scale));
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(
          (blob) => done(blob ? { blob, width: video.videoWidth, height: video.videoHeight, duration: video.duration } : null),
          'image/jpeg',
          0.72
        );
      } catch {
        fail();
      }
    }, { once: true });
    video.src = url;
  });
}

/** Read image intrinsic dimensions without loading it into a canvas. */
export function imageDimensions(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve({ width: img.naturalWidth, height: img.naturalHeight });
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      resolve(null);
    };
    img.src = url;
  });
}
