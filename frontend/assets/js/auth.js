/**
 * NEXORA — auth.js
 * Session state, route guards, login/logout, credential change.
 *
 * Security contract:
 *  - No PIN, password or token is ever written to storage or logged.
 *  - In cookie mode the browser holds an HttpOnly session; nothing is mirrored.
 *  - In bearer mode the access token lives only in memory (api.js tokenStore).
 *  - Role flags drive UX only. The backend is the authorization boundary.
 */

import { ApiError, api, apiEvents, tokenStore } from './api.js';
import { Emitter, prefs } from './utils.js';
import { realtime } from './websocket.js';

export const authEvents = new Emitter();

const ROLE_ADMIN = 'admin';
const ROLE_MEMBER = 'member';

let currentUser = null;
let bootstrapPromise = null;
let signingOut = false;
/**
 * True when the most recent bootstrap could NOT verify the session because the
 * backend/network was unavailable (timeout, offline, 5xx) rather than because
 * the backend actually rejected the credential (401/403). A connectivity
 * failure must never be mistaken for a logout.
 */
let sessionUnverified = false;

/** Whether the last session check failed for connectivity reasons only. */
export function isSessionUnverified() {
  return sessionUnverified;
}

/* ============================================================
   Session state
   ============================================================ */

export function getUser() {
  return currentUser;
}

export function isAuthenticated() {
  return !!currentUser;
}

export function isAdmin() {
  return normalizeRole(currentUser?.role) === ROLE_ADMIN || currentUser?.is_admin === true;
}

export function isMember() {
  return isAuthenticated() && !isAdmin();
}

/** True when the backend says the initial/default credential is still in use. */
export function mustChangeCredential() {
  return !!(currentUser && (currentUser.must_change_pin || currentUser.requires_pin_change || currentUser.is_default_pin));
}

function normalizeRole(role) {
  const value = String(role || '').toLowerCase();
  if (value.includes('admin')) return ROLE_ADMIN;
  return ROLE_MEMBER;
}

function setUser(user) {
  const previous = currentUser;
  currentUser = user ? { ...user, role: normalizeRole(user.role || (user.is_admin ? 'admin' : 'member')) } : null;
  if (previous?.id !== currentUser?.id || (!previous) !== (!currentUser)) {
    authEvents.emit('user', currentUser);
  } else {
    authEvents.emit('user-updated', currentUser);
  }
  return currentUser;
}

/** Merge a partial profile update coming from REST or WebSocket. */
export function patchUser(partial) {
  if (!currentUser || !partial) return currentUser;
  return setUser({ ...currentUser, ...partial });
}

/* ============================================================
   Bootstrap
   ============================================================ */

/**
 * Establish the session for the current page.
 * @param {object} [options] { required: boolean }
 * @returns {Promise<object|null>} the authenticated user or null
 */
export function bootstrap(options = {}) {
  if (bootstrapPromise) return bootstrapPromise;
  bootstrapPromise = (async () => {
    try {
      const me = await api.me.get({ retries: 1, timeout: 12000 });
      sessionUnverified = false;
      return setUser(me);
    } catch (error) {
      if (error instanceof ApiError && error.isAuth) {
        // The backend positively rejected the credential: this is a real logout.
        sessionUnverified = false;
        setUser(null);
        return null;
      }
      // Network/server issues must not silently log the user out. Flag the
      // session as *unverified* (not gone) so requireSession keeps the shell
      // mounted instead of bouncing to the sign-in screen.
      if (error instanceof ApiError && (error.isNetwork || error.isOffline || error.isTimeout || error.isServer)) {
        sessionUnverified = true;
        authEvents.emit('bootstrap-error', error);
        return null;
      }
      sessionUnverified = false;
      setUser(null);
      return null;
    } finally {
      bootstrapPromise = null;
    }
  })();
  return bootstrapPromise;
}

/**
 * Guard an authenticated page. Redirects to login when there is no session.
 * @param {object} [options] { adminOnly }
 * @returns {Promise<object|null>}
 */
export async function requireSession(options = {}) {
  const user = await bootstrap();
  if (!user) {
    if (sessionUnverified) {
      // Could not confirm the session because the backend/network was
      // unreachable. Keep the (already-mounted) shell in place and let the
      // caller degrade gracefully; a later request reconciles the real state.
      // We deliberately do NOT redirect to login on a connectivity failure.
      authEvents.emit('session-unverified');
      return null;
    }
    redirectToLogin();
    return null;
  }
  if (mustChangeCredential() && !isOnPage('login.html')) {
    // The credential change is enforced by the backend too; this is the UX path.
    window.location.replace(`login.html?change=1&next=${encodeURIComponent(currentPath())}`);
    return null;
  }
  if (options.adminOnly && !isAdmin()) {
    window.location.replace('403.html');
    return null;
  }
  return user;
}

/** Redirect an already-authenticated user away from the login page. */
export async function redirectIfAuthenticated() {
  const user = await bootstrap();
  if (user && !mustChangeCredential()) {
    window.location.replace(landingPage());
    return true;
  }
  return false;
}

export function landingPage() {
  const next = new URLSearchParams(window.location.search).get('next');
  if (next && isSafeInternalPath(next)) return next;
  return isAdmin() ? 'admin.html' : 'chat.html';
}

function isSafeInternalPath(path) {
  // Only same-origin, relative, non-protocol paths.
  return typeof path === 'string' && /^[A-Za-z0-9_\-./?=&%#]+$/.test(path) && !path.startsWith('//') && !path.includes(':');
}

function currentPath() {
  return window.location.pathname.split('/').pop() + window.location.search;
}

function isOnPage(name) {
  return window.location.pathname.endsWith(name);
}

export function redirectToLogin(reason) {
  if (isOnPage('login.html')) return;
  const params = new URLSearchParams();
  const next = currentPath();
  if (next && next !== 'index.html') params.set('next', next);
  if (reason) params.set('reason', reason);
  const qs = params.toString();
  window.location.replace(`login.html${qs ? `?${qs}` : ''}`);
}

/* ============================================================
   Login / logout
   ============================================================ */

/**
 * @param {string} identifier phone or login identifier
 * @param {string} pin six-digit credential (never stored, never logged)
 * @returns {Promise<{user:object, mustChangePin:boolean}>}
 */
export async function login(identifier, pin) {
  const payload = await api.auth.login(String(identifier).trim(), String(pin));
  // Bearer deployments return a short-lived access token; keep it in memory only.
  if (tokenStore.isBearerMode) {
    const token = payload?.access || payload?.access_token || payload?.token;
    if (token) tokenStore.set(token);
  }
  let user = payload?.user || payload?.me || null;
  if (!user) user = await api.me.get();
  setUser(user);
  authEvents.emit('login', currentUser);
  return { user: currentUser, mustChangePin: mustChangeCredential() };
}

/**
 * Change the six-digit credential.
 * @param {{currentPin?:string,newPin:string,confirmPin:string}} input
 */
export async function changePin({ currentPin, newPin, confirmPin }) {
  const body = { new_pin: String(newPin), confirm_pin: String(confirmPin) };
  if (currentPin) body.current_pin = String(currentPin);
  const result = await api.auth.changePin(body);
  // Backend may rotate the session/token after a credential change.
  if (tokenStore.isBearerMode) {
    const token = result?.access || result?.access_token;
    if (token) tokenStore.set(token);
  }
  const user = result?.user || (await api.me.get());
  setUser(user);
  authEvents.emit('pin-changed');
  return currentUser;
}

export async function logout({ silent = false } = {}) {
  if (signingOut) return;
  signingOut = true;
  // Kill the realtime transport first: timers, heartbeat, queued events and
  // reconnection all stop before the credential is invalidated, so a logout
  // can never leave a socket retrying against a dead session.
  realtime.stop('logout');
  try {
    await api.auth.logout();
  } catch {
    // Even if the call fails we clear local state; the cookie may already be gone.
  } finally {
    tokenStore.clear();
    clearLocalSessionArtifacts();
    setUser(null);
    authEvents.emit('logout');
    signingOut = false;
    if (!silent) window.location.replace('login.html?reason=signed-out');
  }
}

/** Remove non-authoritative local caches on sign-out (never holds secrets). */
function clearLocalSessionArtifacts() {
  prefs.remove('lastConversation');
  prefs.remove('unreadSnapshot');
  prefs.remove('draft');
  try {
    // Drop any private caches the service worker may hold for this user.
    navigator.serviceWorker?.controller?.postMessage({ type: 'NEXORA_CLEAR_PRIVATE_CACHE' });
  } catch { /* no service worker */ }
}

/* ============================================================
   Sessions (where the backend supports device session management)
   ============================================================ */

export async function listSessions() {
  return api.auth.sessions();
}

export async function revokeSession(id) {
  await api.auth.revokeSession(id);
  authEvents.emit('session-revoked', id);
}

/* ============================================================
   Validation helpers (UI-side only — backend re-validates)
   ============================================================ */

export const SIX_DIGITS = /^\d{6}$/;

export function validateIdentifier(value) {
  const v = String(value || '').trim();
  if (!v) return 'Enter your phone number or login identifier.';
  if (v.length < 4) return 'That identifier looks too short.';
  if (v.length > 64) return 'That identifier is too long.';
  return null;
}

export function validatePin(value, label = 'PIN') {
  const v = String(value || '');
  if (!v) return `Enter your six-digit ${label}.`;
  if (!SIX_DIGITS.test(v)) return `Your ${label} must be exactly six digits.`;
  return null;
}

export function validateNewPin(newPin, confirmPin, currentPin = null) {
  const base = validatePin(newPin, 'new PIN');
  if (base) return { field: 'new', message: base };
  if (/^(\d)\1{5}$/.test(newPin)) {
    return { field: 'new', message: 'Choose a PIN that is not a single repeated digit.' };
  }
  if (/^(012345|123456|234567|345678|456789|567890|987654|654321)$/.test(newPin)) {
    return { field: 'new', message: 'Choose a PIN that is not a simple sequence.' };
  }
  if (currentPin && newPin === currentPin) {
    return { field: 'new', message: 'Your new PIN must be different from your current PIN.' };
  }
  if (newPin !== confirmPin) {
    return { field: 'confirm', message: 'The two PINs do not match.' };
  }
  return null;
}

/* ============================================================
   Global reactions to API auth events
   ============================================================ */

apiEvents.on('unauthorized', () => {
  if (signingOut) return;
  if (!currentUser) return;
  tokenStore.clear();
  // The session is gone: the socket must not keep reconnecting behind the
  // "session expired" screen.
  realtime.stop('session-expired');
  setUser(null);
  authEvents.emit('session-expired');
});

export default {
  bootstrap,
  requireSession,
  redirectIfAuthenticated,
  redirectToLogin,
  login,
  logout,
  changePin,
  getUser,
  isAuthenticated,
  isAdmin,
  isMember,
  mustChangeCredential,
  authEvents,
};
