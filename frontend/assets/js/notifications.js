/**
 * NEXORA — notifications.js
 * Backend-authoritative unread state, the notification centre UI, and the
 * PWA application badge.
 *
 * Counts always come from the backend (summary endpoint + WebSocket deltas).
 * We never derive unread counts by downloading message history.
 */

import { ApiError, api, normalizePage } from './api.js';
import { Emitter, clear, el, formatCount, formatRelative } from './utils.js';
import { emptyState, errorState, icon, loadingRow, toast } from './ui.js';
import { socketEvents } from './websocket.js';

export const unreadEvents = new Emitter();
export const notificationEvents = new Emitter();

/* ============================================================
   Unread state
   ============================================================ */

const UNREAD_CACHE_KEY = 'nexora.unreadSnapshot.v1';

function readUnreadSnapshot() {
  try {
    const value = JSON.parse(sessionStorage.getItem(UNREAD_CACHE_KEY) || 'null');
    return value && typeof value === 'object' ? value : {};
  } catch {
    return {};
  }
}

const cachedUnread = readUnreadSnapshot();
const unread = {
  total: Number(cachedUnread.total) || 0,
  conversations: Number(cachedUnread.conversations) || 0,
  groups: Number(cachedUnread.groups) || 0,
  notifications: Number(cachedUnread.notifications) || 0,
  /** conversationId -> count */
  byConversation: new Map(Array.isArray(cachedUnread.byConversation) ? cachedUnread.byConversation : []),
};

function persistUnread() {
  try {
    sessionStorage.setItem(UNREAD_CACHE_KEY, JSON.stringify({
      total: unread.total,
      conversations: unread.conversations,
      groups: unread.groups,
      notifications: unread.notifications,
      byConversation: Array.from(unread.byConversation.entries()).slice(0, 200),
    }));
  } catch { /* storage unavailable */ }
}

export function getUnread() {
  return {
    total: unread.total,
    conversations: unread.conversations,
    groups: unread.groups,
    notifications: unread.notifications,
  };
}

export function getConversationUnread(conversationId) {
  return unread.byConversation.get(String(conversationId)) || 0;
}

function publishUnread() {
  persistUnread();
  unreadEvents.emit('change', getUnread());
  syncAppBadge(unread.total);
}

function recompute({ deriveMessagesFromMap = false } = {}) {
  if (deriveMessagesFromMap) {
    unread.conversations = Array.from(unread.byConversation.values()).reduce(
      (sum, entry) => sum + (Number.isFinite(entry) ? Math.max(0, entry) : 0),
      0
    );
  }
  unread.total = unread.conversations + unread.notifications;
  publishUnread();
}

function unreadCountFromEntry(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const value = entry.unread_count ?? entry.unread;
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : null;
}

/** Apply the authoritative summary from REST or a full WebSocket snapshot. */
export function applyUnreadSummary(summary) {
  if (!summary || typeof summary !== 'object') return;

  let hasFullConversationMap = false;
  const conversationMap = summary.conversations;
  if (Array.isArray(conversationMap)) {
    unread.byConversation.clear();
    for (const entry of conversationMap) {
      const id = String(entry?.conversation_id ?? entry?.id ?? '');
      const count = unreadCountFromEntry(entry);
      if (id && count !== null) unread.byConversation.set(id, count);
    }
    hasFullConversationMap = true;
  } else if (conversationMap && typeof conversationMap === 'object') {
    unread.byConversation.clear();
    for (const [rawId, rawCount] of Object.entries(conversationMap)) {
      const id = String(rawId);
      const value = Number(rawCount);
      if (id && Number.isFinite(value)) unread.byConversation.set(id, Math.max(0, value));
    }
    hasFullConversationMap = true;
  }

  const conversationId = summary.conversation_id == null ? '' : String(summary.conversation_id);
  const conversationCount =
    typeof summary.unread_count === 'number' && Number.isFinite(summary.unread_count)
      ? Math.max(0, summary.unread_count)
      : typeof summary.count === 'number' && Number.isFinite(summary.count)
        ? Math.max(0, summary.count)
        : null;
  let previousConversationCount;
  if (conversationId && conversationCount !== null) {
    previousConversationCount = unread.byConversation.get(conversationId);
    unread.byConversation.set(conversationId, conversationCount);
  }

  if (typeof summary.groups_unread === 'number') unread.groups = Math.max(0, summary.groups_unread);
  if (typeof summary.notifications_unread === 'number') {
    unread.notifications = Math.max(0, summary.notifications_unread);
  } else if (typeof summary.notifications === 'number') {
    unread.notifications = Math.max(0, summary.notifications);
  }

  const messageTotal = [
    summary.unread_messages_total,
    summary.total_unread_messages,
    summary.global,
    summary.total,
  ].find((value) => typeof value === 'number' && Number.isFinite(value));
  if (messageTotal !== undefined) {
    unread.conversations = Math.max(0, messageTotal);
  } else if (hasFullConversationMap) {
    unread.conversations = Array.from(unread.byConversation.values()).reduce((sum, value) => sum + value, 0);
  } else if (conversationId && conversationCount !== null && previousConversationCount !== undefined) {
    // A per-thread authoritative delta can adjust a known aggregate, but an
    // absent map entry is not assumed to mean zero.
    unread.conversations = Math.max(0, unread.conversations - previousConversationCount + conversationCount);
  }

  const combinedTotal = [summary.unread_total, summary.total_unread].find(
    (value) => typeof value === 'number' && Number.isFinite(value)
  );
  unread.total = combinedTotal === undefined
    ? unread.conversations + unread.notifications
    : Math.max(0, combinedTotal);
  publishUnread();
  if (conversationId && conversationCount !== null) unreadEvents.emit('conversation', conversationId, conversationCount);
}

export function setConversationUnread(conversationId, count) {
  const id = String(conversationId);
  const value = Math.max(0, Number(count) || 0);
  const previous = unread.byConversation.get(id);
  if (previous === value) return;
  unread.byConversation.set(id, value);
  if (previous !== undefined) {
    unread.conversations = Math.max(0, unread.conversations - previous + value);
  }
  recompute();
  unreadEvents.emit('conversation', id, value);
  if (previous === undefined) refreshUnread({ force: true });
}

export function incrementConversationUnread(conversationId, by = 1) {
  const id = String(conversationId);
  setConversationUnread(id, (unread.byConversation.get(id) || 0) + by);
}

export function clearConversationUnread(conversationId) {
  setConversationUnread(conversationId, 0);
}

export function setNotificationUnread(count) {
  unread.notifications = Math.max(0, Number(count) || 0);
  recompute();
}

/** Refresh authoritative counts. Called on load, on reconnect, on focus.
 *
 *  Those three triggers routinely fire within the same second (page load
 *  opens the socket, the socket opening refreshes again, focus lands too),
 *  so the in-flight call is shared and a just-completed result is reused for
 *  a short window instead of hammering the summary endpoint.
 */
let unreadRefreshInFlight = null;
let unreadRefreshAt = 0;
const UNREAD_REFRESH_DEDUP_MS = 2500;

export async function refreshUnread({ force = false, signal } = {}) {
  if (unreadRefreshInFlight) return unreadRefreshInFlight;
  if (!force && Date.now() - unreadRefreshAt < UNREAD_REFRESH_DEDUP_MS) return null;

  unreadRefreshInFlight = (async () => {
    try {
      const summary = await api.conversations.unreadSummary({ signal, retries: 1 });
      applyUnreadSummary(summary);
      return summary;
    } catch (error) {
      if (error instanceof ApiError && (error.isNotFound)) {
        // Deployment without the summary endpoint: fall back to the list payload.
        try {
          const response = await api.conversations.list({ limit: 50 }, { signal });
          const page = normalizePage(response.data ?? response);
          unread.byConversation.clear();
          for (const conv of page.items) {
            unread.byConversation.set(String(conv.id), Number(conv.unread_count) || 0);
          }
          recompute();
        } catch { /* leave counts untouched */ }
      }
      return null;
    } finally {
      unreadRefreshAt = Date.now();
      unreadRefreshInFlight = null;
    }
  })();
  return unreadRefreshInFlight;
}

/* ============================================================
   PWA badge
   ============================================================ */

let badgeSupported = null;

export function isBadgeSupported() {
  if (badgeSupported === null) {
    badgeSupported = typeof navigator !== 'undefined' && typeof navigator.setAppBadge === 'function';
  }
  return badgeSupported;
}

/**
 * Reflect unread state on the app icon where the platform supports it.
 * Support is not universal — failures are silent and non-blocking.
 */
export async function syncAppBadge(count = unread.total) {
  if (!isBadgeSupported()) return false;
  try {
    if (count > 0) await navigator.setAppBadge(count);
    else await navigator.clearAppBadge?.();
    return true;
  } catch {
    return false;
  }
}

export async function clearAppBadge() {
  if (!isBadgeSupported()) return;
  try { await navigator.clearAppBadge(); } catch { /* unsupported */ }
}

/* ============================================================
   Notification centre
   ============================================================ */

const NOTIF_ICONS = {
  message: 'message-square',
  group: 'messages-square',
  member: 'user-plus',
  security: 'shield-alert',
  system: 'info',
};

/**
 * Render the notification list into a container.
 * @param {HTMLElement} container
 * @param {object} options { onOpen(notification), pageSize }
 */
export async function mountNotificationCentre(container, options = {}) {
  const { onOpen, pageSize = 25 } = options;
  let cursor = null;
  let loading = false;
  let exhausted = false;
  const seen = new Set();

  const list = el('div', { class: 'notif-list' });
  const footer = el('div', {});
  clear(container);
  container.append(list, footer);

  async function loadPage(reset = false) {
    if (loading) return;
    loading = true;
    if (reset) {
      cursor = null;
      exhausted = false;
      seen.clear();
      clear(list);
    }
    const spinner = loadingRow('Loading notifications…');
    footer.replaceChildren(spinner);
    try {
      const response = await api.notifications.list({ limit: pageSize, cursor: cursor || undefined });
      const page = normalizePage(response.data ?? response);
      cursor = page.next;
      exhausted = !page.next;

      for (const raw of page.items) {
        const id = String(raw.id);
        if (seen.has(id)) continue; // backend aggregation may repeat entries
        seen.add(id);
        list.append(renderNotification(raw, onOpen));
      }

      if (!list.children.length) {
        clear(footer);
        list.append(
          emptyState({
            icon: 'bell',
            title: 'No notifications yet',
            text: 'Activity relevant to you will appear here.',
          })
        );
        return;
      }

      clear(footer);
      if (!exhausted) {
        const more = el('button', { type: 'button', class: 'btn btn--subtle btn--block', text: 'Load more' });
        more.addEventListener('click', () => loadPage(false));
        footer.append(more);
      }
    } catch (error) {
      clear(footer);
      footer.append(errorState({ text: error.message, onRetry: () => loadPage(reset) }));
    } finally {
      loading = false;
    }
  }

  await loadPage(true);

  const off = notificationEvents.on('new', (raw) => {
    const id = String(raw.id);
    if (seen.has(id)) return;
    seen.add(id);
    list.prepend(renderNotification(raw, onOpen));
  });

  return {
    reload: () => loadPage(true),
    destroy: off,
  };
}

function renderNotification(raw, onOpen) {
  const kind = String(raw.kind || raw.category || 'system').toLowerCase();
  const isUnread = !(raw.is_read ?? raw.read ?? false);

  const node = el('button', {
    type: 'button',
    class: 'notif-item',
    dataset: { unread: String(isUnread), id: String(raw.id) },
  });

  node.append(
    el('span', { class: 'activity-item__icon' }, [icon(NOTIF_ICONS[kind] || 'info', { size: 16 })]),
    el('span', { class: 'notif-item__body' }, [
      el('span', { class: 'notif-item__title', text: raw.title || 'Notification' }),
      raw.body || raw.message ? el('span', { class: 'notif-item__text clamp-2', text: raw.body || raw.message }) : null,
    ]),
    el('span', { class: 'notif-item__time', text: formatRelative(raw.created_at) })
  );

  node.addEventListener('click', async () => {
    if (isUnread) {
      node.dataset.unread = 'false';
      try {
        await api.notifications.markRead([raw.id]);
        setNotificationUnread(Math.max(0, unread.notifications - 1));
      } catch { /* the next refresh reconciles */ }
    }
    onOpen?.(raw);
  });

  return node;
}

export async function markAllNotificationsRead() {
  await api.notifications.markAllRead();
  setNotificationUnread(0);
  notificationEvents.emit('all-read');
}

/* ============================================================
   WebSocket wiring
   ============================================================ */

socketEvents.on('unread.update', (payload) => applyUnreadSummary(payload));

socketEvents.on('conversation.unread', (payload) => {
  if (payload?.conversation_id === undefined) return;
  if (
    payload.unread_messages_total !== undefined ||
    payload.global !== undefined ||
    payload.total !== undefined ||
    payload.conversations !== undefined
  ) {
    applyUnreadSummary(payload);
    return;
  }
  const count = payload.unread_count ?? payload.count;
  if (typeof count === 'number' && Number.isFinite(count)) setConversationUnread(payload.conversation_id, count);
});

socketEvents.on('notification.new', (payload) => {
  if (!payload) return;
  notificationEvents.emit('new', payload);
  // A notification event is not a count delta. The server follows it with an
  // authoritative unread.update; only accept an explicit count if provided.
  if (typeof payload.unread_count === 'number') setNotificationUnread(payload.unread_count);
});

socketEvents.on('notification.read', (payload) => {
  if (typeof payload?.unread_count === 'number') setNotificationUnread(payload.unread_count);
});

// Reconnection and refocus both resynchronise from the backend.
socketEvents.on('state', (state) => {
  if (state === 'open') refreshUnread();
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refreshUnread();
});

/* ============================================================
   In-app toast for messages arriving in other conversations
   ============================================================ */

/**
 * @param {object} options { getActiveConversationId, onOpen, previewsEnabled }
 */
export function mountForegroundMessageAlerts(options = {}) {
  const { getActiveConversationId, onOpen, previewsEnabled = true } = options;

  socketEvents.on('message.new', (payload) => {
    const convId = String(payload?.conversation_id ?? payload?.message?.conversation_id ?? '');
    if (!convId) return;

    const message = payload.message || payload;
    if (message?.is_mine) return;

    if (convId === String(getActiveConversationId?.() || '')) return;
    if (document.visibilityState !== 'visible') return; // the SW handles background

    const title = payload.conversation_name || message?.sender?.display_name || 'New message';
    const body = previewsEnabled ? summarize(message) : 'You have a new message.';
    toast(body, {
      type: 'info',
      title,
      action: onOpen ? { label: 'Open', onClick: () => onOpen(convId) } : null,
    });
  });
}

function summarize(message) {
  const kind = String(message?.kind || message?.message_type || 'text').toLowerCase();
  if (kind === 'image') return 'Sent a photo';
  if (kind === 'video') return 'Sent a video';
  if (kind === 'voice' || kind === 'audio') return 'Sent a voice note';
  const text = String(message?.text || message?.body || '');
  return text.length > 120 ? `${text.slice(0, 117)}…` : text || 'New message';
}

export { formatCount };

export default {
  getUnread,
  refreshUnread,
  setConversationUnread,
  clearConversationUnread,
  mountNotificationCentre,
  syncAppBadge,
  unreadEvents,
};
