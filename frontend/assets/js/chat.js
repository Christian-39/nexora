/**
 * NEXORA — chat.js
 * Conversation list, message thread rendering and the composer.
 *
 * Rendering rules:
 *  - Message text is inserted as text nodes only (never innerHTML).
 *  - Status is whatever the backend reports; local states are SENDING,
 *    UNCONFIRMED and FAILED only.
 *  - History is paginated; the DOM is bounded by the store's retention cap.
 */

import { ApiError, api, normalizePage, resolveMediaUrl } from './api.js';
import { getUser, isAdmin } from './auth.js';
import { getFeatures, getLimits } from './theme.js';
import {
  avatar,
  confirmDialog,
  emptyState,
  errorState,
  icon,
  iconButton,
  loadingRow,
  openLightbox,
  openMenu,
  skeletonList,
  toast,
  toastApiError,
  announce,
  setBusy,
} from './ui.js';
import {
  STATUS,
  STATUS_LABEL,
  createOptimistic,
  confirmOptimistic,
  deleteMessage as apiDeleteMessage,
  editMessage as apiEditMessage,
  getStore,
  loadLatest,
  loadOlder,
  markLocalFailed,
  markLocalSending,
  markLocalUnconfirmed,
  messageEvents,
  messagePreview,
  normalizeMessage,
  removeLocal,
  retryText,
  sendText,
  setCurrentUser,
  setLocalProgress,
  toggleReaction,
} from './messages.js';
import {
  acceptFor,
  cancelUpload,
  createDraft,
  pickFiles,
  renderImageAttachment,
  renderVideoAttachment,
  uploadDraft,
  validateFile,
} from './media.js';
import { VoiceRecorder, isRecordingSupported, renderVoicePlayer, renderVoicePreview, voiceEvents } from './voice.js';
import {
  clearConversationUnread,
  getConversationUnread,
  incrementConversationUnread,
  refreshUnread,
  setConversationUnread,
  unreadEvents,
} from './notifications.js';
import { getPresence, presenceEvents, presenceLabel, seedPresence, typingLabel } from './presence.js';
import { mobile } from './navigation.js';
import { realtime, rt, socketEvents } from './websocket.js';
import {
  clear,
  debounce,
  el,
  formatCount,
  formatDayLabel,
  formatDuration,
  formatListTime,
  formatRelative,
  formatTime,
  isSameDay,
  prefs,
  renderTextWithLinks,
  throttle,
} from './utils.js';

const REACTION_CHOICES = ['👍', '❤️', '✅', '👏', '🙏', '😀'];

/* ============================================================
   Controller
   ============================================================ */

export class ChatController {
  /**
   * @param {object} refs DOM references
   * @param {object} [options] { scope: 'all'|'direct'|'groups' }
   */
  constructor(refs, options = {}) {
    this.refs = refs;
    this.options = { scope: 'all', ...options };
    this.user = getUser();
    this.conversations = new Map();
    this.order = [];
    this.activeId = null;
    this.listCursor = null;
    this.listLoading = false;
    this.listAbort = null;
    this.listQuery = '';
    this.listFilter = 'all';
    this.threadAbort = null;
    this.replyTo = null;
    this.editing = null;
    this.draft = null;          // pending media attachment
    this.recorder = null;
    this.recording = null;      // { blob, duration, mimeType, name }
    // Media drafts whose upload has not yet confirmed, keyed by client_id.
    // A FAILED/UNCONFIRMED upload keeps its draft (File/Blob + preview) here so
    // it can be retried WITHOUT the user reselecting or re-recording the file.
    // Entry: { draft, caption, replyToId, posterUrl }
    this.pendingMedia = new Map();
    this.pinnedScroll = null;
    this.atBottom = true;
    this.newWhileAway = 0;
    this.rendered = new Map();  // messageId/clientId -> row element
    this.destroyed = false;

    setCurrentUser(this.user?.id);
  }

  /* ---------------- lifecycle ---------------- */

  async init() {
    this.bindListUI();
    this.bindComposer();
    this.bindStoreEvents();
    this.bindRealtime();

    // Chat-boot waterfall fix: the most likely initial thread is known BEFORE
    // the conversation list returns (?c= deep link or the remembered last
    // conversation). Warm its detail + history in parallel with the list —
    // api.js deduplicates in-flight GETs and loadLatest() coalesces callers,
    // so open() below joins these exact requests instead of starting a second
    // sequential round-trip chain. Requests that turn out to be unnecessary
    // are simply ignored; authorization stays entirely server-side.
    const requested = new URLSearchParams(window.location.search).get('c');
    const remembered = prefs.get('lastConversation');
    const initialCandidate = requested || (!mobile.isSmall ? remembered : null);
    if (initialCandidate) {
      this.bootPrefetchedId = String(initialCandidate);
      api.conversations.get(this.bootPrefetchedId).catch(() => {});
      loadLatest(this.bootPrefetchedId, { force: true }).catch(() => {});
    }

    await this.loadConversations({ reset: true });

    const initial = initialCandidate;
    if (initial && this.conversations.has(String(initial))) {
      this.open(String(initial));
    } else if (!mobile.isSmall && this.order.length) {
      this.open(this.order[0]);
    } else {
      this.renderEmptyThread();
    }
  }

  destroy() {
    this.destroyed = true;
    this.threadAbort?.abort();
    this.listAbort?.abort();
    this.recorder?.cancel();
    // Release any retained media resources so a long session cannot leak
    // File/Blob object URLs.
    for (const entry of this.pendingMedia.values()) this.disposePending(entry);
    this.pendingMedia.clear();
  }

  /** Remove a local message and release any retained upload resources for it. */
  discardPending(clientId) {
    const entry = this.pendingMedia.get(clientId);
    if (entry) {
      this.pendingMedia.delete(clientId);
      this.disposePending(entry);
    }
    removeLocal(this.activeId, clientId);
  }

  /** Dispose a retained pending-upload entry's resources exactly once. */
  disposePending(entry) {
    if (!entry) return;
    try { entry.draft?.dispose?.(); } catch { /* already disposed */ }
    if (entry.posterUrl) {
      try { URL.revokeObjectURL(entry.posterUrl); } catch { /* ignore */ }
    }
  }

  /**
   * Clear the composer tray and forget the current draft WITHOUT disposing it.
   * Used once a draft has been handed to the upload lifecycle: the pending-upload
   * machinery now owns its File/Blob and preview URL until the upload is
   * confirmed or the message is explicitly discarded. Disposing here would
   * revoke the very preview URL the optimistic bubble is still showing and the
   * File a retry would need.
   */
  detachDraft() {
    this.draft = null;
    this.recording = null;
    const { trayEl } = this.refs;
    if (trayEl) {
      clear(trayEl);
      trayEl.hidden = true;
    }
  }

  /* ============================================================
     Conversation list
     ============================================================ */

  bindListUI() {
    const { searchInput, filterBar, listEl } = this.refs;

    if (searchInput) {
      const run = debounce((value) => {
        this.listQuery = value.trim();
        this.loadConversations({ reset: true });
      }, 300);
      searchInput.addEventListener('input', (event) => run(event.target.value));
    }

    if (filterBar) {
      filterBar.addEventListener('click', (event) => {
        const btn = event.target.closest('[data-filter]');
        if (!btn) return;
        for (const node of filterBar.querySelectorAll('[data-filter]')) {
          node.setAttribute('aria-pressed', String(node === btn));
        }
        this.listFilter = btn.dataset.filter;
        this.loadConversations({ reset: true });
      });
    }

    if (listEl) {
      // Infinite scroll for long conversation directories.
      listEl.addEventListener(
        'scroll',
        throttle(() => {
          if (this.listLoading || !this.listCursor) return;
          const remaining = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight;
          if (remaining < 320) this.loadConversations({ reset: false });
        }, 200)
      );
    }

    unreadEvents.on('conversation', (id) => this.refreshConversationRow(id));
  }

  async loadConversations({ reset = false } = {}) {
    const { listEl } = this.refs;
    if (!listEl || this.listLoading) return;
    this.listLoading = true;
    // A new list request supersedes any in-flight one (search typing, filter
    // switching); the abandoned request is aborted instead of racing back.
    this.listAbort?.abort();
    this.listAbort = new AbortController();

    if (reset) {
      this.listCursor = null;
      clear(listEl);
      listEl.append(skeletonList(7));
    }

    try {
      const params = { limit: 30 };
      if (this.listCursor) params.cursor = this.listCursor;
      if (this.listQuery) params.search = this.listQuery;
      if (this.listFilter === 'unread') params.unread = true;
      if (this.listFilter === 'groups') params.type = 'group';
      if (this.listFilter === 'direct') params.type = 'direct';
      if (this.options.scope === 'groups') params.type = 'group';
      if (this.options.scope === 'direct') params.type = 'direct';

      const response = await api.conversations.list(params, { signal: this.listAbort.signal });
      const page = normalizePage(response.data ?? response);

      if (reset) {
        clear(listEl);
        this.conversations.clear();
        this.order = [];
      }

      for (const raw of page.items) {
        const conv = normalizeConversation(raw, this.user);
        this.conversations.set(conv.id, conv);
        if (!this.order.includes(conv.id)) this.order.push(conv.id);
        if (typeof raw.unread_count === 'number') setConversationUnread(conv.id, raw.unread_count);
        seedPresence([conv.counterpart].filter(Boolean));
      }

      this.listCursor = page.next;
      this.renderList();
    } catch (error) {
      if (error instanceof ApiError && error.isAborted) return; // superseded, not failed
      clear(listEl);
      listEl.append(errorState({ title: 'Unable to load conversations', text: error.message, onRetry: () => this.loadConversations({ reset: true }) }));
    } finally {
      this.listLoading = false;
    }
  }

  renderList() {
    const { listEl } = this.refs;
    if (!listEl) return;
    clear(listEl);

    if (!this.order.length) {
      listEl.append(
        this.listQuery
          ? emptyState({ icon: 'search', title: 'No matches', text: `Nothing matched “${this.listQuery}”.` })
          : emptyState({
              icon: 'message-square',
              title: 'No conversations yet',
              text: isAdmin()
                ? 'Conversations appear here once members are created and messaging begins.'
                : 'Your conversation with the administrator will appear here.',
            })
      );
      return;
    }

    const sorted = [...this.order].sort((a, b) => {
      const ca = this.conversations.get(a);
      const cb = this.conversations.get(b);
      if (ca?.pinned !== cb?.pinned) return ca?.pinned ? -1 : 1;
      return (Date.parse(cb?.lastActivity || 0) || 0) - (Date.parse(ca?.lastActivity || 0) || 0);
    });

    let lastGroupLabel = null;
    for (const id of sorted) {
      const conv = this.conversations.get(id);
      if (!conv) continue;
      if (this.options.scope === 'all' && this.listFilter === 'all') {
        const label = conv.isGroup ? 'Groups' : conv.isAdminThread ? 'Administrator' : 'Direct';
        if (label !== lastGroupLabel) {
          listEl.append(el('div', { class: 'conv-group-label', text: label }));
          lastGroupLabel = label;
        }
      }
      listEl.append(this.renderConversationRow(conv));
    }

    if (this.listCursor) listEl.append(loadingRow('Loading more…'));
  }

  renderConversationRow(conv) {
    const unreadCount = getConversationUnread(conv.id);
    const row = el('button', {
      type: 'button',
      class: 'conv-item',
      dataset: { id: conv.id, unread: String(unreadCount > 0) },
      'aria-current': String(this.activeId === conv.id),
    });

    const presence = conv.isGroup ? null : getPresence(conv.counterpart?.id).status;
    row.append(
      avatar(conv.title, conv.avatarUrl, {
        presence: presence === 'online' ? 'online' : presence === 'offline' ? 'offline' : null,
      })
    );

    const preview = el('span', { class: 'conv-item__preview' });
    if (conv.lastMessage?.kind && conv.lastMessage.kind !== 'text') {
      preview.append(icon(previewIconFor(conv.lastMessage.kind), { size: 14 }));
    }
    preview.append(el('span', { class: 'truncate', text: conv.previewText }));

    const bottom = el('div', { class: 'conv-item__bottom' }, [preview]);
    if (unreadCount > 0) {
      bottom.append(el('span', { class: 'badge', text: formatCount(unreadCount), 'aria-label': `${unreadCount} unread messages` }));
    }

    row.append(
      el('span', { class: 'conv-item__body' }, [
        el('span', { class: 'conv-item__top' }, [
          el('span', { class: 'conv-item__name', text: conv.title }),
          el('span', { class: 'conv-item__time', text: formatListTime(conv.lastActivity) }),
        ]),
        bottom,
      ])
    );

    row.addEventListener('click', () => this.open(conv.id));
    return row;
  }

  refreshConversationRow(id) {
    const { listEl } = this.refs;
    if (!listEl) return;
    const existing = listEl.querySelector(`.conv-item[data-id="${cssEscape(id)}"]`);
    const conv = this.conversations.get(String(id));
    if (!existing || !conv) return;
    existing.replaceWith(this.renderConversationRow(conv));
  }

  /* ============================================================
     Thread
     ============================================================ */

  async open(conversationId) {
    const id = String(conversationId);
    if (this.activeId === id) {
      if (mobile.isSmall) mobile.showThread();
      return;
    }

    if (this.activeId) rt.leaveConversation(this.activeId);
    this.threadAbort?.abort();
    this.threadAbort = new AbortController();

    this.activeId = id;
    this.replyTo = null;
    this.editing = null;
    this.clearDraft();
    this.rendered.clear();
    this.newWhileAway = 0;
    prefs.set('lastConversation', id);

    const conv = this.conversations.get(id);
    this.renderThreadHeader(conv);
    this.renderComposerState(conv);

    for (const node of this.refs.listEl?.querySelectorAll('.conv-item') || []) {
      node.setAttribute('aria-current', String(node.dataset.id === id));
    }

    if (mobile.isSmall) mobile.showThread();

    const { scrollEl, messagesEl } = this.refs;
    clear(messagesEl);
    messagesEl.append(skeletonList(5, { avatar: false }));

    rt.joinConversation(id);

    try {
      // Fetch authoritative conversation metadata in parallel with history.
      // If init() already prefetched THIS conversation, loadLatest() either
      // joins the in-flight request or returns the just-loaded fresh page —
      // no second fetch of data that arrived milliseconds ago.
      const prefetched = this.bootPrefetchedId === id;
      this.bootPrefetchedId = null;
      const [detail] = await Promise.allSettled([
        api.conversations.get(id, { signal: this.threadAbort.signal }),
        loadLatest(id, { signal: this.threadAbort.signal, force: !prefetched }),
      ]);

      if (detail.status === 'fulfilled' && detail.value) {
        const merged = normalizeConversation(detail.value, this.user);
        this.conversations.set(id, merged);
        seedPresence([merged.counterpart].filter(Boolean));
        this.renderThreadHeader(merged);
        this.renderComposerState(merged);
        this.refreshConversationRow(id);
      }

      if (this.activeId !== id) return;
      this.renderMessages({ scrollToBottom: true });
      this.markRead();
    } catch (error) {
      if (error instanceof ApiError && error.isAborted) return;
      clear(messagesEl);
      messagesEl.append(
        error instanceof ApiError && error.isForbidden
          ? errorState({ title: 'Not authorized', text: 'You do not have access to this conversation.' })
          : errorState({ title: 'Unable to load messages', text: error.message, onRetry: () => { this.activeId = null; this.open(id); } })
      );
    }

    if (scrollEl) this.bindThreadScroll();
  }

  renderThreadHeader(conv) {
    const { headerEl, threadEl } = this.refs;
    if (!headerEl) return;
    clear(headerEl);

    if (!conv) {
      headerEl.append(el('div', { class: 'thread__name', text: 'Select a conversation' }));
      return;
    }

    threadEl?.removeAttribute('hidden');

    const back = iconButton('arrow-left', 'Back to conversations', {
      className: 'thread__back',
      onClick: () => {
        mobile.showList();
        this.activeId && rt.leaveConversation(this.activeId);
      },
    });
    headerEl.append(back);

    const identity = el('button', { type: 'button', class: 'thread__identity', 'aria-label': `${conv.title} details` });
    identity.append(avatar(conv.title, conv.avatarUrl, { size: 'sm' }));

    const status = el('div', { class: 'thread__status', id: 'thread-status' });
    identity.append(
      el('span', { class: 'thread__meta' }, [
        el('span', { class: 'thread__name', text: conv.title }),
        status,
      ])
    );
    identity.addEventListener('click', () => this.openDetails(conv));
    headerEl.append(identity);

    const actions = el('div', { class: 'thread__actions' });
    actions.append(iconButton('info', 'Conversation details', { onClick: () => this.openDetails(conv) }));
    headerEl.append(actions);

    this.updateThreadStatus(conv);
  }

  updateThreadStatus(conv = this.conversations.get(this.activeId)) {
    const node = document.getElementById('thread-status');
    if (!node || !conv) return;

    const typing = typingLabel(conv.id, { isGroup: conv.isGroup });
    if (typing) {
      node.dataset.typing = 'true';
      node.textContent = typing;
      return;
    }
    delete node.dataset.typing;

    if (conv.isGroup) {
      node.textContent = conv.memberCount ? `${conv.memberCount} members` : 'Group';
      return;
    }
    if (!getFeatures().presence) {
      node.textContent = conv.isAdminThread ? 'Administrator' : '';
      return;
    }
    const label = presenceLabel(conv.counterpart?.id, { formatRelative });
    node.textContent = label || (conv.isAdminThread ? 'Administrator' : '');
  }

  bindThreadScroll() {
    const { scrollEl } = this.refs;
    if (!scrollEl || scrollEl.dataset.bound === 'true') return;
    scrollEl.dataset.bound = 'true';

    scrollEl.addEventListener(
      'scroll',
      throttle(() => {
        const distanceFromBottom = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
        this.atBottom = distanceFromBottom < 80;
        this.updateJumpButton();

        if (this.atBottom && this.newWhileAway > 0) {
          this.newWhileAway = 0;
          this.updateJumpButton();
          this.markRead();
        }

        if (scrollEl.scrollTop < 240) this.loadOlderMessages();
      }, 120)
    );
  }

  async loadOlderMessages() {
    const id = this.activeId;
    if (!id) return;
    const store = getStore(id);
    if (store.loadingOlder || !store.hasMore) return;

    const { scrollEl, messagesEl } = this.refs;
    const previousHeight = scrollEl.scrollHeight;
    const previousTop = scrollEl.scrollTop;

    const loader = el('div', { class: 'thread__loader' }, [el('span', { class: 'spinner' })]);
    messagesEl.prepend(loader);

    try {
      await loadOlder(id);
      if (this.activeId !== id) return;
      this.renderMessages({ scrollToBottom: false });
      // Preserve the reading position exactly.
      const delta = scrollEl.scrollHeight - previousHeight;
      scrollEl.scrollTop = previousTop + delta;
    } catch (error) {
      toastApiError(error);
    } finally {
      loader.remove();
    }
  }

  /**
   * Full re-render of the visible thread. The store caps retained messages,
   * so this never renders thousands of nodes.
   */
  renderMessages({ scrollToBottom = false } = {}) {
    const id = this.activeId;
    const { messagesEl, scrollEl } = this.refs;
    if (!id || !messagesEl) return;

    const store = getStore(id);
    const conv = this.conversations.get(id);
    clear(messagesEl);
    this.rendered.clear();

    if (!store.items.length) {
      messagesEl.append(
        emptyState({
          icon: 'message-square',
          title: 'No messages yet',
          text: conv?.isGroup
            ? 'Start the conversation with this group.'
            : 'Send a message to start the conversation.',
        })
      );
      return;
    }

    if (!store.hasMore) {
      messagesEl.append(el('div', { class: 'thread__history-end', text: 'Beginning of conversation' }));
    }

    let previous = null;
    for (const message of store.items) {
      if (!previous || !isSameDay(previous.createdAt, message.createdAt)) {
        messagesEl.append(el('div', { class: 'date-sep' }, [el('span', { text: formatDayLabel(message.createdAt) })]));
      }
      const groupStart = !previous || previous.senderId !== message.senderId || !isSameDay(previous.createdAt, message.createdAt);
      const node = this.renderMessage(message, { conv, groupStart });
      messagesEl.append(node);
      this.rendered.set(message.id || message.clientId, node);
      previous = message;
    }

    messagesEl.append(this.typingRow());

    if (scrollToBottom && scrollEl) {
      window.requestAnimationFrame(() => {
        scrollEl.scrollTop = scrollEl.scrollHeight;
        this.atBottom = true;
        this.updateJumpButton();
      });
    }
  }

  typingRow() {
    const row = el('div', { class: 'typing-row', id: 'typing-row', hidden: true });
    row.append(el('div', { class: 'typing-bubble', 'aria-hidden': 'true' }, [el('i'), el('i'), el('i')]));
    return row;
  }

  updateTypingRow() {
    const row = document.getElementById('typing-row');
    if (!row || !this.activeId) return;
    const conv = this.conversations.get(this.activeId);
    const label = typingLabel(this.activeId, { isGroup: conv?.isGroup });
    row.hidden = !label;
    if (label && this.atBottom) {
      this.refs.scrollEl.scrollTop = this.refs.scrollEl.scrollHeight;
    }
  }

  /** Build one message row. */
  renderMessage(message, { conv, groupStart = false } = {}) {
    if (message.system) {
      return el('div', { class: 'msg--system', dataset: { id: message.id || message.clientId } }, [message.text || '']);
    }

    const row = el('div', {
      class: 'msg-row',
      dataset: {
        dir: message.outgoing ? 'out' : 'in',
        id: message.id || message.clientId,
        groupStart: String(groupStart),
      },
    });

    if (!message.outgoing && conv?.isGroup) {
      const slot = el('div', { class: 'msg-row__avatar' });
      if (groupStart) slot.append(avatar(message.sender?.display_name || message.sender?.name, message.sender?.avatar_url, { size: 'sm' }));
      row.append(slot);
    }

    const bubble = el('div', {
      class: `bubble ${message.kind === 'image' || message.kind === 'video' ? 'bubble--media' : ''}`.trim(),
      dataset: {
        pending: String(message.status === STATUS.SENDING),
        failed: String(message.status === STATUS.FAILED),
      },
    });

    if (message.isDeleted) {
      bubble.append(el('div', { class: 'bubble__text text-muted', style: { fontStyle: 'italic' }, text: 'This message was deleted.' }));
      bubble.append(this.bubbleFooter(message));
      row.append(bubble);
      return row;
    }

    if (!message.outgoing && conv?.isGroup && groupStart) {
      bubble.append(el('div', { class: 'bubble__author', text: message.sender?.display_name || message.sender?.name || 'Member' }));
    }

    if (message.replyTo) bubble.append(this.renderReplyRef(message.replyTo));

    switch (message.kind) {
      case 'image': {
        bubble.append(this.mediaWrapper(message, renderImageAttachment(message.media, { onOpen: openLightbox, alt: message.caption || 'Image attachment' })));
        if (message.caption) bubble.append(el('div', { class: 'bubble__caption' }, [renderTextWithLinks(message.caption)]));
        break;
      }
      case 'video': {
        bubble.append(this.mediaWrapper(message, renderVideoAttachment(message.media, { onOpen: openLightbox })));
        if (message.caption) bubble.append(el('div', { class: 'bubble__caption' }, [renderTextWithLinks(message.caption)]));
        break;
      }
      case 'voice': {
        bubble.append(renderVoicePlayer(message.media, { duration: message.media?.duration }));
        break;
      }
      case 'file': {
        const link = el('a', { class: 'row', href: '#', text: message.media?.name || 'Attachment' });
        link.addEventListener('click', async (event) => {
          event.preventDefault();
          const { getMediaUrl } = await import('./media.js');
          const url = await getMediaUrl(message.media, 'full');
          if (url) window.open(url, '_blank', 'noopener');
          else toast('This attachment is no longer available.', { type: 'warning' });
        });
        bubble.append(link);
        break;
      }
      default: {
        const text = el('div', { class: 'bubble__text' });
        text.append(renderTextWithLinks(message.text));
        bubble.append(text);
      }
    }

    if (getFeatures().reactions && message.reactions?.length) {
      bubble.append(this.renderReactions(message));
    }

    bubble.append(this.bubbleFooter(message, conv));

    if ((message.status === STATUS.FAILED || message.status === STATUS.UNCONFIRMED) && message.outgoing) {
      const retry = el('div', { class: 'bubble__retry' });
      const labelText =
        message.status === STATUS.UNCONFIRMED
          ? message.error || 'Delivery unconfirmed. Tap Retry to send again.'
          : message.error || 'Not sent.';
      retry.append(icon('alert-circle', { size: 14 }), el('span', { text: labelText }));
      const btn = el('button', { type: 'button', class: 'btn btn--sm btn--ghost', text: 'Retry' });
      btn.addEventListener('click', () => this.retryMessage(message));
      const discard = el('button', { type: 'button', class: 'btn btn--sm btn--ghost', text: 'Discard' });
      discard.addEventListener('click', () => {
        this.discardPending(message.clientId);
      });
      retry.append(btn, discard);
      bubble.append(retry);
    }

    row.append(bubble);
    if (!message.local && !message.isDeleted && getFeatures().replies) {
      this.bindSwipeToReply(row, bubble, message);
    }
    return row;
  }

  mediaWrapper(message, node) {
    if (message.progress == null) return node;
    const wrap = el('div', { style: { position: 'relative' } }, [node]);
    const overlay = el('div', { class: 'media-upload' });
    const bar = el('div', { class: 'progress' }, [el('div', { class: 'progress__bar', style: { width: `${message.progress}%` } })]);
    overlay.append(el('span', { text: `Uploading ${message.progress}%` }), bar);
    const cancel = el('button', { type: 'button', class: 'btn btn--sm btn--ghost', text: 'Cancel' });
    cancel.addEventListener('click', () => {
      cancelUpload(message.clientId);
      this.discardPending(message.clientId);
    });
    overlay.append(cancel);
    wrap.append(overlay);
    return wrap;
  }

  bubbleFooter(message, conv) {
    const footer = el('div', { class: 'bubble__footer' });
    if (message.isEdited) footer.append(el('span', { class: 'bubble__edited', text: 'edited' }));
    footer.append(el('span', { class: 'bubble__time', text: formatTime(message.createdAt) }));

    if (message.outgoing && !message.isDeleted) {
      const label = STATUS_LABEL[message.status] || '';
      const statusEl = el('span', {
        class: 'msg-status',
        dataset: { status: message.status },
        title: label,
        'aria-label': label,
        role: 'img',
      });
      statusEl.append(icon(statusIconFor(message.status), { size: 14 }));
      footer.append(statusEl);
    }
    if (!message.local && !message.isDeleted) {
      const tools = this.bubbleTools(message, conv);
      if (tools.childNodes.length) footer.append(tools);
    }
    return footer;
  }

  renderReplyRef(reply) {
    const node = el('button', {
      type: 'button',
      class: `reply-ref ${reply.available ? '' : 'reply-ref--missing'}`.trim(),
      'aria-label': reply.available ? 'Go to the referenced message' : 'The referenced message is unavailable',
    });
    if (!reply.available) {
      node.append(el('span', { class: 'reply-ref__text', text: 'Message unavailable.' }));
      node.disabled = true;
      return node;
    }
    node.append(
      el('span', { class: 'stack-sm', style: { gap: '0', minWidth: '0' } }, [
        el('span', { class: 'reply-ref__author', text: reply.authorName || 'Message' }),
        el('span', { class: 'reply-ref__text', text: reply.preview || previewLabelFor(reply.kind) }),
      ])
    );
    node.addEventListener('click', () => this.jumpToMessage(reply.id));
    return node;
  }

  renderReactions(message) {
    const wrap = el('div', { class: 'reactions' });
    const grouped = new Map();
    for (const r of message.reactions) {
      const key = r.reaction || r.emoji;
      if (!key) continue;
      const entry = grouped.get(key) || { count: 0, mine: false };
      entry.count += Number(r.count ?? 1);
      if (r.is_mine || String(r.user_id) === String(this.user?.id)) entry.mine = true;
      grouped.set(key, entry);
    }
    for (const [emoji, entry] of grouped) {
      const btn = el('button', {
        type: 'button',
        class: 'reaction',
        dataset: { mine: String(entry.mine) },
        'aria-label': `${emoji} ${entry.count} reaction${entry.count === 1 ? '' : 's'}${entry.mine ? ', including yours' : ''}`,
        'aria-pressed': String(entry.mine),
      });
      btn.append(el('span', { 'aria-hidden': 'true', text: emoji }), el('span', { class: 'reaction__count', text: String(entry.count) }));
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
          await toggleReaction(this.activeId, message.id, emoji, entry.mine);
        } catch (error) {
          toastApiError(error);
        } finally {
          btn.disabled = false;
        }
      });
      wrap.append(btn);
    }
    return wrap;
  }

  bubbleTools(message, conv) {
    const features = getFeatures();
    const tools = el('div', { class: 'bubble__tools' });

    if (features.replies) {
      tools.append(
        iconButton('reply', 'Reply to this message', {
          className: 'icon-btn--sm bubble__reply-btn',
          onClick: () => this.startReply(message),
        })
      );
    }

    const menuItems = [];
    if (features.replies) {
      menuItems.push({
        label: 'Reply',
        icon: 'reply',
        onClick: () => this.startReply(message),
      });
    }
    if (features.reactions && message.permissions.canReact) {
      menuItems.push({
        label: 'React',
        icon: 'circle',
        onClick: () => this.openReactionPicker(message),
      });
    }
    if (features.editing && message.permissions.canEdit && message.kind === 'text') {
      menuItems.push({ label: 'Edit', icon: 'pencil', onClick: () => this.startEdit(message) });
    }
    if (message.permissions.canDeleteForSelf) {
      menuItems.push({
        label: 'Delete for me',
        icon: 'trash',
        danger: true,
        onClick: () => this.confirmDelete(message, 'self'),
      });
    }
    if (features.deleteForEveryone && message.permissions.canDeleteForEveryone) {
      menuItems.push({
        label: 'Delete for everyone',
        icon: 'trash',
        danger: true,
        onClick: () => this.confirmDelete(message, 'everyone'),
      });
    }

    if (menuItems.length) {
      const more = iconButton('more-vertical', 'Message actions', { className: 'icon-btn--sm bubble__more-btn' });
      more.addEventListener('click', () => openMenu(more, menuItems));
      tools.append(more);
    }

    void conv;
    return tools;
  }

  /**
   * Bind touch swipe-to-reply on a message bubble.
   * Requires a clear horizontal gesture (|dx| > |dy| * 1.5 and |dx| >= 12px)
   * so vertical scrolling, text selection, and media playback are unaffected.
   */
  bindSwipeToReply(row, bubble, message) {
    const SWIPE_THRESHOLD = 48;
    const MAX_TRANSLATE = 64;
    let startX = 0;
    let startY = 0;
    let currentOffset = 0;
    let tracking = false;
    let lockedHorizontal = false;
    let hintEl = null;

    const resetSwipe = () => {
      if (lockedHorizontal || currentOffset !== 0) {
        row.classList.remove('msg-row--swiping');
        bubble.style.transform = '';
        if (hintEl) {
          hintEl.dataset.ready = 'false';
          hintEl.style.opacity = '0';
        }
      }
      tracking = false;
      lockedHorizontal = false;
      currentOffset = 0;
    };

    row.addEventListener(
      'touchstart',
      (event) => {
        if (event.touches.length !== 1) return;
        const target = event.target;
        if (target?.closest?.('button, a, audio, video, input, textarea, [role="slider"], .voice__track')) {
          return;
        }
        const touch = event.touches[0];
        startX = touch.clientX;
        startY = touch.clientY;
        currentOffset = 0;
        tracking = true;
        lockedHorizontal = false;
      },
      { passive: true }
    );

    row.addEventListener(
      'touchmove',
      (event) => {
        if (!tracking || event.touches.length !== 1) return;
        const touch = event.touches[0];
        const dx = touch.clientX - startX;
        const dy = touch.clientY - startY;

        if (!lockedHorizontal) {
          if (Math.abs(dy) > 10 && Math.abs(dy) >= Math.abs(dx)) {
            tracking = false;
            return;
          }
          if (Math.abs(dx) < 12 || Math.abs(dx) < Math.abs(dy) * 1.5) {
            return;
          }
          // Outgoing bubbles sit on the right (swipe left or right works, biased inward);
          // incoming bubbles sit on the left (swipe right).
          if (!message.outgoing && dx < 0) {
            tracking = false;
            return;
          }
          lockedHorizontal = true;
          row.classList.add('msg-row--swiping');
          if (!hintEl) {
            hintEl = el('span', { class: 'msg-row__swipe-hint', 'aria-hidden': 'true' }, [
              icon('reply', { size: 16 }),
            ]);
            row.append(hintEl);
          }
        }

        const clamped = message.outgoing
          ? Math.max(-MAX_TRANSLATE, Math.min(MAX_TRANSLATE, dx))
          : Math.max(0, Math.min(MAX_TRANSLATE, dx));
        currentOffset = clamped;
        bubble.style.transform = `translate3d(${clamped}px, 0, 0)`;
        if (hintEl) {
          const progress = Math.min(1, Math.abs(clamped) / SWIPE_THRESHOLD);
          hintEl.style.opacity = String(progress);
          hintEl.dataset.ready = String(Math.abs(clamped) >= SWIPE_THRESHOLD);
        }
      },
      { passive: true }
    );

    const onEnd = () => {
      if (!tracking) return;
      const triggered = lockedHorizontal && Math.abs(currentOffset) >= SWIPE_THRESHOLD;
      resetSwipe();
      if (triggered) {
        try { navigator.vibrate?.(10); } catch { /* ignore */ }
        this.startReply(message);
      }
    };

    row.addEventListener('touchend', onEnd, { passive: true });
    row.addEventListener('touchcancel', resetSwipe, { passive: true });
  }

  openReactionPicker(message) {
    const items = REACTION_CHOICES.map((emoji) => ({
      label: emoji,
      onClick: async () => {
        try {
          await toggleReaction(this.activeId, message.id, emoji, false);
        } catch (error) {
          toastApiError(error);
        }
      },
    }));
    const anchor = this.rendered.get(message.id) || this.refs.messagesEl;
    openMenu(anchor.querySelector?.('.bubble') || anchor, items);
  }

  async confirmDelete(message, scope) {
    const ok = await confirmDialog({
      title: scope === 'everyone' ? 'Delete for everyone?' : 'Delete this message?',
      message:
        scope === 'everyone'
          ? 'This message will be removed for every participant in this conversation.'
          : 'This message will be removed from your view only.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      await apiDeleteMessage(this.activeId, message.id, scope);
      announce('Message deleted.');
    } catch (error) {
      toastApiError(error);
    }
  }

  jumpToMessage(messageId) {
    const node = this.rendered.get(String(messageId));
    if (!node) {
      toast('That message is not loaded in this view.', { type: 'info' });
      return;
    }
    node.scrollIntoView({ block: 'center', behavior: 'smooth' });
    node.dataset.highlight = 'true';
    setTimeout(() => delete node.dataset.highlight, 1600);
  }

  updateJumpButton() {
    const { jumpBtn } = this.refs;
    if (!jumpBtn) return;
    jumpBtn.hidden = this.atBottom;
    const badge = jumpBtn.querySelector('.badge');
    if (badge) {
      badge.hidden = this.newWhileAway === 0;
      badge.textContent = formatCount(this.newWhileAway);
    }
  }

  scrollToBottom({ smooth = false } = {}) {
    const { scrollEl } = this.refs;
    if (!scrollEl) return;
    scrollEl.scrollTo({ top: scrollEl.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    this.atBottom = true;
    this.newWhileAway = 0;
    this.updateJumpButton();
  }

  renderEmptyThread() {
    const { messagesEl, headerEl, composerEl } = this.refs;
    if (headerEl) {
      clear(headerEl);
      headerEl.append(el('div', { class: 'thread__name', text: 'Select a conversation' }));
    }
    if (composerEl) composerEl.hidden = true;
    if (messagesEl) {
      clear(messagesEl);
      messagesEl.append(
        emptyState({
          icon: 'message-square',
          title: 'No conversation selected',
          text: 'Choose a conversation from the list to start reading and replying.',
        })
      );
    }
  }

  /* ============================================================
     Read receipts
     ============================================================ */

  markRead = debounce(async () => {
    const id = this.activeId;
    if (!id || document.visibilityState !== 'visible') return;
    const store = getStore(id);
    const last = store.last;
    if (!last?.id) return;
    if (getConversationUnread(id) === 0 && this.lastReadId === last.id) return;
    this.lastReadId = last.id;

    clearConversationUnread(id);
    rt.markRead(id, last.id);
    try {
      await api.conversations.markRead(id, { last_message_id: last.id });
    } catch {
      // Backend remains authoritative; the next summary refresh corrects this.
      refreshUnread();
    }
  }, 500);

  /* ============================================================
     Composer
     ============================================================ */

  renderComposerState(conv) {
    const { composerEl, inputEl, composerNotice } = this.refs;
    if (!composerEl) return;

    const canSend = conv ? conv.canSend !== false : false;
    composerEl.hidden = !conv;
    if (inputEl) {
      inputEl.disabled = !canSend;
      inputEl.placeholder = canSend ? 'Write a message' : 'You cannot send messages in this conversation';
    }
    for (const btn of composerEl.querySelectorAll('button')) btn.disabled = !canSend;

    if (composerNotice) {
      if (conv && conv.canSend === false) {
        composerNotice.hidden = false;
        composerNotice.textContent = conv.readOnlyReason || 'This conversation is read-only.';
      } else {
        composerNotice.hidden = true;
        composerNotice.textContent = '';
      }
    }
  }

  bindComposer() {
    const { inputEl, sendBtn, attachBtn, voiceBtn, counterEl, jumpBtn } = this.refs;
    if (!inputEl) return;

    const limits = getLimits();

    const autoGrow = () => {
      inputEl.style.height = 'auto';
      inputEl.style.height = `${Math.min(inputEl.scrollHeight, 140)}px`;
    };

    const updateCounter = () => {
      if (!counterEl) return;
      const length = inputEl.value.length;
      const threshold = Math.floor(limits.maxMessageLength * 0.8);
      if (length >= threshold) {
        counterEl.hidden = false;
        counterEl.textContent = `${length} / ${limits.maxMessageLength}`;
        counterEl.dataset.over = String(length > limits.maxMessageLength);
      } else {
        counterEl.hidden = true;
      }
    };

    const notifyTyping = throttle(() => {
      if (this.activeId && getFeatures().typing) rt.typing(this.activeId, true);
    }, 2500);

    const stopTyping = debounce(() => {
      if (this.activeId && getFeatures().typing) rt.typing(this.activeId, false);
    }, 3000);

    inputEl.addEventListener('input', () => {
      autoGrow();
      updateCounter();
      if (inputEl.value.trim()) {
        notifyTyping();
        stopTyping();
      }
    });

    inputEl.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !mobile.isSmall) {
        event.preventDefault();
        this.submit();
      }
      if (event.key === 'Escape') {
        this.cancelContext();
      }
    });

    // Paste an image straight into the composer.
    inputEl.addEventListener('paste', async (event) => {
      const file = Array.from(event.clipboardData?.files || [])[0];
      if (!file) return;
      event.preventDefault();
      await this.attachFile(file);
    });

    sendBtn?.addEventListener('click', () => this.submit());

    attachBtn?.addEventListener('click', () => this.openAttachMenu(attachBtn));

    if (voiceBtn) {
      if (!isRecordingSupported() || !getFeatures().voiceNotes) {
        voiceBtn.hidden = true;
      } else {
        voiceBtn.addEventListener('click', () => this.toggleRecording());
      }
    }

    jumpBtn?.addEventListener('click', () => this.scrollToBottom({ smooth: true }));

    // Drag & drop attachments.
    const threadEl = this.refs.threadEl;
    if (threadEl) {
      threadEl.addEventListener('dragover', (event) => {
        if (!event.dataTransfer?.types?.includes('Files')) return;
        event.preventDefault();
      });
      threadEl.addEventListener('drop', async (event) => {
        const file = event.dataTransfer?.files?.[0];
        if (!file) return;
        event.preventDefault();
        await this.attachFile(file);
      });
    }

    voiceEvents.on('tick', (seconds) => {
      const timeEl = document.getElementById('recorder-time');
      if (timeEl) timeEl.textContent = formatDuration(seconds);
    });
    voiceEvents.on('limit-reached', () => {
      toast('Maximum voice note length reached.', { type: 'warning' });
      this.stopRecording();
    });

    // Keep the thread anchored to the bottom when the mobile virtual keyboard
    // resizes the visualViewport while the user is already at the bottom.
    window.visualViewport?.addEventListener(
      'resize',
      () => {
        if (this.activeId && this.atBottom) {
          window.requestAnimationFrame(() => this.scrollToBottom());
        }
      },
      { passive: true }
    );
  }

  openAttachMenu(anchor) {
    const features = getFeatures();
    const items = [
      {
        label: 'Photo',
        icon: 'image',
        onClick: async () => {
          const [file] = await pickFiles({ accept: acceptFor('image') });
          if (file) this.attachFile(file, 'image');
        },
      },
    ];
    if (features.videoMessages) {
      items.push({
        label: 'Video',
        icon: 'video',
        onClick: async () => {
          const [file] = await pickFiles({ accept: acceptFor('video') });
          if (file) this.attachFile(file, 'video');
        },
      });
    }
    if (isMobileDevice()) {
      items.push({
        label: 'Take photo',
        icon: 'image',
        onClick: async () => {
          const [file] = await pickFiles({ accept: acceptFor('image'), capture: 'environment' });
          if (file) this.attachFile(file, 'image');
        },
      });
    }
    openMenu(anchor, items);
  }

  async attachFile(file, kindHint = 'auto') {
    const check = validateFile(file, kindHint);
    if (!check.ok) {
      toast(check.message, { type: 'error' });
      return;
    }
    this.clearDraft();
    try {
      this.draft = await createDraft(file, check.kind);
      this.renderPreviewTray();
    } catch {
      toast('That file could not be prepared for sending.', { type: 'error' });
    }
  }

  renderPreviewTray() {
    const { trayEl } = this.refs;
    if (!trayEl) return;
    clear(trayEl);
    trayEl.hidden = !this.draft && !this.recording;

    if (this.recording) {
      const preview = renderVoicePreview(this.recording.blob, this.recording.duration);
      const actions = el('div', { class: 'preview-tray__actions' });
      const cancel = el('button', { type: 'button', class: 'btn btn--sm', text: 'Discard' });
      cancel.addEventListener('click', () => {
        preview.dispose?.();
        this.recording = null;
        this.renderPreviewTray();
      });
      actions.append(cancel);
      trayEl.append(
        el('div', { class: 'preview-tray' }, [
          el('div', { class: 'preview-tray__body' }, [
            el('div', { class: 'preview-tray__name', text: 'Voice note ready — tap Send below' }),
            preview,
            actions,
          ]),
        ])
      );
      return;
    }

    if (!this.draft) return;

    const thumb = el('div', { class: 'preview-tray__thumb' });
    if (this.draft.kind === 'image') {
      thumb.append(el('img', { src: this.draft.previewUrl, alt: '' }));
    } else if (this.draft.kind === 'video') {
      const video = el('video', { src: this.draft.previewUrl, muted: true, playsInline: true, preload: 'metadata' });
      thumb.append(video);
    } else {
      thumb.append(icon('paperclip'));
    }

    const actions = el('div', { class: 'preview-tray__actions' });
    const cancel = el('button', { type: 'button', class: 'btn btn--sm', text: 'Cancel' });
    cancel.addEventListener('click', () => this.clearDraft());
    actions.append(cancel);

    trayEl.append(
      el('div', { class: 'preview-tray' }, [
        thumb,
        el('div', { class: 'preview-tray__body' }, [
          el('div', { class: 'preview-tray__name', text: this.draft.name }),
          el('div', { class: 'preview-tray__meta', text: draftMeta(this.draft) }),
          el('div', { class: 'text-xs text-muted', text: 'Add an optional caption in the message box, then tap Send.' }),
          actions,
        ]),
      ])
    );
  }

  clearDraft() {
    this.draft?.dispose?.();
    this.draft = null;
    this.recording = null;
    const { trayEl } = this.refs;
    if (trayEl) {
      clear(trayEl);
      trayEl.hidden = true;
    }
  }

  /* ---------------- reply / edit context ---------------- */

  startReply(message) {
    this.editing = null;
    this.replyTo = {
      id: message.id,
      authorName: message.outgoing ? 'You' : message.sender?.display_name || message.sender?.name || 'Member',
      preview: messagePreview(message).slice(0, 140),
      kind: message.kind,
    };
    this.renderContextBar();
    this.refs.inputEl?.focus();
  }

  startEdit(message) {
    this.replyTo = null;
    this.editing = { id: message.id, original: message.text };
    this.refs.inputEl.value = message.text;
    this.renderContextBar();
    this.refs.inputEl?.focus();
  }

  cancelContext() {
    if (this.editing) this.refs.inputEl.value = '';
    this.replyTo = null;
    this.editing = null;
    this.renderContextBar();
  }

  renderContextBar() {
    const { contextEl } = this.refs;
    if (!contextEl) return;
    clear(contextEl);
    const active = this.replyTo || this.editing;
    contextEl.hidden = !active;
    if (!active) return;

    const label = this.editing ? 'Editing message' : `Replying to ${this.replyTo.authorName}`;
    const text = this.editing ? this.editing.original : this.replyTo.preview;
    contextEl.append(
      el('div', { class: 'composer__context-body' }, [
        el('div', { class: 'composer__context-label', text: label }),
        el('div', { class: 'composer__context-text', text: text || '' }),
      ]),
      iconButton('x', 'Cancel', { className: 'icon-btn--sm', onClick: () => this.cancelContext() })
    );
  }

  /* ---------------- voice recording ---------------- */

  async toggleRecording() {
    if (this.recorder?.isActive) {
      await this.stopRecording();
      return;
    }
    this.recorder = new VoiceRecorder();
    try {
      await this.recorder.start();
      this.renderRecorderBar();
    } catch (error) {
      toast(error.message, { type: 'error' });
      this.recorder = null;
    }
  }

  renderRecorderBar() {
    const { recorderEl, composerRow } = this.refs;
    if (!recorderEl) return;
    clear(recorderEl);
    recorderEl.hidden = false;
    if (composerRow) composerRow.hidden = true;

    const bar = el('div', { class: 'recorder', role: 'status', 'aria-live': 'polite' });
    bar.append(
      el('span', { class: 'recorder__dot', 'aria-hidden': 'true' }),
      el('span', { class: 'recorder__time', id: 'recorder-time', text: '0:00' }),
      el('span', { class: 'recorder__label', text: 'Recording voice note' })
    );

    const pause = iconButton('pause', 'Pause recording', {
      onClick: () => {
        if (this.recorder.state === 'paused') {
          this.recorder.resume();
          pause.setAttribute('aria-label', 'Pause recording');
        } else {
          this.recorder.pause();
          pause.setAttribute('aria-label', 'Resume recording');
        }
      },
    });
    const cancel = iconButton('trash', 'Cancel recording', {
      className: 'icon-btn--danger',
      onClick: () => {
        this.recorder.cancel();
        this.recorder = null;
        this.hideRecorderBar();
      },
    });
    const stop = iconButton('check', 'Finish recording', {
      className: 'icon-btn--primary',
      onClick: () => this.stopRecording(),
    });
    bar.append(pause, cancel, stop);
    recorderEl.append(bar);
  }

  hideRecorderBar() {
    const { recorderEl, composerRow } = this.refs;
    if (recorderEl) {
      clear(recorderEl);
      recorderEl.hidden = true;
    }
    if (composerRow) composerRow.hidden = false;
  }

  async stopRecording() {
    if (!this.recorder) return;
    const result = await this.recorder.stop();
    this.recorder = null;
    this.hideRecorderBar();
    if (!result) {
      toast('Nothing was recorded.', { type: 'warning' });
      return;
    }
    const limits = getLimits();
    if (result.blob.size > limits.maxVoiceBytes) {
      toast('That voice note is too large to send.', { type: 'error' });
      return;
    }
    this.recording = result;
    this.renderPreviewTray();
  }

  /* ---------------- submit ---------------- */

  async submit() {
    const { inputEl, sendBtn } = this.refs;
    const id = this.activeId;
    if (!id) return;

    const conv = this.conversations.get(id);
    if (conv?.canSend === false) return;

    const text = (inputEl?.value || '').trim();
    const limits = getLimits();

    if (this.editing) {
      if (!text) return;
      if (text === this.editing.original) {
        this.cancelContext();
        return;
      }
      setBusy(sendBtn, true, 'Saving edit');
      try {
        await apiEditMessage(id, this.editing.id, text);
        this.cancelContext();
        inputEl.value = '';
      } catch (error) {
        toastApiError(error);
      } finally {
        setBusy(sendBtn, false);
      }
      return;
    }

    if (this.recording) {
      await this.sendVoice(this.recording, text);
      return;
    }

    if (this.draft) {
      await this.sendMedia(this.draft, text);
      return;
    }

    if (!text) return;
    if (text.length > limits.maxMessageLength) {
      toast(`Messages cannot be longer than ${limits.maxMessageLength} characters.`, { type: 'error' });
      return;
    }

    inputEl.value = '';
    inputEl.style.height = 'auto';
    if (this.refs.counterEl) this.refs.counterEl.hidden = true;
    const replyTo = this.replyTo;
    this.cancelContext();
    rt.typing(id, false);

    try {
      await sendText(id, text, { replyTo, sender: this.user });
      announce('Message sent.');
    } catch (error) {
      if (error instanceof ApiError && (error.isTimeout || error.isNetwork || error.isOffline)) {
        announce('Message status is being confirmed.');
      } else {
        toastApiError(error);
      }
    }
    this.scrollToBottom();
  }

  async sendMedia(draft, caption) {
    const id = this.activeId;
    const replyTo = this.replyTo;
    this.cancelContext();

    const posterUrl = draft.kind === 'video' && draft.poster ? URL.createObjectURL(draft.poster) : null;
    // Reuse the attachment client id from the start so store.byClientId is
    // indexed consistently and a retry can never duplicate.
    const optimistic = createOptimistic(id, {
      clientId: draft.clientId,
      kind: draft.kind,
      caption,
      media: {
        id: null,
        url: draft.previewUrl,
        thumbnailUrl: posterUrl || draft.previewUrl,
        mimeType: draft.mimeType,
        size: draft.size,
        width: draft.width,
        height: draft.height,
        duration: draft.duration,
        name: draft.name,
        status: 'uploading',
      },
      replyTo: replyTo ? { id: replyTo.id, available: true, authorName: replyTo.authorName, preview: replyTo.preview, kind: replyTo.kind } : null,
      sender: this.user,
    });
    void optimistic;

    if (this.refs.inputEl) this.refs.inputEl.value = '';
    // Hand the draft to the pending-upload lifecycle: clear the composer tray
    // WITHOUT disposing the File/Blob or the preview the bubble still shows.
    this.detachDraft();
    this.scrollToBottom();

    await this.performMediaUpload(id, draft, { caption, replyToId: replyTo?.id ?? null, posterUrl });
  }

  /**
   * Upload a retained draft and reconcile the optimistic message. On any
   * non-abort failure the draft (File/Blob + preview) is KEPT in pendingMedia
   * so retryMessage() can re-send it with the same client_id — the user never
   * has to reselect the file. The backend deduplicates on client_id, so a retry
   * after a lost response cannot create a duplicate server message.
   */
  async performMediaUpload(id, draft, { caption, replyToId, posterUrl = null }) {
    this.pendingMedia.set(draft.clientId, { draft, caption, replyToId, posterUrl });
    try {
      const created = await uploadDraft(id, draft, {
        caption,
        replyToId,
        onProgress: ({ percent }) => setLocalProgress(id, draft.clientId, percent),
      });
      confirmOptimistic(id, draft.clientId, created);
      // Success: the message now points at server media — release local resources.
      this.pendingMedia.delete(draft.clientId);
      this.disposePending({ draft, posterUrl });
      announce('Attachment sent.');
    } catch (error) {
      if (error instanceof ApiError && error.isAborted) {
        this.pendingMedia.delete(draft.clientId);
        this.disposePending({ draft, posterUrl });
        removeLocal(id, draft.clientId);
        return;
      }
      if (error instanceof ApiError && (error.isTimeout || error.isNetwork || error.isOffline)) {
        // Outcome unknown: keep the draft retained (do NOT dispose) so a retry
        // can re-send the same file.
        markLocalUnconfirmed(id, draft.clientId);
        announce('Upload status is being confirmed.');
      } else {
        markLocalFailed(id, draft.clientId, error);
        toastApiError(error);
      }
    }
  }

  async sendVoice(recording, caption) {
    const id = this.activeId;
    const file = new File([recording.blob], recording.name, { type: recording.mimeType });
    const draft = await createDraft(file, 'voice');
    draft.duration = recording.duration;
    // Detach the recording state without disposing the just-built draft.
    this.detachDraft();
    await this.sendMedia(draft, caption);
  }

  async retryMessage(message) {
    const id = this.activeId;
    if (message.kind === 'text') {
      try {
        await retryText(id, message.clientId);
      } catch (error) {
        toastApiError(error);
      }
      return;
    }

    // Media/voice: re-send the retained draft with the SAME client_id. No
    // reselection, no re-recording, and the backend dedupes so no duplicate.
    const pending = this.pendingMedia.get(message.clientId);
    if (pending?.draft) {
      markLocalSending(id, message.clientId);
      await this.performMediaUpload(id, pending.draft, {
        caption: pending.caption,
        replyToId: pending.replyToId,
        posterUrl: pending.posterUrl,
      });
      return;
    }

    // The draft is genuinely gone (e.g. the tab was reloaded — an in-memory
    // File cannot survive that). Reselection is the only remaining option.
    toast('This file is no longer available. Please attach it again to send.', { type: 'info' });
    removeLocal(id, message.clientId);
  }

  /* ============================================================
     Details panel
     ============================================================ */

  openDetails(conv) {
    const { detailsEl, layoutEl } = this.refs;
    if (!detailsEl || !conv) return;
    clear(detailsEl);

    const header = el('div', { class: 'details-pane__header' });
    header.append(el('h2', { class: 'card__title', text: conv.isGroup ? 'Group information' : 'Contact information' }));
    header.append(
      iconButton('x', 'Close details', {
        className: 'mt-auto',
        onClick: () => {
          layoutEl.dataset.details = '';
          document.querySelector('.details-scrim')?.remove();
        },
      })
    );

    const body = el('div', { class: 'details-pane__body stack' });
    body.append(
      el('div', { class: 'details-pane__profile' }, [
        avatar(conv.title, conv.avatarUrl, { size: 'xl' }),
        el('h3', { text: conv.title }),
        conv.description ? el('p', { class: 'text-sm text-muted', text: conv.description }) : null,
      ])
    );

    if (conv.isGroup) {
      const link = el('a', { class: 'btn btn--block', href: `groups.html?g=${encodeURIComponent(conv.groupId || conv.id)}`, text: 'Open group details' });
      body.append(link);
    } else if (conv.counterpart) {
      const dl = el('dl', { class: 'dl' });
      if (conv.counterpart.phone) {
        dl.append(el('dt', { text: 'Phone' }), el('dd', { text: conv.counterpart.phone }));
      }
      const presence = getPresence(conv.counterpart.id);
      if (presence.status !== 'hidden' && presence.status !== 'unknown') {
        dl.append(el('dt', { text: 'Status' }), el('dd', { text: presenceLabel(conv.counterpart.id, { formatRelative }) || 'Offline' }));
      }
      body.append(dl);
      if (isAdmin()) {
        const link = el('a', { class: 'btn btn--block', href: `members.html?m=${encodeURIComponent(conv.counterpart.id)}`, text: 'View member record' });
        body.append(link);
      }
    }

    detailsEl.append(header, body);
    layoutEl.dataset.details = 'open';

    if (window.matchMedia('(max-width: 1279px)').matches) {
      const scrim = el('div', { class: 'details-scrim' });
      scrim.addEventListener('click', () => {
        layoutEl.dataset.details = '';
        scrim.remove();
      });
      document.body.append(scrim);
    }
  }

  /* ============================================================
     Store + realtime wiring
     ============================================================ */

  bindStoreEvents() {
    const rerenderIfActive = (convId) => {
      if (String(convId) !== this.activeId) return;
      const wasAtBottom = this.atBottom;
      this.renderMessages({ scrollToBottom: false });
      this.updateTypingRow();
      if (wasAtBottom) this.scrollToBottom();
    };

    messageEvents.on('added', (convId, message) => {
      if (String(convId) !== this.activeId) return;
      const wasAtBottom = this.atBottom;
      this.renderMessages({ scrollToBottom: false });
      if (wasAtBottom || message.outgoing) this.scrollToBottom();
      else {
        this.newWhileAway += 1;
        this.updateJumpButton();
      }
      if (!message.outgoing && wasAtBottom) this.markRead();
    });

    messageEvents.on('updated', (convId) => rerenderIfActive(convId));
    messageEvents.on('removed', (convId) => rerenderIfActive(convId));
    messageEvents.on('reconciled', (convId) => rerenderIfActive(convId));

    messageEvents.on('progress', (convId, message) => {
      if (String(convId) !== this.activeId) return;
      const node = this.rendered.get(message.clientId);
      const bar = node?.querySelector('.media-upload .progress__bar');
      const label = node?.querySelector('.media-upload span');
      if (bar) bar.style.width = `${message.progress}%`;
      if (label) label.textContent = `Uploading ${message.progress}%`;
      if (!bar) rerenderIfActive(convId);
    });

    presenceEvents.on('typing', (convId) => {
      if (String(convId) === this.activeId) {
        this.updateThreadStatus();
        this.updateTypingRow();
      }
    });
    presenceEvents.on('presence', () => this.updateThreadStatus());
  }

  bindRealtime() {
    // New message anywhere: update the list ordering + previews.
    socketEvents.on('message.new', (payload) => {
      const convId = String(payload?.conversation_id ?? payload?.message?.conversation_id ?? '');
      if (!convId) return;
      const raw = payload.message || payload;
      const message = normalizeMessage(raw, { currentUserId: this.user?.id });

      let conv = this.conversations.get(convId);
      if (!conv) {
        // A conversation we don't know about yet — fetch it rather than invent it.
        this.hydrateConversation(convId);
        return;
      }
      conv.lastMessage = message;
      conv.previewText = messagePreview(message);
      conv.lastActivity = message.createdAt;
      if (convId !== this.activeId && !message.outgoing) incrementConversationUnread(convId, 1);
      this.renderList();

      if (convId === this.activeId && !message.outgoing) {
        rt.markDelivered([message.id]);
        if (this.atBottom) this.markRead();
      }
    });

    socketEvents.on('conversation.updated', (payload) => {
      if (payload?.conversation_id && payload?.last_message && !payload?.id && !payload?.conversation) {
        const convId = String(payload.conversation_id);
        const conv = this.conversations.get(convId);
        if (!conv) {
          this.hydrateConversation(convId);
          return;
        }
        const message = normalizeMessage(payload.last_message, { currentUserId: this.user?.id });
        if (message) {
          conv.lastMessage = message;
          conv.previewText = messagePreview(message);
          conv.lastActivity = message.createdAt || conv.lastActivity;
          this.renderList();
        }
        return;
      }
      const raw = payload?.conversation || payload;
      if (!raw?.id) return;
      const conv = normalizeConversation(raw, this.user);
      this.conversations.set(conv.id, conv);
      if (!this.order.includes(conv.id)) this.order.push(conv.id);
      this.renderList();
      if (conv.id === this.activeId) {
        this.renderThreadHeader(conv);
        this.renderComposerState(conv);
      }
    });

    socketEvents.on('conversation.created', (payload) => {
      const raw = payload?.conversation || payload;
      if (raw?.id) this.hydrateConversation(raw.id);
    });

    socketEvents.on('group.membership', () => {
      this.loadConversations({ reset: true });
    });

    socketEvents.on('group.removed', (payload) => {
      const convId = String(payload?.conversation_id ?? '');
      if (!convId) return;
      this.conversations.delete(convId);
      this.order = this.order.filter((x) => x !== convId);
      this.renderList();
      if (this.activeId === convId) {
        this.activeId = null;
        this.renderEmptyThread();
        toast('You no longer have access to that group.', { type: 'info' });
      }
    });

    socketEvents.on('state', (state) => {
      if (state !== 'open') return;
      if (this.activeId) rt.joinConversation(this.activeId);
      this.loadConversations({ reset: true });
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && this.activeId && this.atBottom) this.markRead();
    });
  }

  async hydrateConversation(conversationId) {
    try {
      const detail = await api.conversations.get(conversationId);
      const conv = normalizeConversation(detail, this.user);
      this.conversations.set(conv.id, conv);
      if (!this.order.includes(conv.id)) this.order.unshift(conv.id);
      this.renderList();
    } catch {
      // Unauthorized or removed — nothing to show.
    }
  }
}

/* ============================================================
   Helpers
   ============================================================ */

/** Normalize a backend conversation payload into the view model. */
export function normalizeConversation(raw, user) {
  const id = String(raw.id ?? raw.conversation_id ?? '');
  const type = String(raw.type || raw.kind || (raw.group ? 'group' : 'direct')).toLowerCase();
  const isGroup = type === 'group';
  const participants = Array.isArray(raw.participants) ? raw.participants : [];
  const counterpart =
    raw.counterpart ||
    raw.other_participant ||
    participants.find((p) => String(p.id) !== String(user?.id)) ||
    null;

  const lastRaw = raw.last_message || raw.latest_message || null;
  const lastMessage = lastRaw ? normalizeMessage(lastRaw, { currentUserId: user?.id }) : null;

  const title = isGroup
    ? raw.name || raw.title || 'Group'
    : raw.title || counterpart?.display_name || counterpart?.name || (raw.is_admin_thread ? 'Administrator' : 'Conversation');

  return {
    id,
    isGroup,
    groupId: raw.group_id ?? raw.group?.id ?? null,
    isAdminThread: !!(raw.is_admin_thread || counterpart?.is_admin),
    title,
    description: raw.description || '',
    avatarUrl: resolveMediaUrl(raw.image_url || raw.avatar_url || counterpart?.avatar_url || null),
    counterpart: counterpart
      ? {
          id: String(counterpart.id),
          display_name: counterpart.display_name || counterpart.name || '',
          phone: counterpart.phone || '',
          avatar_url: counterpart.avatar_url || null,
          online: counterpart.online,
          last_seen: counterpart.last_seen,
          presence_visible: counterpart.presence_visible,
          is_admin: !!counterpart.is_admin,
        }
      : null,
    memberCount: Number(raw.member_count ?? raw.members_count ?? participants.length) || 0,
    lastMessage,
    previewText: lastMessage ? messagePreview(lastMessage) : raw.preview || 'No messages yet',
    lastActivity: raw.last_activity_at || raw.updated_at || lastMessage?.createdAt || raw.created_at || null,
    pinned: !!raw.is_pinned,
    canSend: raw.can_send !== false && raw.is_archived !== true,
    readOnlyReason: raw.read_only_reason || (raw.is_archived ? 'This conversation is archived.' : ''),
    raw,
  };
}

function statusIconFor(status) {
  switch (status) {
    case STATUS.SENDING: return 'clock';
    case STATUS.UNCONFIRMED: return 'alert-circle';
    case STATUS.SENT: return 'check';
    case STATUS.DELIVERED: return 'check-check';
    case STATUS.READ: return 'check-check';
    case STATUS.FAILED: return 'alert-triangle';
    default: return 'check';
  }
}

function previewIconFor(kind) {
  if (kind === 'image') return 'image';
  if (kind === 'video') return 'video';
  if (kind === 'voice') return 'mic';
  if (kind === 'file') return 'paperclip';
  return 'message-square';
}

function previewLabelFor(kind) {
  if (kind === 'image') return 'Photo';
  if (kind === 'video') return 'Video';
  if (kind === 'voice') return 'Voice note';
  return 'Message';
}

function draftMeta(draft) {
  const parts = [];
  if (draft.size) parts.push(formatBytesLocal(draft.size));
  if (draft.duration) parts.push(formatDuration(draft.duration));
  if (draft.width && draft.height) parts.push(`${draft.width}×${draft.height}`);
  return parts.join(' · ');
}

function formatBytesLocal(bytes) {
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function isMobileDevice() {
  return window.matchMedia('(pointer: coarse)').matches;
}

function cssEscape(value) {
  if (window.CSS?.escape) return CSS.escape(String(value));
  return String(value).replace(/["\\]/g, '\\$&');
}

export default ChatController;
