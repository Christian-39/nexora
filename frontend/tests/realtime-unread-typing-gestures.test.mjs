/** Typing self-filter, authoritative unread/badge updates, and mobile long press. */

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { setNavigator } from './helpers/browser-env.mjs';

function installEnvironment() {
  globalThis.location = {
    hostname: 'frontend.example.test', port: '', protocol: 'https:',
    origin: 'https://frontend.example.test', href: 'https://frontend.example.test/chat.html',
  };
  const listeners = new Map();
  globalThis.window = {
    location: globalThis.location,
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    removeEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {} }),
  };
  const makeElement = () => ({
    dataset: {},
    style: {},
    classList: { add() {}, remove() {}, contains: () => false },
    setAttribute() {},
    append() {},
    remove() {},
  });
  globalThis.document = {
    cookie: '',
    visibilityState: 'visible',
    documentElement: { dataset: {}, style: { setProperty() {} } },
    body: { dataset: {}, append() {} },
    head: { append() {} },
    addEventListener() {},
    removeEventListener() {},
    getElementById: () => null,
    querySelector: () => null,
    createElement: makeElement,
  };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
  const badgeCalls = [];
  setNavigator({
    onLine: true,
    setAppBadge: async (count) => badgeCalls.push(['set', count]),
    clearAppBadge: async () => badgeCalls.push(['clear']),
  });
  const storage = new Map();
  globalThis.sessionStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  };
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: 'https://api.example.test' };
  globalThis.fetch = async () => { throw new Error('unexpected network request'); };
  return badgeCalls;
}

const badgeCalls = installEnvironment();
sessionStorage.setItem('nexora.sessionProfile.v1', JSON.stringify({
  id: 'self-user', full_name: 'Current User', display_name: 'Current User', role: 'member',
}));
const { getUser } = await import('../assets/js/auth.js');
const { socketEvents } = await import('../assets/js/websocket.js');
const presence = await import('../assets/js/presence.js');
const notifications = await import('../assets/js/notifications.js');
const { ChatController } = await import('../assets/js/chat.js');

class FakeRow {
  constructor() {
    this.handlers = new Map();
    this.classes = new Set();
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
    };
    this.isConnected = true;
  }
  addEventListener(type, handler) {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type).push(handler);
  }
  dispatch(type, event = {}) {
    for (const handler of this.handlers.get(type) || []) handler(event);
  }
}

const touch = (x, y) => ({ touches: [{ clientX: x, clientY: y }], target: { closest: () => null } });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

 test('typing frames from the current user never enter the visible typing state', () => {
  assert.equal(getUser()?.id, 'self-user');
  presence.clearAllTyping();
  const conversationId = 'typing-self-filter';

  socketEvents.emit('typing.start', { conversation_id: conversationId, user_id: 'self-user' });
  assert.deepEqual(presence.getTypingUsers(conversationId), []);

  socketEvents.emit('typing.start', {
    conversation_id: conversationId,
    user_id: 'peer-user',
    user: { display_name: 'A peer' },
  });
  assert.deepEqual(presence.getTypingUsers(conversationId), [{ id: 'peer-user', name: 'A peer' }]);
  assert.equal(presence.typingLabel(conversationId), 'A peer is typing…');

  socketEvents.emit('typing.stop', { conversation_id: conversationId, user_id: 'peer-user' });
  assert.deepEqual(presence.getTypingUsers(conversationId), []);
  presence.clearAllTyping();
});

test('full unread snapshots and explicit conversation updates remain authoritative and drive the PWA badge', async () => {
  badgeCalls.length = 0;
  notifications.applyUnreadSummary({
    global: 5,
    unread_messages_total: 5,
    notifications_unread: 2,
    unread_total: 7,
    conversations: { 'conversation-one': 3, 'conversation-two': 2 },
  });
  assert.deepEqual(notifications.getUnread(), { total: 7, conversations: 5, groups: 0, notifications: 2 });
  assert.equal(notifications.getConversationUnread('conversation-one'), 3);

  socketEvents.emit('conversation.unread', { conversation_id: 'conversation-one', unread_count: 1 });
  assert.deepEqual(notifications.getUnread(), { total: 5, conversations: 3, groups: 0, notifications: 2 });

  // A missing per-conversation count is not interpreted as zero.
  socketEvents.emit('conversation.unread', { conversation_id: 'conversation-two' });
  assert.equal(notifications.getConversationUnread('conversation-two'), 2);
  assert.equal(notifications.getUnread().total, 5);

  socketEvents.emit('unread.update', {
    global: 2,
    unread_messages_total: 2,
    notifications_unread: 1,
    unread_total: 3,
    conversations: { 'conversation-two': 2 },
  });
  assert.deepEqual(notifications.getUnread(), { total: 3, conversations: 2, groups: 0, notifications: 1 });
  await notifications.syncAppBadge();
  await notifications.syncAppBadge(0);
  assert.ok(badgeCalls.some(([kind, count]) => kind === 'set' && count === 3));
  assert.ok(badgeCalls.some(([kind]) => kind === 'clear'));
});

test('stationary 500ms touch highlights a whole row; scrolling, swiping, taps and controls do not trigger it', async () => {
  const controller = Object.create(ChatController.prototype);
  controller.selectedMessageRow = null;
  controller.selectedMessageKey = null;

  const first = new FakeRow();
  controller.bindLongPressHighlight(first, { id: 'message-one', clientId: 'client-one' });
  first.dispatch('touchstart', touch(20, 30));
  first.dispatch('touchend', { touches: [] });
  await delay(520);
  assert.equal(first.classList.contains('msg-row--long-pressed'), false, 'a tap does not select');

  first.dispatch('touchstart', touch(20, 30));
  await delay(520);
  assert.equal(first.classList.contains('msg-row--long-pressed'), true);
  assert.strictEqual(controller.selectedMessageRow, first);
  assert.equal(controller.selectedMessageKey, 'message-one');

  const second = new FakeRow();
  controller.bindLongPressHighlight(second, { id: 'message-two', clientId: 'client-two' });
  second.dispatch('touchstart', touch(40, 40));
  await delay(520);
  assert.equal(first.classList.contains('msg-row--long-pressed'), false, 'only one whole row remains highlighted');
  assert.equal(second.classList.contains('msg-row--long-pressed'), true);

  const vertical = new FakeRow();
  controller.bindLongPressHighlight(vertical, { id: 'vertical-scroll' });
  vertical.dispatch('touchstart', touch(10, 10));
  vertical.dispatch('touchmove', { touches: [{ clientX: 10, clientY: 24 }] });
  await delay(520);
  assert.equal(vertical.classList.contains('msg-row--long-pressed'), false, 'vertical scrolling cancels selection');

  const horizontal = new FakeRow();
  controller.bindLongPressHighlight(horizontal, { id: 'horizontal-swipe' });
  horizontal.dispatch('touchstart', touch(10, 10));
  horizontal.dispatch('touchmove', { touches: [{ clientX: 24, clientY: 10 }] });
  await delay(520);
  assert.equal(horizontal.classList.contains('msg-row--long-pressed'), false, 'horizontal swipe-to-reply movement cancels selection');

  const control = new FakeRow();
  controller.bindLongPressHighlight(control, { id: 'control' });
  control.dispatch('touchstart', {
    touches: [{ clientX: 10, clientY: 10 }],
    target: { closest: (selector) => selector.includes('button') ? {} : null },
  });
  await delay(520);
  assert.equal(control.classList.contains('msg-row--long-pressed'), false, 'buttons, links and media controls do not trigger row selection');

  const chatSource = await readFile(new URL('../assets/js/chat.js', import.meta.url), 'utf8');
  assert.match(chatSource, /this\.bindSwipeToReply\(row,\s*bubble,\s*message\)/, 'the existing swipe-to-reply binding remains installed');
});
