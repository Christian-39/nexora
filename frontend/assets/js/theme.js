/**
 * NEXORA — theme.js
 * Theme (light/dark/system) + dynamic branding from GET /api/public/config/.
 *
 * Security notes:
 *  - Only strictly-validated #RRGGBB values are written into CSS custom props.
 *  - Organization configuration is NEVER evaluated as CSS or HTML.
 *  - Logo/favicon URLs must resolve to http(s) and are applied with fallbacks.
 */

import { api, resolveMediaUrl } from './api.js';
import { Emitter, isHexColor, isSafeHttpUrl, prefs } from './utils.js';

const THEME_KEY = 'theme';
const CONFIG_CACHE_KEY = 'publicConfig';
const CONFIG_CACHE_TTL = 15 * 60 * 1000; // branding is public + low-churn

export const themeEvents = new Emitter();

/* ============================================================
   Theme
   ============================================================ */

const media = window.matchMedia('(prefers-color-scheme: dark)');

/** @returns {'light'|'dark'|'system'} */
export function getThemePreference() {
  const value = prefs.get(THEME_KEY, 'system');
  return ['light', 'dark', 'system'].includes(value) ? value : 'system';
}

export function resolveTheme(preference = getThemePreference()) {
  if (preference === 'light' || preference === 'dark') return preference;
  return media.matches ? 'dark' : 'light';
}

export function applyTheme(preference = getThemePreference()) {
  const resolved = resolveTheme(preference);
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themePref = preference;
  updateThemeColorMeta();
  themeEvents.emit('change', { preference, resolved });
  return resolved;
}

export function setTheme(preference) {
  prefs.set(THEME_KEY, preference);
  return applyTheme(preference);
}

media.addEventListener('change', () => {
  if (getThemePreference() === 'system') applyTheme('system');
});

function updateThemeColorMeta() {
  const resolved = document.documentElement.dataset.theme;
  const color = getComputedStyle(document.documentElement).getPropertyValue(resolved === 'dark' ? '--surface' : '--surface').trim();
  let meta = document.querySelector('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement('meta');
    meta.name = 'theme-color';
    document.head.append(meta);
  }
  if (color) meta.setAttribute('content', color);
}

/* ============================================================
   Branding / public configuration
   ============================================================ */

/** Shape used throughout the app; every field may be absent. */
const EMPTY_CONFIG = Object.freeze({
  app_name: 'NEXORA',
  app_short_name: 'NEXORA',
  organization_name: '',
  logo_url: null,
  favicon_url: null,
  primary_color: null,
  secondary_color: null,
  contact_phone: '',
  contact_email: '',
  address: '',
  website: '',
  about: '',
  support: '',
  policies: [],
  pwa: {},
  limits: {},
  features: {},
});

let currentConfig = { ...EMPTY_CONFIG };
let loadPromise = null;

export function getConfig() {
  return currentConfig;
}

/** Backend-configured limits with conservative fallbacks. */
export function getLimits() {
  const l = currentConfig.limits || {};
  return {
    maxMessageLength: Number(l.max_message_length) || 4000,
    maxImageBytes: Number(l.max_image_size) || 10 * 1024 * 1024,
    maxVideoBytes: Number(l.max_video_size) || 100 * 1024 * 1024,
    maxVoiceSeconds: Number(l.max_voice_duration) || 300,
    maxVoiceBytes: Number(l.max_voice_size) || 20 * 1024 * 1024,
    allowedImageTypes: Array.isArray(l.allowed_image_types) ? l.allowed_image_types : ['image/jpeg', 'image/png', 'image/webp', 'image/gif'],
    allowedVideoTypes: Array.isArray(l.allowed_video_types) ? l.allowed_video_types : ['video/mp4', 'video/webm', 'video/quicktime'],
    allowedAudioTypes: Array.isArray(l.allowed_audio_types) ? l.allowed_audio_types : ['audio/webm', 'audio/ogg', 'audio/mp4', 'audio/mpeg'],
  };
}

/** Backend feature flags — the UI only *offers* what the backend enables. */
export function getFeatures() {
  const f = currentConfig.features || {};
  return {
    replies: f.replies !== false,
    reactions: !!f.reactions,
    editing: !!f.message_editing,
    deleteForEveryone: !!f.delete_for_everyone,
    voiceNotes: f.voice_notes !== false,
    videoMessages: f.video_messages !== false,
    presence: f.presence !== false,
    typing: f.typing !== false,
    push: f.push !== false,
    memberLeaveGroup: !!f.member_leave_group,
  };
}

function readCache() {
  const cached = prefs.get(CONFIG_CACHE_KEY);
  if (!cached || typeof cached !== 'object') return null;
  if (!cached.at || Date.now() - cached.at > CONFIG_CACHE_TTL) return null;
  return cached.value || null;
}

function writeCache(value) {
  prefs.set(CONFIG_CACHE_KEY, { at: Date.now(), value });
}

/**
 * Load and apply public configuration. Safe to call on every page.
 * Uses a short-lived local cache so first paint is branded, then revalidates.
 * @param {object} [options] { force }
 */
export async function loadBranding(options = {}) {
  if (loadPromise && !options.force) return loadPromise;

  // 1. Paint from cache immediately (branding is public, not sensitive).
  const cached = readCache();
  if (cached) {
    currentConfig = { ...EMPTY_CONFIG, ...cached };
    applyBranding(currentConfig);
  }

  // 2. Revalidate against the backend.
  loadPromise = (async () => {
    try {
      const data = await api.publicConfig();
      if (data && typeof data === 'object') {
        currentConfig = { ...EMPTY_CONFIG, ...data };
        writeCache(currentConfig);
        applyBranding(currentConfig);
        themeEvents.emit('config', currentConfig);
      }
    } catch (error) {
      // Branding failure must never block the application.
      if (!cached) {
        applyBranding(currentConfig);
        themeEvents.emit('config', currentConfig);
      }
      console.warn('[branding] public configuration unavailable:', error.code || error.message);
    } finally {
      loadPromise = null;
    }
    return currentConfig;
  })();

  return loadPromise;
}

/**
 * Apply configuration to the document. Every value is validated before use.
 */
export function applyBranding(config = currentConfig) {
  const root = document.documentElement;

  // --- Colours: strict #RRGGBB only ---
  if (isHexColor(config.primary_color)) {
    root.style.setProperty('--brand-primary', config.primary_color.trim());
    const darkVariant = lighten(config.primary_color.trim(), 0.42);
    if (darkVariant) root.style.setProperty('--brand-accent-dark', darkVariant);
  }
  if (isHexColor(config.secondary_color)) {
    root.style.setProperty('--brand-secondary', config.secondary_color.trim());
  }
  updateThemeColorMeta();

  // --- Names ---
  const appName = safeText(config.app_name) || 'NEXORA';
  const orgName = safeText(config.organization_name);
  for (const node of document.querySelectorAll('[data-brand="app-name"]')) node.textContent = appName;
  for (const node of document.querySelectorAll('[data-brand="org-name"]')) node.textContent = orgName || appName;
  for (const node of document.querySelectorAll('[data-brand="short-name"]')) {
    node.textContent = safeText(config.app_short_name) || appName;
  }
  for (const node of document.querySelectorAll('[data-brand="phone"]')) node.textContent = safeText(config.contact_phone);
  for (const node of document.querySelectorAll('[data-brand="email"]')) node.textContent = safeText(config.contact_email);
  for (const node of document.querySelectorAll('[data-brand="address"]')) node.textContent = safeText(config.address);
  for (const node of document.querySelectorAll('[data-brand="about"]')) node.textContent = safeText(config.about);
  for (const node of document.querySelectorAll('[data-brand="support"]')) node.textContent = safeText(config.support);
  for (const node of document.querySelectorAll('[data-brand="website"]')) {
    const url = resolveMediaUrl(config.website);
    if (url && isSafeHttpUrl(url)) {
      node.textContent = safeText(config.website);
      if (node.tagName === 'A') {
        node.href = url;
        node.rel = 'noopener noreferrer';
      }
    } else {
      node.textContent = '';
    }
  }

  // --- Document title (page-specific prefix preserved) ---
  const titlePrefix = document.body?.dataset.pageTitle;
  const brandName = orgName || appName;
  document.title = titlePrefix ? `${titlePrefix} · ${brandName}` : brandName;

  // --- Logo with graceful fallback ---
  const logoUrl = resolveMediaUrl(config.logo_url);
  for (const node of document.querySelectorAll('[data-brand="logo"]')) applyLogo(node, logoUrl, brandName);

  // --- Favicon ---
  const faviconUrl = resolveMediaUrl(config.favicon_url) || logoUrl;
  if (faviconUrl && isSafeHttpUrl(faviconUrl)) applyFavicon(faviconUrl);

  // --- Manifest branding hints for the service worker / install prompt ---
  const manifestName = safeText(config.pwa?.name) || brandName;
  document.querySelector('meta[name="application-name"]')?.setAttribute('content', manifestName);
  document.querySelector('meta[name="apple-mobile-web-app-title"]')?.setAttribute('content', safeText(config.app_short_name) || manifestName);

  themeEvents.emit('branding', config);
}

function applyLogo(node, url, name) {
  if (node.tagName === 'IMG') {
    if (url) {
      node.alt = name ? `${name} logo` : 'Organization logo';
      node.addEventListener('error', () => swapToFallback(node, name), { once: true });
      node.src = url;
      node.hidden = false;
    } else {
      swapToFallback(node, name);
    }
    return;
  }
  // Container form: render <img> or initials block.
  node.textContent = '';
  if (url) {
    const img = new Image();
    img.className = 'app-nav__logo';
    img.alt = name ? `${name} logo` : 'Organization logo';
    img.decoding = 'async';
    img.addEventListener('error', () => {
      img.remove();
      node.append(fallbackMark(name));
    }, { once: true });
    img.src = url;
    node.append(img);
  } else {
    node.append(fallbackMark(name));
  }
}

function fallbackMark(name) {
  const span = document.createElement('span');
  span.className = 'app-nav__logo-fallback';
  span.setAttribute('aria-hidden', 'true');
  const text = String(name || 'NEXORA').trim();
  span.textContent = text ? text.slice(0, 2).toUpperCase() : 'NX';
  return span;
}

function swapToFallback(imgNode, name) {
  const mark = fallbackMark(name);
  mark.className = imgNode.className || 'app-nav__logo-fallback';
  mark.classList.add('app-nav__logo-fallback');
  imgNode.replaceWith(mark);
}

function applyFavicon(url) {
  const versioned = url.includes('?') ? `${url}&v=${Date.now().toString(36).slice(-4)}` : url;
  let link = document.querySelector('link[rel="icon"]');
  if (!link) {
    link = document.createElement('link');
    link.rel = 'icon';
    document.head.append(link);
  }
  link.href = versioned;
  const apple = document.querySelector('link[rel="apple-touch-icon"]');
  if (apple) apple.href = url;
}

function safeText(value) {
  if (value === null || value === undefined) return '';
  const text = String(value).trim();
  // Guard against absurd payloads; this is display text only.
  return text.length > 2000 ? text.slice(0, 2000) : text;
}

/** Produce a lighter variant of a hex colour for dark-mode accents. */
function lighten(hex, amount) {
  if (!isHexColor(hex)) return null;
  const n = parseInt(hex.slice(1), 16);
  const mix = (channel) => Math.round(channel + (255 - channel) * amount);
  const r = mix((n >> 16) & 255);
  const g = mix((n >> 8) & 255);
  const b = mix(n & 255);
  return `#${[r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')}`;
}

/* ============================================================
   Policies
   ============================================================ */

/** @returns {Array<{key:string,title:string,body:string,url:string|null}>} */
export function getPolicies() {
  const raw = currentConfig.policies;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((p) => ({
      key: safeText(p?.key || p?.slug),
      title: safeText(p?.title || p?.name),
      body: typeof p?.body === 'string' ? p.body : typeof p?.content === 'string' ? p.content : '',
      url: isSafeHttpUrl(p?.url) ? p.url : null,
    }))
    .filter((p) => p.title);
}

/* ============================================================
   Boot — theme must apply before first paint wherever possible.
   ============================================================ */

applyTheme();

export default { applyTheme, setTheme, getThemePreference, resolveTheme, loadBranding, getConfig, getLimits, getFeatures, getPolicies };
