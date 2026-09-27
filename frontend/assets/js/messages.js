/**
 * NEXORA — messages.js
 * Message domain store: normalization, cursor pagination, optimistic sends,
 * status reconciliation and WebSocket event integration.
 *
 * Rules enforced here:
 *  - The backend is authoritative. Local status never claims DELIVERED.
 *  - Events may duplicate or arrive out of order; every mutation is idempotent.
 *  - Nothing is rendered from this module (see chat.js).
 */

import { ApiError, api, normalizePage } from './api.js';
import { Emitter, LRU, uid } from './utils.js';
import { socketEvents } from './websocket.js';

export const messageEvents = new Emitter();

/** Backend-authoritative delivery states, plus two local-only states. */
export const STATUS = Object.freeze({
  SENDING: 'sending',
  SENT: 'sent',
  DELIVERED: 'delivered',
  READ: 'read',
  FAILED: 'failed',
  /** Local: submitted but the outcome is unknown (timeout / lost connection). */
  UNCONFIRMED: 'unconfirmed',
});

const STATUS_RANK = {
  [STATUS.FAILED]: -1,
  [STATUS.SENDING]: 0,
  [STATUS.UNCONFIRMED]: 1,
  [STATUS.SENT]: 2,
  [STATUS.DELIVERED]: 3,
  [STATUS.READ]: 4,
};

export const STATUS_LABEL = {
  [STATUS.SENDING]: 'Sending',
  [STATUS.UNCONFIRMED]: 'Message status is being confirmed',
  [STATUS.SENT]: 'Sent',
  [STATUS.DELIVERED]: 'Delivered',
  [STATUS.READ]: 'Read',
  [STATUS.FAILED]: 'Not sent',
};

const PAGE_SIZE = 30;
/** Hard cap on rendered/retained messages per conversation. */
const MAX_RETAINED = 300;

/* ============================================================
   Normalization
   ============================================================ */

/**
 * Convert any backend message shape into the internal model.
 * Unknown fields are preserved under `raw` but never trusted for rendering.
 */
export function normalizeMessage(raw, { currentUserId } = {}) {
  if (!raw || typeof raw !== 'object') return null;
  const id = raw.id ?? raw.uuid ?? raw.message_id;
  const senderId = raw.sender_id ?? raw.sender?.id ?? raw.author?.id ?? null;
  const kind = raw.kind || raw.message_type || raw.type || inferKind(raw);

  return {
    id: id != null ? String(id) : null,
    clientId: raw.client_id ?? raw.client_message_id ?? null,
    conversationId: String(raw.conversation_id ?? raw.conversation ?? raw.thread_id ?? ''),
    kind: normalizeKind(kind),
    text: typeof raw.text === 'string' ? raw.text : typeof raw.body === 'string' ? raw.body : typeof raw.content === 'string' ? raw.content : '',
    caption: typeof raw.caption === 'string' ? raw.caption : '',
    createdAt: raw.created_at ?? raw.timestamp ?? raw.sent_at ?? null,
    editedAt: raw.edited_at ?? null,
    isEdited: !!(raw.is_edited ?? raw.edited_at),
    deletedAt: raw.deleted_at ?? null,
    isDeleted: !!(raw.is_deleted ?? raw.deleted ?? raw.deleted_at),
    status: normalizeStatus(raw.status ?? raw.delivery_status),
    sender: raw.sender || raw.author || (senderId ? { id: senderId } : null),
    senderId: senderId != null ? String(senderId) : null,
    outgoing: currentUserId != null && senderId != null ? String(senderId) === String(currentUserId) : !!raw.is_mine,
    media: normalizeMedia(raw.media || raw.attachment || raw.attachments?.[0] || null),
    replyTo: normalizeReply(raw.reply_to ?? raw.parent ?? raw.reply_to_message ?? null),
    reactions: Array.isArray(raw.reactions) ? raw.reactions : [],
    permissions: {
      canEdit: !!(raw.can_edit ?? raw.permissions?.can_edit),
      canDeleteForSelf: raw.can_delete_for_self ?? raw.permissions?.can_delete_for_self ?? true,
      canDeleteForEveryone: !!(raw.can_delete_for_everyone ?? raw.permissions?.can_delete_for_everyone),
      canReact: !!(raw.can_react ?? raw.permissions?.can_react),
    },
    system: normalizeKind(kind) === 'system',
    local: false,
    error: null,
    progress: null,
  };
}

function inferKind(raw) {
  if (raw.media || raw.attachment) {
    const mime = raw.media?.mime_type || raw.attachment?.mime_type || '';
    if (mime.startsWith('image/')) return 'image';
    if (mime.startsWith('video/')) return 'video';
    if (mime.startsWith('audio/')) return 'voice';
  }
  return 'text';
}

function normalizeKind(kind) {
  const k = String(kind || 'text').toLowerCase();
  if (['image', 'photo'].includes(k)) return 'image';
  if (['video'].includes(k)) return 'video';
  if (['voice', 'audio', 'voice_note'].includes(k)) return 'voice';
  if (['system', 'event', 'notice'].includes(k)) return 'system';
  if (['file', 'document'].includes(k)) return 'file';
  return 'text';
}

function normalizeStatus(status) {
  const s = String(status || '').toLowerCase();
  if (Object.values(STATUS).includes(s)) return s;
  if (s === 'seen') return STATUS.READ;
  if (s === 'pending' || s === 'queued') return STATUS.SENDING;
  if (s === 'error') return STATUS.FAILED;
  return STATUS.SENT;
}

function normalizeMedia(media) {
  if (!media || typeof media !== 'object') return null;
  return {
    id: media.id != null ? String(media.id) : null,
    url: media.url || media.file_url || media.download_url || null,
    thumbnailUrl: media.thumbnail_url || media.poster_url || media.preview_url || null,
    downloadUrl: media.download_url || null,
    mimeType: media.mime_type || media.content_type || '',
    size: Number(media.size ?? media.file_size ?? 0) || 0,
    width: Number(media.width) || null,
    height: Number(media.height) || null,
    duration: Number(media.duration ?? media.duration_seconds ?? 0) || 0,
    name: media.name || media.filename || '',
    status: media.status || 'ready', // backend may report 'processing'
    expiresAt: media.expires_at || null,
  };
}

function normalizeReply(reply) {
  if (!reply) return null;
  if (typeof reply === 'string' || typeof reply === 'number') return { id: String(reply), available: false };
  const id = reply.id ?? reply.message_id;
  if (id == null) return null;
  return {
    id: String(id),
    available: reply.is_deleted ? false : reply.text !== undefined || reply.kind !== undefined || reply.preview !== undefined,
    authorName: reply.sender?.display_name || reply.sender?.name || reply.author_name || '',
    preview: typeof reply.preview === 'string' ? reply.preview : typeof reply.text === 'string' ? reply.text : '',
    kind: normalizeKind(reply.kind || reply.message_type),
  };
}

/** Short text used for conversation-list previews and reply references. */
export function messagePreview(message) {
  if (!message) return '';
  if (message.isDeleted) return 'Message deleted';
  switch (message.kind) {
    case 'image': return message.caption || 'Photo';
    case 'video': return message.caption || 'Video';
    case 'voice': return 'Voice note';
    case 'file': return message.media?.name || 'Attachment';
    case 'system': return message.text || '';
    default: return message.text || '';
  }
}

/* ============================================================
   Per-conversation store
   ============================================================ */

class ConversationMessages {
  constructor(conversationId) {
    this.id = String(conversationId);
    /** Ascending by time; the newest message is last. */
    this.items = [];
    this.byId = new Map();
    this.byClientId = new Map();
    this.cursor = null;          // backend cursor/URL for older messages
    this.hasMore = true;
    this.loading = false;
    this.loadingOlder = false;
    this.loadedOnce = false;
    this.error = null;
    this.trimmedNewer = false;   // set when we dropped newest items (unused today)
  }

  index(message) {
    if (message.id) this.byId.set(message.id, message);
    if (message.clientId) this.byClientId.set(message.clientId, message);
  }

  find(idOrClientId) {
    if (idOrClientId == null) return null;
    const key = String(idOrClientId);
    return this.byId.get(key) || this.byClientId.get(key) || null;
  }

  /** Insert keeping ascending order; returns the stored message. */
  insert(message) {
    const existing = this.find(message.id) || (message.clientId ? this.byClientId.get(message.clientId) : null);
    if (existing) return this.merge(existing, message);

    const ts = timeOf(message);
    let i = this.items.length;
    while (i > 0 && timeOf(this.items[i - 1]) > ts) i -= 1;
    this.items.splice(i, 0, message);
    this.index(message);
    this.#trim();
    return message;
  }

  /** Idempotent merge: server data wins, status only ever moves forward. */
  merge(target, incoming) {
    const nextStatus =
      (STATUS_RANK[incoming.status] ?? 0) >= (STATUS_RANK[target.status] ?? 0) ? incoming.status : target.status;

    // Reindex if the optimistic placeholder just received its server id.
    if (incoming.id && incoming.id !== target.id) {
      if (target.id) this.byId.delete(target.id);
      this.byId.set(incoming.id, target);
    }

    Object.assign(target, incoming, {
      status: nextStatus,
      clientId: target.clientId || incoming.clientId,
      local: false,
      error: incoming.error ?? null,
      progress: null,
    });
    if (target.clientId) this.byClientId.set(target.clientId, target);
    return target;
  }

  remove(id) {
    const message = this.find(id);
    if (!message) return null;
    const idx = this.items.indexOf(message);
    if (idx >= 0) this.items.splice(idx, 1);
    if (message.id) this.byId.delete(message.id);
    if (message.clientId) this.byClientId.delete(message.clientId);
    return message;
  }

  /** Bound memory: drop the oldest messages once past the retention cap. */
  #trim() {
    if (this.items.length <= MAX_RETAINED) return;
    const overflow = this.items.length - MAX_RETAINED;
    const dropped = this.items.splice(0, overflow);
    for (const m of dropped) {
      if (m.id) this.byId.delete(m.id);
      if (m.clientId) this.byClientId.delete(m.clientId);
    }
    // We no longer hold the true head of history.
    this.hasMore = true;
  }

  get last() {
    for (let i = this.items.length - 1; i >= 0; i -= 1) {
      if (!this.items[i].isDeleted) return this.items[i];
    }
    return this.items[this.items.length - 1] || null;
  }
}

function timeOf(message) {
  const t = Date.parse(message.createdAt || '');
  return Number.isNaN(t) ? Date.now() : t;
}

/* ============================================================
   Store facade
   ============================================================ */

const stores = new LRU(12); // keep a handful of recent conversations in memory
let currentUserId = null;

export function setCurrentUser(userId) {
  currentUserId = userId != null ? String(userId) : null;
}

export function getStore(conversationId) {
  const key = String(conversationId);
  let store = stores.get(key);
  if (!store) {
    store = new ConversationMessages(key);
    stores.set(key, store);
  }
  return store;
}

export function dropStore(conversationId) {
  stores.delete(String(conversationId));
}

export function clearAllStores() {
  stores.clear();
}

/* ---------------- Loading ---------------- */

/**
 * Load the most recent page for a conversation.
 * @returns {Promise<{items:Array, hasMore:boolean}>}
 */
export async function loadLatest(conversationId, { signal, force = false } = {}) {
  const store = getStore(conversationId);
  if (store.loading) return { items: store.items, hasMore: store.hasMore };
  if (store.loadedOnce && !force) return { items: store.items, hasMore: store.hasMore };

  store.loading = true;
  store.error = null;
  messageEvents.emit('loading', store.id, true);
  try {
    const response = await api.conversations.messages(conversationId, { limit: PAGE_SIZE }, { signal });
    const page = normalizePage(response.data ?? response);
    ingestPage(store, page, { replace: force });
    store.loadedOnce = true;
    store.cursor = page.next;
    store.hasMore = !!page.next;
    messageEvents.emit('loaded', store.id, store.items);
    return { items: store.items, hasMore: store.hasMore };
  } catch (error) {
    store.error = error instanceof ApiError ? error : new ApiError({ message: 'Unable to load messages.' });
    messageEvents.emit('error', store.id, store.error);
    throw store.error;
  } finally {
    store.loading = false;
    messageEvents.emit('loading', store.id, false);
  }
}

/**
 * Load one page of older messages (scroll-up / infinite history).
 * @returns {Promise<{added:number, hasMore:boolean}>}
 */
export async function loadOlder(conversationId, { signal } = {}) {
  const store = getStore(conversationId);
  if (store.loadingOlder || !store.hasMore) return { added: 0, hasMore: store.hasMore };

  store.loadingOlder = true;
  messageEvents.emit('loading-older', store.id, true);
  try {
    let response;
    if (store.cursor && /^https?:\/\//i.test(store.cursor)) {
      // The backend returned an absolute pagination link — follow it directly.
      response = await followCursor(store.cursor, signal);
    } else {
      const before = store.items[0]?.id;
      response = await api.conversations.messages(
        conversationId,
        { limit: PAGE_SIZE, cursor: store.cursor || undefined, before: store.cursor ? undefined : before },
        { signal }
      );
    }
    const page = normalizePage(response.data ?? response);
    const added = ingestPage(store, page, { prepend: true });
    store.cursor = page.next;
    store.hasMore = !!page.next && added >= 0 && page.items.length > 0;
    messageEvents.emit('older', store.id, added);
    return { added, hasMore: store.hasMore };
  } catch (error) {
    messageEvents.emit('error', store.id, error);
    throw error;
  } finally {
    store.loadingOlder = false;
    messageEvents.emit('loading-older', store.id, false);
  }
}

async function followCursor(url, signal) {
  const { fetchPageUrl } = await import('./api.js');
  return fetchPageUrl(url, { signal });
}

function ingestPage(store, page, { replace = false, prepend = false } = {}) {
  if (replace) {
    store.items = [];
    store.byId.clear();
    store.byClientId.clear();
  }
  let added = 0;
  for (const raw of page.items) {
    const message = normalizeMessage(raw, { currentUserId });
    if (!message) continue;
    const existed = !!store.find(message.id);
    store.insert(message);
    if (!existed) added += 1;
  }
  void prepend;
  return added;
}

/* ---------------- Mutations ---------------- */

/** Create an optimistic local message shown immediately as SENDING. */
export function createOptimistic(conversationId, { kind = 'text', text = '', caption = '', media = null, replyTo = null, sender = null }) {
  const message = {
    id: null,
    clientId: uid('msg'),
    conversationId: String(conversationId),
    kind,
    text,
    caption,
    createdAt: new Date().toISOString(),
    editedAt: null,
    isEdited: false,
    deletedAt: null,
    isDeleted: false,
    status: STATUS.SENDING,
    sender,
    senderId: currentUserId,
    outgoing: true,
    media,
    replyTo,
    reactions: [],
    permissions: { canEdit: false, canDeleteForSelf: true, canDeleteForEveryone: false, canReact: false },
    system: false,
    local: true,
    error: null,
    progress: null,
  };
  const store = getStore(conversationId);
  store.insert(message);
  messageEvents.emit('added', store.id, message);
  return message;
}

/** Apply the server's authoritative version of an optimistic message. */
export function confirmOptimistic(conversationId, clientId, serverMessage) {
  const store = getStore(conversationId);
  const normalized = normalizeMessage(serverMessage, { currentUserId });
  if (!normalized) return null;
  if (!normalized.clientId) normalized.clientId = clientId;
  const existing = store.byClientId.get(clientId);
  const result = existing ? store.merge(existing, normalized) : store.insert(normalized);
  messageEvents.emit('updated', store.id, result);
  return result;
}

export function markLocalFailed(conversationId, clientId, error) {
  const store = getStore(conversationId);
  const message = store.byClientId.get(clientId);
  if (!message) return null;
  message.status = STATUS.FAILED;
  message.error = error?.message || 'Message not sent.';
  message.progress = null;
  messageEvents.emit('updated', store.id, message);
  return message;
}

/** Move a failed/unconfirmed local message back to SENDING (used on retry). */
export function markLocalSending(conversationId, clientId) {
  const store = getStore(conversationId);
  const message = store.byClientId.get(clientId);
  if (!message) return null;
  message.status = STATUS.SENDING;
  message.error = null;
  messageEvents.emit('updated', store.id, message);
  return message;
}

/** Outcome unknown (timeout / connection lost) — never claim delivery. */
export function markLocalUnconfirmed(conversationId, clientId) {
  const store = getStore(conversationId);
  const message = store.byClientId.get(clientId);
  if (!message) return null;
  message.status = STATUS.UNCONFIRMED;
  message.error = null;
  messageEvents.emit('updated', store.id, message);
  return message;
}

export function setLocalProgress(conversationId, clientId, progress) {
  const store = getStore(conversationId);
  const message = store.byClientId.get(clientId);
  if (!message) return null;
  message.progress = progress;
  messageEvents.emit('progress', store.id, message);
  return message;
}

export function removeLocal(conversationId, clientId) {
  const store = getStore(conversationId);
  const removed = store.remove(clientId);
  if (removed) messageEvents.emit('removed', store.id, removed);
  return removed;
}

/* ---------------- Sending ---------------- */

/**
 * Send a text message with optimistic rendering and duplicate protection.
 * The backend deduplicates on client_id, so a retry after an uncertain
 * outcome can never create two messages.
 */
export async function sendText(conversationId, text, { replyTo = null, sender = null } = {}) {
  const optimistic = createOptimistic(conversationId, {
    kind: 'text',
    text,
    replyTo: replyTo ? { id: String(replyTo.id), available: true, authorName: replyTo.authorName, preview: replyTo.preview, kind: replyTo.kind } : null,
    sender,
  });
  return deliverText(conversationId, optimistic, { text, replyToId: replyTo?.id ?? null });
}

/** Retry a failed/unconfirmed text message using the SAME client id. */
export async function retryText(conversationId, clientId) {
  const store = getStore(conversationId);
  const message = store.byClientId.get(clientId);
  if (!message || message.kind !== 'text') return null;
  message.status = STATUS.SENDING;
  message.error = null;
  messageEvents.emit('updated', store.id, message);
  return deliverText(conversationId, message, { text: message.text, replyToId: message.replyTo?.id ?? null });
}

async function deliverText(conversationId, optimistic, { text, replyToId }) {
  try {
    const payload = {
      kind: 'text',
      text,
      client_id: optimistic.clientId,
    };
    if (replyToId) payload.reply_to = replyToId;
    const created = await api.conversations.send(conversationId, payload, { timeout: 15000 });
    return confirmOptimistic(conversationId, optimistic.clientId, created);
  } catch (error) {
    if (error instanceof ApiError && (error.isTimeout || error.isNetwork || error.isOffline)) {
      // The server may well have accepted it — reconcile later.
      markLocalUnconfirmed(conversationId, optimistic.clientId);
      scheduleReconciliation(conversationId);
    } else {
      markLocalFailed(conversationId, optimistic.clientId, error);
    }
    throw error;
  }
}

/* ---------------- Editing / deletion / reactions ---------------- */

export async function editMessage(conversationId, messageId, text) {
  const updated = await api.messages.edit(messageId, { text });
  return applyServerMessage(conversationId, updated);
}

export async function deleteMessage(conversationId, messageId, scope = 'self') {
  await api.messages.remove(messageId, scope);
  const store = getStore(conversationId);
  const message = store.find(messageId);
  if (message) {
    if (scope === 'everyone') {
      message.isDeleted = true;
      message.text = '';
      message.media = null;
      messageEvents.emit('updated', store.id, message);
    } else {
      store.remove(messageId);
      messageEvents.emit('removed', store.id, message);
    }
  }
}

const reactionLocks = new Set();

export async function toggleReaction(conversationId, messageId, reaction, mine) {
  const lockKey = `${messageId}:${reaction}`;
  if (reactionLocks.has(lockKey)) return null; // prevent duplicate submissions
  reactionLocks.add(lockKey);
  try {
    const updated = mine
      ? await api.messages.unreact(messageId, reaction)
      : await api.messages.react(messageId, reaction);
    if (updated) return applyServerMessage(conversationId, updated);
    // Backend returned no body — refetch authoritative state.
    const fresh = await api.messages.get(messageId);
    return applyServerMessage(conversationId, fresh);
  } finally {
    reactionLocks.delete(lockKey);
  }
}

/* ---------------- Server event application ---------------- */

export function applyServerMessage(conversationId, raw) {
  const store = getStore(conversationId);
  const normalized = normalizeMessage(raw, { currentUserId });
  if (!normalized) return null;
  const existed = !!store.find(normalized.id) || !!(normalized.clientId && store.byClientId.get(normalized.clientId));
  const message = store.insert(normalized);
  messageEvents.emit(existed ? 'updated' : 'added', store.id, message);
  return message;
}

export function applyStatusUpdate(conversationId, messageId, status) {
  const store = getStore(conversationId);
  const message = store.find(messageId);
  if (!message) return null;
  const next = normalizeStatus(status);
  if ((STATUS_RANK[next] ?? 0) <= (STATUS_RANK[message.status] ?? 0)) return message;
  message.status = next;
  messageEvents.emit('updated', store.id, message);
  return message;
}

/** Bulk read receipt: everything up to `messageId` becomes READ. */
export function applyReadUpTo(conversationId, messageId) {
  const store = getStore(conversationId);
  const anchor = store.find(messageId);
  const anchorTime = anchor ? timeOf(anchor) : Date.now();
  let changed = false;
  for (const message of store.items) {
    if (!message.outgoing) continue;
    if (timeOf(message) > anchorTime) continue;
    if ((STATUS_RANK[message.status] ?? 0) < STATUS_RANK[STATUS.READ]) {
      message.status = STATUS.READ;
      changed = true;
      messageEvents.emit('updated', store.id, message);
    }
  }
  return changed;
}

/* ---------------- Reconciliation ---------------- */

const pendingReconcile = new Set();

/**
 * After an uncertain outcome or a reconnection, re-fetch the newest page and
 * let client_id matching resolve anything that actually made it through.
 */
export function scheduleReconciliation(conversationId, delay = 1500) {
  const key = String(conversationId);
  if (pendingReconcile.has(key)) return;
  pendingReconcile.add(key);
  setTimeout(async () => {
    pendingReconcile.delete(key);
    try {
      await reconcile(key);
    } catch {
      /* the next reconnection will try again */
    }
  }, delay);
}

export async function reconcile(conversationId) {
  const store = getStore(conversationId);
  if (!store.loadedOnce) return;
  const response = await api.conversations.messages(conversationId, { limit: PAGE_SIZE });
  const page = normalizePage(response.data ?? response);
  const seenClientIds = new Set();
  for (const raw of page.items) {
    const message = normalizeMessage(raw, { currentUserId });
    if (!message) continue;
    if (message.clientId) seenClientIds.add(message.clientId);
    store.insert(message);
  }
  // Anything still 'unconfirmed' and absent from the server never landed.
  for (const message of store.items) {
    if (message.status === STATUS.UNCONFIRMED && message.clientId && !seenClientIds.has(message.clientId)) {
      message.status = STATUS.FAILED;
      message.error = 'Message was not delivered. Tap to retry.';
      messageEvents.emit('updated', store.id, message);
    }
  }
  messageEvents.emit('reconciled', store.id, store.items);
}

/* ============================================================
   WebSocket wiring — every handler is idempotent
   ============================================================ */

function convIdOf(payload) {
  return String(payload?.conversation_id ?? payload?.conversation ?? payload?.message?.conversation_id ?? '');
}

socketEvents.on('message.new', (payload) => {
  const convId = convIdOf(payload);
  if (!convId) return;
  const raw = payload.message || payload;
  const store = stores.get(convId);
  // Only materialise into a store we're actually tracking; list-level updates
  // are handled by chat.js from the same event.
  if (store) applyServerMessage(convId, raw);
  messageEvents.emit('incoming', convId, raw);
});

socketEvents.on('message.updated', (payload) => {
  const convId = convIdOf(payload);
  if (!convId || !stores.get(convId)) return;
  applyServerMessage(convId, payload.message || payload);
});

socketEvents.on('message.edited', (payload) => {
  const convId = convIdOf(payload);
  if (!convId || !stores.get(convId)) return;
  applyServerMessage(convId, payload.message || payload);
});

socketEvents.on('message.deleted', (payload) => {
  const convId = convIdOf(payload);
  if (!convId) return;
  const store = stores.get(convId);
  if (!store) return;
  const messageId = String(payload.message_id ?? payload.id ?? '');
  const message = store.find(messageId);
  if (!message) return;
  if (payload.scope === 'everyone' || payload.for_everyone) {
    message.isDeleted = true;
    message.text = '';
    message.media = null;
    messageEvents.emit('updated', convId, message);
  } else {
    store.remove(messageId);
    messageEvents.emit('removed', convId, message);
  }
});

socketEvents.on('message.delivered', (payload) => {
  const convId = convIdOf(payload);
  const ids = [].concat(payload.message_ids || payload.message_id || []);
  for (const id of ids) applyStatusUpdate(convId, String(id), STATUS.DELIVERED);
});

socketEvents.on('message.read', (payload) => {
  const convId = convIdOf(payload);
  if (payload.up_to || payload.message_id) {
    applyReadUpTo(convId, String(payload.up_to ?? payload.message_id));
  }
  const ids = [].concat(payload.message_ids || []);
  for (const id of ids) applyStatusUpdate(convId, String(id), STATUS.READ);
});

socketEvents.on('message.reaction', (payload) => {
  const convId = convIdOf(payload);
  if (!convId || !stores.get(convId)) return;
  if (payload.message) {
    applyServerMessage(convId, payload.message);
    return;
  }
  const store = getStore(convId);
  const message = store.find(String(payload.message_id));
  if (message && Array.isArray(payload.reactions)) {
    message.reactions = payload.reactions;
    messageEvents.emit('updated', convId, message);
  }
});

socketEvents.on('media.ready', (payload) => {
  const convId = convIdOf(payload);
  if (!convId || !stores.get(convId)) return;
  if (payload.message) applyServerMessage(convId, payload.message);
});

// Reconnection: WebSocket gaps are filled from REST, never assumed away.
socketEvents.on('state', (state) => {
  if (state !== 'open') return;
  for (const [convId, store] of stores.map) {
    if (store.loadedOnce) scheduleReconciliation(convId, 300);
  }
});

export default {
  STATUS,
  STATUS_LABEL,
  getStore,
  loadLatest,
  loadOlder,
  sendText,
  retryText,
  editMessage,
  deleteMessage,
  toggleReaction,
  messageEvents,
};
