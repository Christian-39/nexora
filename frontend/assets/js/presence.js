/**
 * NEXORA — presence.js
 * Presence + typing state, driven entirely by WebSocket events.
 * No polling. Stale typing indicators expire locally; typing is never persisted.
 * Privacy flags from the backend are respected: when presence is hidden the
 * store reports 'hidden' and the UI shows nothing.
 */

import { Emitter } from './utils.js';
import { getUser } from './auth.js';
import { socketEvents } from './websocket.js';

const TYPING_TTL = 6000; // clear if no refresh arrives
const PRESENCE_TTL = 90000; // treat stale online flags as unknown

export const presenceEvents = new Emitter();

/** userId -> { status:'online'|'offline'|'hidden', lastSeen, at } */
const presenceMap = new Map();
/** conversationId -> Map(userId -> { name, timer }) */
const typingMap = new Map();

/* ============================================================
   Presence
   ============================================================ */

export function setPresence(userId, data = {}) {
  if (!userId) return;
  const hidden = data.hidden === true || data.presence_visible === false;
  const status = hidden ? 'hidden' : data.online || data.status === 'online' ? 'online' : 'offline';
  const record = {
    status,
    lastSeen: data.last_seen ?? data.lastSeen ?? null,
    at: Date.now(),
  };
  presenceMap.set(String(userId), record);
  presenceEvents.emit('presence', String(userId), record);
}

/** @returns {{status:'online'|'offline'|'hidden'|'unknown', lastSeen:string|null}} */
export function getPresence(userId) {
  const record = presenceMap.get(String(userId));
  if (!record) return { status: 'unknown', lastSeen: null };
  if (record.status === 'online' && Date.now() - record.at > PRESENCE_TTL) {
    return { status: 'unknown', lastSeen: record.lastSeen };
  }
  return { status: record.status, lastSeen: record.lastSeen };
}

/** Seed presence from a REST payload (conversation/member list). */
export function seedPresence(entities = []) {
  for (const entity of entities) {
    if (!entity?.id) continue;
    if (entity.presence_visible === false) {
      presenceMap.set(String(entity.id), { status: 'hidden', lastSeen: null, at: Date.now() });
      continue;
    }
    if (entity.online === undefined && entity.last_seen === undefined) continue;
    presenceMap.set(String(entity.id), {
      status: entity.online ? 'online' : 'offline',
      lastSeen: entity.last_seen ?? null,
      at: Date.now(),
    });
  }
}

/** Human label honouring privacy configuration. */
export function presenceLabel(userId, { formatRelative } = {}) {
  const { status, lastSeen } = getPresence(userId);
  if (status === 'hidden' || status === 'unknown') return '';
  if (status === 'online') return 'Online';
  if (!lastSeen) return 'Offline';
  if (formatRelative) return `Last seen ${formatRelative(lastSeen)}`;
  return 'Offline';
}

/* ============================================================
   Typing
   ============================================================ */

export function setTyping(conversationId, user, isTyping) {
  if (!conversationId || !user?.id) return;
  const convId = String(conversationId);
  const userId = String(user.id);
  const currentUserId = getUser()?.id;
  if (currentUserId != null && userId === String(currentUserId)) return;

  if (!typingMap.has(convId)) typingMap.set(convId, new Map());
  const bucket = typingMap.get(convId);
  const existing = bucket.get(userId);

  if (!isTyping) {
    if (existing) {
      clearTimeout(existing.timer);
      bucket.delete(userId);
      emitTyping(convId);
    }
    return;
  }

  if (existing) clearTimeout(existing.timer);
  const timer = setTimeout(() => {
    bucket.delete(userId);
    emitTyping(convId);
  }, TYPING_TTL);
  bucket.set(userId, { name: user.display_name || user.name || 'Someone', timer });
  emitTyping(convId);
}

function emitTyping(conversationId) {
  presenceEvents.emit('typing', conversationId, getTypingUsers(conversationId));
}

/** @returns {Array<{id:string,name:string}>} */
export function getTypingUsers(conversationId) {
  const bucket = typingMap.get(String(conversationId));
  if (!bucket) return [];
  return Array.from(bucket.entries()).map(([id, value]) => ({ id, name: value.name }));
}

export function typingLabel(conversationId, { isGroup = false } = {}) {
  const users = getTypingUsers(conversationId);
  if (!users.length) return '';
  if (!isGroup) return `${users[0].name} is typing…`;
  if (users.length === 1) return `${users[0].name} is typing…`;
  if (users.length === 2) return `${users[0].name} and ${users[1].name} are typing…`;
  return `${users.length} people are typing…`;
}

export function clearTyping(conversationId) {
  const bucket = typingMap.get(String(conversationId));
  if (!bucket) return;
  for (const entry of bucket.values()) clearTimeout(entry.timer);
  bucket.clear();
  emitTyping(conversationId);
}

export function clearAllTyping() {
  for (const id of typingMap.keys()) clearTyping(id);
}

/* ============================================================
   WebSocket wiring
   ============================================================ */

socketEvents.on('presence.update', (payload) => {
  const users = payload?.users || (payload?.user_id ? [payload] : []);
  for (const entry of [].concat(users)) {
    setPresence(entry.user_id ?? entry.id, entry);
  }
});

socketEvents.on('presence.online', (payload) => setPresence(payload?.user_id ?? payload?.id, { ...payload, online: true }));
socketEvents.on('presence.offline', (payload) => setPresence(payload?.user_id ?? payload?.id, { ...payload, online: false }));

socketEvents.on('typing', (payload) => {
  setTyping(payload?.conversation_id, { id: payload?.user_id ?? payload?.user?.id, ...(payload?.user || {}) }, payload?.typing !== false);
});
socketEvents.on('typing.start', (payload) => {
  setTyping(payload?.conversation_id, { id: payload?.user_id ?? payload?.user?.id, ...(payload?.user || {}) }, true);
});
socketEvents.on('typing.stop', (payload) => {
  setTyping(payload?.conversation_id, { id: payload?.user_id ?? payload?.user?.id, ...(payload?.user || {}) }, false);
});

// Connection loss invalidates ephemeral state: never show stale indicators.
socketEvents.on('state', (state) => {
  if (state === 'open') return;
  clearAllTyping();
  for (const [id, record] of presenceMap) {
    if (record.status === 'online') presenceMap.set(id, { ...record, status: 'offline', at: Date.now() });
  }
  presenceEvents.emit('reset');
});

export default { getPresence, setPresence, seedPresence, presenceLabel, getTypingUsers, typingLabel, presenceEvents };
