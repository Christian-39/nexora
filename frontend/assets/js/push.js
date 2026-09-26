/**
 * NEXORA — push.js
 * Web Push subscription lifecycle and service-worker registration.
 *
 * The VAPID *public* key is fetched from the backend. No private key, and no
 * push credential of any kind, ever reaches the browser. Notification content
 * is composed server-side and must respect the deployment's preview settings.
 */

import { ApiError, api } from './api.js';
import { Emitter, prefs } from './utils.js';
import { toast } from './ui.js';

export const pushEvents = new Emitter();

const SW_URL = 'sw.js';
const PROMPT_DISMISSED_KEY = 'pushPromptDismissed';

let registration = null;
let registering = null;

/* ============================================================
   Capability
   ============================================================ */

export function isPushSupported() {
  return (
    typeof navigator !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    typeof Notification !== 'undefined'
  );
}

export function permissionState() {
  if (typeof Notification === 'undefined') return 'unsupported';
  return Notification.permission; // 'default' | 'granted' | 'denied'
}

/* ============================================================
   Service worker
   ============================================================ */

/**
 * Register the service worker and wire the update flow.
 * Safe to call on every authenticated page.
 */
export async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return null;
  if (registration) return registration;
  if (registering) return registering;

  registering = (async () => {
    try {
      const reg = await navigator.serviceWorker.register(SW_URL, { scope: './', updateViaCache: 'none' });
      registration = reg;
      watchForUpdates(reg);
      // Periodically check for a newer frontend build.
      setInterval(() => reg.update().catch(() => {}), 60 * 60 * 1000);
      return reg;
    } catch (error) {
      console.warn('[push] service worker registration failed:', error?.message);
      return null;
    } finally {
      registering = null;
    }
  })();

  return registering;
}

/** Offer a non-destructive reload when a new frontend version is waiting. */
function watchForUpdates(reg) {
  const promptUpdate = (worker) => {
    toast('A new version of the app is available.', {
      type: 'info',
      duration: 0,
      action: {
        label: 'Reload',
        onClick: () => {
          worker.postMessage({ type: 'NEXORA_SKIP_WAITING' });
        },
      },
    });
  };

  if (reg.waiting && navigator.serviceWorker.controller) promptUpdate(reg.waiting);

  reg.addEventListener('updatefound', () => {
    const installing = reg.installing;
    if (!installing) return;
    installing.addEventListener('statechange', () => {
      if (installing.state === 'installed' && navigator.serviceWorker.controller) {
        promptUpdate(installing);
      }
    });
  });

  let reloading = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (reloading) return;
    reloading = true;
    window.location.reload();
  });
}

/* ============================================================
   Subscription
   ============================================================ */

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
  return output;
}

/**
 * Subscribe this device for push, registering the subscription with the
 * backend. Must be called from a user gesture on most browsers.
 * @returns {Promise<{ok:boolean, reason?:string}>}
 */
export async function subscribe() {
  if (!isPushSupported()) return { ok: false, reason: 'unsupported' };

  const reg = await registerServiceWorker();
  if (!reg) return { ok: false, reason: 'no-service-worker' };

  let permission = Notification.permission;
  if (permission === 'default') {
    permission = await Notification.requestPermission();
  }
  if (permission !== 'granted') {
    pushEvents.emit('permission', permission);
    return { ok: false, reason: permission === 'denied' ? 'denied' : 'dismissed' };
  }

  let config;
  try {
    config = await api.push.config();
  } catch (error) {
    if (error instanceof ApiError && error.isNotFound) return { ok: false, reason: 'push-disabled' };
    return { ok: false, reason: 'config-unavailable' };
  }

  const publicKey = config?.vapid_public_key || config?.public_key;
  if (!publicKey) return { ok: false, reason: 'push-disabled' };

  let subscription = await reg.pushManager.getSubscription();

  // If the server key rotated, the old subscription is useless.
  if (subscription) {
    const existingKey = subscription.options?.applicationServerKey;
    if (existingKey && !keysMatch(existingKey, publicKey)) {
      try { await subscription.unsubscribe(); } catch { /* ignore */ }
      subscription = null;
    }
  }

  if (!subscription) {
    try {
      subscription = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      });
    } catch (error) {
      return { ok: false, reason: 'subscribe-failed' };
    }
  }

  try {
    await api.push.subscribe({
      subscription: subscription.toJSON(),
      endpoint: subscription.endpoint,
      user_agent: navigator.userAgent.slice(0, 200),
    });
  } catch (error) {
    return { ok: false, reason: 'registration-failed' };
  }

  pushEvents.emit('subscribed', subscription.endpoint);
  return { ok: true };
}

function keysMatch(buffer, base64) {
  try {
    const a = new Uint8Array(buffer);
    const b = urlBase64ToUint8Array(base64);
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return false;
    return true;
  } catch {
    return false;
  }
}

export async function unsubscribe() {
  if (!isPushSupported()) return false;
  const reg = await registerServiceWorker();
  const subscription = await reg?.pushManager.getSubscription();
  if (!subscription) return true;
  try {
    await api.push.unsubscribe(subscription.endpoint);
  } catch { /* the backend prunes dead endpoints anyway */ }
  const ok = await subscription.unsubscribe();
  pushEvents.emit('unsubscribed');
  return ok;
}

export async function isSubscribed() {
  if (!isPushSupported()) return false;
  const reg = await navigator.serviceWorker.getRegistration();
  if (!reg) return false;
  const subscription = await reg.pushManager.getSubscription();
  return !!subscription;
}

/* ============================================================
   Gentle, non-nagging enable prompt
   ============================================================ */

/**
 * Show a one-time invitation to enable notifications.
 * Never auto-requests permission without a user gesture.
 */
export async function maybeOfferPush() {
  if (!isPushSupported()) return;
  if (permissionState() !== 'default') return;
  if (prefs.get(PROMPT_DISMISSED_KEY, false)) return;

  // Wait until the user has actually engaged with the app.
  setTimeout(() => {
    toast('Get notified about new messages on this device.', {
      type: 'info',
      title: 'Enable notifications',
      duration: 0,
      action: {
        label: 'Enable',
        onClick: async () => {
          const result = await subscribe();
          if (result.ok) toast('Notifications enabled for this device.', { type: 'success' });
          else if (result.reason === 'denied') {
            toast('Notifications are blocked in your browser settings.', { type: 'warning' });
          }
          prefs.set(PROMPT_DISMISSED_KEY, true);
        },
      },
    });
    prefs.set(PROMPT_DISMISSED_KEY, true);
  }, 20000);
}

/* ============================================================
   Notification click routing (messages from the service worker)
   ============================================================ */

/**
 * @param {(target:{conversationId?:string, url?:string})=>void} handler
 */
export function onNotificationNavigate(handler) {
  if (!('serviceWorker' in navigator)) return () => {};
  const listener = (event) => {
    const data = event.data;
    if (!data || data.type !== 'NEXORA_NOTIFICATION_CLICK') return;
    handler({ conversationId: data.conversationId, url: data.url });
  };
  navigator.serviceWorker.addEventListener('message', listener);
  return () => navigator.serviceWorker.removeEventListener('message', listener);
}

export default { registerServiceWorker, subscribe, unsubscribe, isSubscribed, isPushSupported, maybeOfferPush, onNotificationNavigate };
