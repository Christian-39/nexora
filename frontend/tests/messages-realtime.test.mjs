/** Realtime/HTTP optimistic reconciliation and stable-key retry contracts. */

import assert from 'node:assert/strict';
import test from 'node:test';
import { setNavigator } from './helpers/browser-env.mjs';

const API_MODULE = new URL('../assets/js/api.js', import.meta.url).href;
const MESSAGES_MODULE = new URL('../assets/js/messages.js', import.meta.url).href;
const SOCKET_MODULE = new URL('../assets/js/websocket.js', import.meta.url).href;

function installEnvironment() {
  globalThis.location = {
    hostname: 'frontend.example.test', port: '', protocol: 'https:',
    origin: 'https://frontend.example.test', href: 'https://frontend.example.test/chat.html',
  };
  globalThis.window = {
    location: globalThis.location,
    addEventListener() {},
    removeEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {} }),
  };
  globalThis.document = {
    cookie: '',
    visibilityState: 'visible',
    addEventListener() {},
    removeEventListener() {},
    getElementById: () => null,
    querySelector: () => null,
  };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
  setNavigator({ onLine: true });
  const storage = new Map();
  globalThis.sessionStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  };
  globalThis.NEXORA_RUNTIME = { API_BASE_URL: 'https://api.example.test' };
  globalThis.fetch = async () => { throw new Error('unexpected network request'); };
}

installEnvironment();
const { api } = await import(API_MODULE);
const messages = await import(MESSAGES_MODULE);
const { socketEvents } = await import(SOCKET_MODULE);

function authoritativeMessage({ id, clientId, conversationId, text, status = 'sent', createdAt = new Date().toISOString() }) {
  return {
    id,
    client_id: clientId,
    conversation_id: conversationId,
    sender_id: 'user-a',
    sender: { id: 'user-a', display_name: 'Sender' },
    type: 'TEXT',
    text,
    status,
    created_at: createdAt,
    reactions: [],
    delivery: { state: status.toUpperCase(), recipients: 1, delivered: 0, read: 0 },
  };
}

test('optimistic text renders immediately and one socket/HTTP race reconciles to one authoritative row', async () => {
  messages.clearAllStores();
  messages.setCurrentUser('user-a');
  const sent = [];
  const originalSend = api.conversations.send;
  let resolveHttp;
  api.conversations.send = async (_conversationId, payload) => {
    sent.push(payload);
    return new Promise((resolve) => { resolveHttp = resolve; });
  };

  const events = [];
  const offAdded = messages.messageEvents.on('added', (_conversationId, message) => events.push(['added', message]));
  const offUpdated = messages.messageEvents.on('updated', (_conversationId, message) => events.push(['updated', message]));
  try {
    const httpPromise = messages.sendText('conversation-live', 'hello in real time');
    const store = messages.getStore('conversation-live');
    assert.equal(store.items.length, 1, 'the optimistic row is inserted before the HTTP request resolves');
    const optimistic = store.items[0];
    assert.equal(optimistic.status, messages.STATUS.SENDING);
    assert.equal(events.filter(([type]) => type === 'added').length, 1);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].client_id, optimistic.clientId);

    const server = authoritativeMessage({
      id: 'server-message-1',
      clientId: optimistic.clientId,
      conversationId: 'conversation-live',
      text: 'hello in real time',
    });
    socketEvents.emit('message.new', server);

    assert.equal(store.items.length, 1, 'the WebSocket message upserts the optimistic row rather than appending');
    assert.strictEqual(store.find(optimistic.clientId), optimistic);
    assert.equal(optimistic.id, 'server-message-1');
    assert.equal(optimistic.status, messages.STATUS.SENT);
    assert.equal(events.filter(([type]) => type === 'added').length, 1);
    assert.equal(events.filter(([type]) => type === 'updated').length, 1);

    resolveHttp(server);
    const confirmed = await httpPromise;
    assert.strictEqual(confirmed, optimistic, 'the later HTTP response reconciles to the same row');
    assert.equal(store.items.length, 1);
    assert.equal(events.filter(([type]) => type === 'updated').length, 1, 'an identical duplicate event is a no-op');

    socketEvents.emit('message.read', {
      conversation_id: 'conversation-live',
      user_id: 'peer-user',
      message_ids: ['server-message-1'],
    });
    assert.equal(optimistic.status, messages.STATUS.READ, 'authoritative receipts remain monotonic after reconciliation');
  } finally {
    api.conversations.send = originalSend;
    offAdded();
    offUpdated();
  }
});

test('reconnect reconciliation updates the visible store by client id without falsely failing an absent upload', async () => {
  messages.clearAllStores();
  messages.setCurrentUser('user-a');
  const conversationId = 'conversation-reconnect';
  const store = messages.getStore(conversationId);
  store.loadedOnce = true;
  const committed = messages.createOptimistic(conversationId, {
    clientId: 'client-committed-old', kind: 'text', text: 'already committed',
  });
  const stillStaging = messages.createOptimistic(conversationId, {
    clientId: 'client-upload-staging', kind: 'image', caption: 'still uploading',
  });
  messages.markLocalUnconfirmed(conversationId, committed.clientId);
  messages.markLocalUnconfirmed(conversationId, stillStaging.clientId);

  const originalPage = api.conversations.messages;
  const originalStatusByClientIds = api.messages.statusByClientIds;
  let requestedClientIds = [];
  const updated = [];
  const offUpdated = messages.messageEvents.on('updated', (_id, message) => updated.push(message));
  api.conversations.messages = async () => ({ data: { results: [] }, status: 200 });
  api.messages.statusByClientIds = async (clientIds) => {
    requestedClientIds = [...clientIds];
    return { results: [authoritativeMessage({
      id: 'server-message-old',
      clientId: committed.clientId,
      conversationId,
      text: 'already committed',
      status: 'delivered',
    })] };
  };

  try {
    await messages.reconcile(conversationId);
    assert.deepEqual(requestedClientIds.sort(), ['client-committed-old', 'client-upload-staging']);
    assert.equal(store.items.length, 2);
    assert.equal(committed.id, 'server-message-old');
    assert.equal(committed.status, messages.STATUS.DELIVERED);
    assert.equal(committed.local, false);
    assert.ok(updated.includes(committed), 'reconciliation emits an update so the existing row repaints');
    assert.equal(stillStaging.status, messages.STATUS.UNCONFIRMED, 'absence is not treated as proof an in-flight upload failed');
    assert.equal(store.find(stillStaging.clientId), stillStaging, 'the retry keeps its original client id');
  } finally {
    api.conversations.messages = originalPage;
    api.messages.statusByClientIds = originalStatusByClientIds;
    offUpdated();
  }
});

test('manual text retry reuses the original client id instead of creating a duplicate', async () => {
  messages.clearAllStores();
  messages.setCurrentUser('user-a');
  const originalSend = api.conversations.send;
  const payloads = [];
  api.conversations.send = async (_conversationId, payload) => {
    payloads.push(payload);
    return authoritativeMessage({
      id: 'server-message-retried',
      clientId: payload.client_id,
      conversationId: 'conversation-retry',
      text: payload.text,
    });
  };
  try {
    const failed = messages.createOptimistic('conversation-retry', {
      clientId: 'retry-key-stable', kind: 'text', text: 'retry me',
    });
    messages.markLocalFailed('conversation-retry', failed.clientId, new Error('temporary server failure'));
    const result = await messages.retryText('conversation-retry', failed.clientId);

    assert.equal(payloads.length, 1);
    assert.equal(payloads[0].client_id, 'retry-key-stable');
    assert.strictEqual(result, failed);
    assert.equal(messages.getStore('conversation-retry').items.length, 1);
    assert.equal(failed.id, 'server-message-retried');
    assert.equal(failed.status, messages.STATUS.SENT);
  } finally {
    api.conversations.send = originalSend;
  }
});
