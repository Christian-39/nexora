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

const unread = {
  total: 0,
  conversations: 0,
  groups: 0,
  notifications: 0,
  /** conversationId -> count */
  byConversation: new Map(),
};

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

function recompute() {
  let conversations = 0;
  let groups = 0;
  for (const [, entry] of unread.byConversation) {
    if (typeof entry === 'number') conversations += entry;
  }
  unread.conversations = conversations;
  unread.groups = groups || unread.groups;
  unread.total = unread.conversations + unread.notifications;
  unreadEvents.emit('change', getUnread());
  syncAppBadge(unread.total);
}

/** Apply the authoritative summary from the backend. */
export function applyUnreadSummary(summary) {
  if (!summary || typeof summary !== 'object') return;

  if (Array.isArray(summary.conversations)) {
    unread.byConversation.clear();
    for (const entry of summary.conversations) {
      const id = String(entry.conversation_id ?? entry.id ?? '');
      if (!id) continue;
      unread.byConversation.set(id, Number(entry.unread_count ?? entry.unread ?? 0) || 0);
    }
  }
  if (typeof summary.total_unread_conversations === 'number') unread.conversations = summary.total_unread_conversations;
  if (typeof summary.total_unread_messages === 'number') unread.conversations = summary.total_unread_messages;
  if (typeof summary.groups_unread === 'number') unread.groups = summary.groups_unread;
  if (typeof summary.notifications_unread === 'number') unread.notifications = summary.notifications_unread;
  if (typeof summary.total === 'number') {
    unread.total = summary.total;
    unreadEvents.emit('change', getUnread());
    syncAppBadge(unread.total);
    return;
  }
  recompute();
}

export function setConversationUnread(conversationId, count) {
  const id = String(conversationId);
  const value = Math.max(0, Number(count) || 0);
  if (unread.byConversation.get(id) === value) return;
  unread.byConversation.set(id, value);
  recompute();
  unreadEvents.emit('conversation', id, value);
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
  setConversationUnread(payload.conversation_id, payload.unread_count ?? payload.count ?? 0);
});

socketEvents.on('notification.new', (payload) => {
  if (!payload) return;
  notificationEvents.emit('new', payload);
  if (typeof payload.unread_count === 'number') setNotificationUnread(payload.unread_count);
  else setNotificationUnread(unread.notifications + 1);
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

    incrementConversationUnread(convId, 1);

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
