/**
 * NEXORA — sw.js  (service worker)
 *
 * Caching policy (deliberately conservative):
 *   - PRECACHE   : application shell + static assets, versioned.
 *   - RUNTIME    : same-origin static assets only (cache-first, revalidated).
 *   - NEVER      : anything under the API prefix, any authenticated response,
 *                  any private media, any WebSocket traffic.
 *
 * Private conversation data must not survive in a shared cache. Every /api/
 * request goes straight to the network; if it fails we return a JSON error the
 * client can classify, never a stale message list.
 */

const VERSION = 'v1.3.0';
const PRECACHE = `nexora-shell-${VERSION}`;
const RUNTIME = `nexora-static-${VERSION}`;
const OFFLINE_URL = 'offline.html';

/** Application shell — safe, public, non-personalised resources only. */
const PRECACHE_URLS = [
  'index.html',
  'login.html',
  'chat.html',
  'groups.html',
  'members.html',
  'settings.html',
  'profile.html',
  'admin.html',
  '403.html',
  '404.html',
  '500.html',
  OFFLINE_URL,
  'manifest.webmanifest',
  'assets/css/variables.css',
  'assets/css/themes.css',
  'assets/css/typography.css',
  'assets/css/main.css',
  'assets/css/components.css',
  'assets/css/chat.css',
  'assets/css/admin.css',
  'assets/css/responsive.css',
  'assets/js/api.js',
  'assets/js/config.js',
  'assets/js/auth.js',
  'assets/js/connection-ux.js',
  'assets/js/chat.js',
  'assets/js/groups.js',
  'assets/js/media.js',
  'assets/js/members.js',
  'assets/js/messages.js',
  'assets/js/navigation.js',
  'assets/js/notifications.js',
  'assets/js/pin.js',
  'assets/js/presence.js',
  'assets/js/push.js',
  'assets/js/settings.js',
  'assets/js/theme.js',
  'assets/js/ui.js',
  'assets/js/utils.js',
  'assets/js/voice.js',
  'assets/js/websocket.js',
  'assets/images/icon-192.png',
  'assets/images/icon-512.png',
];

/**
 * Paths whose responses must never be cached.
 *
 * Everything under /api/ (authenticated JSON *and* private media, which is
 * served from /api/media/) plus the avatar/branding endpoints. A shared
 * Cache Storage entry would outlive sign-out and could be read by the next
 * person to use the device, so these are always network-only.
 */
const PRIVATE_PATH = /\/api\//;

/* ============================================================
   Install / activate
   ============================================================ */

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(PRECACHE);
      // addAll fails atomically; add individually so one 404 can't break install.
      await Promise.all(
        PRECACHE_URLS.map(async (url) => {
          try {
            await cache.add(new Request(url, { cache: 'reload' }));
          } catch {
            /* optional asset missing in this deployment */
          }
        })
      );
      // Do NOT skipWaiting automatically: the page offers a safe update prompt.
    })()
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((key) => key.startsWith('nexora-') && key !== PRECACHE && key !== RUNTIME).map((key) => caches.delete(key))
      );
      if (self.registration.navigationPreload) {
        await self.registration.navigationPreload.enable();
      }
      await self.clients.claim();
    })()
  );
});

self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'NEXORA_SKIP_WAITING') {
    self.skipWaiting();
  }
  if (data.type === 'NEXORA_CLEAR_PRIVATE_CACHE') {
    // Defensive: we never store private responses, but sign-out clears anyway.
    event.waitUntil(
      (async () => {
        const keys = await caches.keys();
        await Promise.all(keys.filter((k) => k.startsWith('nexora-private')).map((k) => caches.delete(k)));
      })()
    );
  }
});

/* ============================================================
   Fetch strategies
   ============================================================ */

self.addEventListener('fetch', (event) => {
  const { request } = event;

  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Never touch API traffic, WebSocket upgrades or cross-origin media.
  if (PRIVATE_PATH.test(url.pathname)) {
    event.respondWith(networkOnlyApi(request));
    return;
  }
  if (url.origin !== self.location.origin) return; // let the network handle it
  if (request.headers.get('upgrade') === 'websocket') return;

  // Navigations: network-first with an offline fallback.
  if (request.mode === 'navigate') {
    event.respondWith(handleNavigation(event));
    return;
  }

  // Application CODE (js/css/manifest): network-first.
  //
  // Cache-first was able to keep an old build alive across reloads, which is
  // exactly how a fixed bug appears to "survive a refresh". The cached copy is
  // still kept and is still served instantly when the network fails, so
  // offline behaviour is unchanged.
  if (isCodeAsset(url.pathname)) {
    event.respondWith(networkFirst(request));
    return;
  }

  // Immutable-ish media/fonts: cache-first, revalidated in the background.
  if (isStaticAsset(url.pathname)) {
    event.respondWith(staleWhileRevalidate(request));
  }
});

function isCodeAsset(pathname) {
  return /\.(?:css|js|mjs|webmanifest)$/i.test(pathname);
}

function isStaticAsset(pathname) {
  return /\.(?:png|jpg|jpeg|webp|svg|ico|woff2?)$/i.test(pathname);
}

/** Fresh code when online, cached code when not. Never used for /api/. */
async function networkFirst(request) {
  const cache = await caches.open(RUNTIME);
  try {
    const response = await fetch(request);
    if (response && response.ok && response.type === 'basic') {
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  } catch {
    const cached = (await cache.match(request)) || (await caches.open(PRECACHE).then((c) => c.match(request)));
    if (cached) return cached;
    throw new Error('offline');
  }
}

/**
 * API requests are never served from cache. On transport failure we synthesize
 * a normalized error envelope so api.js can classify it as offline.
 */
async function networkOnlyApi(request) {
  try {
    return await fetch(request);
  } catch {
    return new Response(
      JSON.stringify({
        success: false,
        message: "You're offline. Reconnect to continue.",
        code: 'OFFLINE',
        errors: {},
      }),
      { status: 503, headers: { 'Content-Type': 'application/json' } }
    );
  }
}

async function handleNavigation(event) {
  try {
    const preload = await event.preloadResponse;
    if (preload) return preload;
    const response = await fetch(event.request);
    return response;
  } catch {
    const cache = await caches.open(PRECACHE);
    const url = new URL(event.request.url);
    // Prefer the exact shell page if we precached it.
    const cached = await cache.match(url.pathname.replace(/^\//, '') || 'index.html');
    return cached || (await cache.match(OFFLINE_URL)) || Response.error();
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(RUNTIME);
  const cached = await cache.match(request);

  const network = fetch(request)
    .then((response) => {
      if (response && response.ok && response.type === 'basic') {
        cache.put(request, response.clone()).catch(() => {});
      }
      return response;
    })
    .catch(() => null);

  if (cached) return cached;
  const fresh = await network;
  if (fresh) return fresh;

  const shell = await caches.open(PRECACHE);
  return (await shell.match(request)) || Response.error();
}

/* ============================================================
   Push notifications
   ============================================================ */

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { title: 'New activity', body: '' };
  }

  // The backend decides what is safe to show. When previews are disabled it
  // sends a generic body; the service worker never invents message content.
  const title = payload.title || 'New message';
  const options = {
    body: payload.body || '',
    icon: payload.icon || 'assets/images/icon-192.png',
    badge: payload.badge || 'assets/images/icon-192.png',
    tag: payload.tag || (payload.conversation_id ? `conv-${payload.conversation_id}` : 'nexora'),
    renotify: !!payload.renotify,
    timestamp: payload.timestamp ? Number(payload.timestamp) : Date.now(),
    requireInteraction: false,
    silent: !!payload.silent,
    data: {
      conversationId: payload.conversation_id ? String(payload.conversation_id) : null,
      url: sanitizeInternalUrl(payload.url),
      notificationId: payload.notification_id ?? null,
    },
  };

  event.waitUntil(
    (async () => {
      await self.registration.showNotification(title, options);
      // Reflect the authoritative unread count the backend sent with the push.
      if (typeof payload.unread_total === 'number' && 'setAppBadge' in self.navigator) {
        try {
          if (payload.unread_total > 0) await self.navigator.setAppBadge(payload.unread_total);
          else await self.navigator.clearAppBadge();
        } catch { /* badging unsupported */ }
      }
    })()
  );
});

/** Only same-scope relative paths are ever followed from a notification. */
function sanitizeInternalUrl(value) {
  if (typeof value !== 'string' || !value) return null;
  if (/^[a-z]+:/i.test(value) || value.startsWith('//')) return null;
  return value.replace(/^\//, '');
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const data = event.notification.data || {};
  const conversationId = data.conversationId;
  const target = data.url || (conversationId ? `chat.html?c=${encodeURIComponent(conversationId)}` : 'chat.html');
  const targetUrl = new URL(target, self.location.origin + self.location.pathname.replace(/sw\.js$/, '')).href;

  event.waitUntil(
    (async () => {
      const clientList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });

      // Focus an existing window and route in-app (keeps the session alive).
      for (const client of clientList) {
        if (!client.url.startsWith(self.location.origin)) continue;
        await client.focus();
        client.postMessage({
          type: 'NEXORA_NOTIFICATION_CLICK',
          conversationId,
          url: target,
          notificationId: data.notificationId,
        });
        return;
      }

      // Otherwise open a window. Authentication is re-established on load;
      // an unauthenticated user lands on the sign-in page, not on chat data.
      await self.clients.openWindow(targetUrl);
    })()
  );
});

self.addEventListener('notificationclose', () => {
  // No tracking of dismissals; nothing private is recorded here.
});

/* ============================================================
   Push subscription rotation
   ============================================================ */

self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil(
    (async () => {
      const clientList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      // Let an open page re-subscribe with a credentialed request.
      for (const client of clientList) {
        client.postMessage({ type: 'NEXORA_RESUBSCRIBE_PUSH' });
      }
    })()
  );
});
