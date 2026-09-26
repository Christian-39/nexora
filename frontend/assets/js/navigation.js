/**
 * NEXORA — navigation.js
 * Application shell: primary navigation, role-aware entries, unread badges,
 * connection banner, theme control and sign-out.
 *
 * Role filtering here is UX only. Every hidden destination is still enforced
 * by the backend; a member typing admin.html receives 403 from the API.
 */

import { authEvents, getUser, isAdmin, logout } from './auth.js';
import { apiEvents } from './api.js';
import { getThemePreference, setTheme } from './theme.js';
import { icon, iconButton, openMenu, toast } from './ui.js';
import { clear, el, formatCount, prefs } from './utils.js';
import { connectionLabel, realtime, socketEvents } from './websocket.js';
import { unreadEvents, getUnread } from './notifications.js';

const NAV_COLLAPSE_KEY = 'navCollapsed';

/** page key -> nav definition */
const NAV_ITEMS = [
  { key: 'admin', href: 'admin.html', label: 'Dashboard', icon: 'layout-dashboard', adminOnly: true },
  { key: 'chat', href: 'chat.html', label: 'Chats', icon: 'message-square', badge: 'conversations' },
  { key: 'groups', href: 'groups.html', label: 'Groups', icon: 'messages-square', badge: 'groups' },
  { key: 'members', href: 'members.html', label: 'Members', icon: 'users', adminOnly: true },
];

const FOOTER_ITEMS = [
  { key: 'profile', href: 'profile.html', label: 'Profile', icon: 'user' },
  { key: 'settings', href: 'settings.html', label: 'Settings', icon: 'settings' },
];

let navRoot = null;
let bannerRoot = null;
let activeKey = null;

/**
 * Mount the primary navigation.
 * @param {object} options { active: string, container?: HTMLElement }
 */
export function mountNavigation(options = {}) {
  activeKey = options.active || document.body.dataset.page || null;
  navRoot = options.container || document.getElementById('app-nav');
  if (!navRoot) return;

  const shell = document.getElementById('app-shell');
  if (shell && prefs.get(NAV_COLLAPSE_KEY, false)) shell.dataset.nav = 'collapsed';

  render();
  authEvents.on('user', render);
  authEvents.on('user-updated', render);
  unreadEvents.on('change', updateBadges);
}

function render() {
  if (!navRoot) return;
  clear(navRoot);
  const admin = isAdmin();
  const user = getUser();

  /* ---- brand ---- */
  const brand = el('div', { class: 'app-nav__brand' });
  const logoSlot = el('span', { 'data-brand': 'logo', class: 'app-nav__logo-slot' });
  brand.append(logoSlot, el('span', { class: 'app-nav__name truncate', 'data-brand': 'org-name', text: '' }));
  navRoot.append(brand);

  /* ---- primary list ---- */
  const list = el('ul', { class: 'app-nav__list' });
  for (const item of NAV_ITEMS) {
    if (item.adminOnly && !admin) continue;
    list.append(navEntry(item));
  }
  navRoot.append(list);

  /* ---- footer ---- */
  const footer = el('div', { class: 'app-nav__footer' });
  for (const item of FOOTER_ITEMS) footer.append(navEntry(item, { bare: true }));

  const themeBtn = el('button', { type: 'button', class: 'nav-item', 'aria-label': 'Change appearance' });
  themeBtn.append(icon(themeIconName()), el('span', { class: 'nav-item__label', text: 'Appearance' }));
  themeBtn.addEventListener('click', () => openThemeMenu(themeBtn));
  footer.append(themeBtn);

  const outBtn = el('button', { type: 'button', class: 'nav-item', 'aria-label': 'Sign out' });
  outBtn.append(icon('log-out'), el('span', { class: 'nav-item__label', text: 'Sign out' }));
  outBtn.addEventListener('click', async () => {
    outBtn.disabled = true;
    await logout();
  });
  footer.append(outBtn);

  if (user) {
    const meta = el('div', {
      class: 'nav-item',
      style: { pointerEvents: 'none', opacity: '0.85' },
    });
    meta.append(
      icon(admin ? 'shield-check' : 'circle', { size: 16 }),
      el('span', { class: 'nav-item__label text-xs truncate', text: admin ? 'Administrator' : 'Member' })
    );
    footer.append(meta);
  }

  navRoot.append(footer);
  updateBadges();
}

function navEntry(item, { bare = false } = {}) {
  const current = activeKey === item.key;
  const link = el('a', {
    class: 'nav-item',
    href: item.href,
    'aria-label': item.label,
  });
  if (current) link.setAttribute('aria-current', 'page');
  link.append(icon(item.icon), el('span', { class: 'nav-item__label', text: item.label }));
  if (item.badge) {
    link.append(el('span', { class: 'badge nav-item__badge', dataset: { badge: item.badge }, hidden: true }));
  }
  return bare ? link : el('li', {}, [link]);
}

function updateBadges() {
  if (!navRoot) return;
  const unread = getUnread();
  const map = {
    conversations: unread.conversations,
    groups: unread.groups,
    notifications: unread.notifications,
  };
  for (const node of navRoot.querySelectorAll('[data-badge]')) {
    const value = Number(map[node.dataset.badge] || 0);
    if (value > 0) {
      node.textContent = formatCount(value);
      node.hidden = false;
      node.setAttribute('aria-label', `${value} unread`);
    } else {
      node.textContent = '';
      node.hidden = true;
    }
  }
}

/* ============================================================
   Theme menu
   ============================================================ */

function themeIconName() {
  const pref = getThemePreference();
  if (pref === 'light') return 'sun';
  if (pref === 'dark') return 'moon';
  return 'monitor';
}

function openThemeMenu(anchor) {
  openMenu(anchor, [
    { label: 'Light', icon: 'sun', onClick: () => applyThemeChoice('light') },
    { label: 'Dark', icon: 'moon', onClick: () => applyThemeChoice('dark') },
    { label: 'Match system', icon: 'monitor', onClick: () => applyThemeChoice('system') },
  ]);
}

function applyThemeChoice(pref) {
  setTheme(pref);
  render();
}

/* ============================================================
   Sidebar collapse (desktop)
   ============================================================ */

export function toggleNavCollapsed() {
  const shell = document.getElementById('app-shell');
  if (!shell) return;
  const collapsed = shell.dataset.nav === 'collapsed';
  shell.dataset.nav = collapsed ? '' : 'collapsed';
  prefs.set(NAV_COLLAPSE_KEY, !collapsed);
}

export function navToggleButton() {
  return iconButton('panel-left', 'Toggle navigation', { onClick: toggleNavCollapsed });
}

/* ============================================================
   Connection banner
   ============================================================ */

/**
 * Mount the "offline / reconnecting / connected" banner.
 * Never claims delivery; it only reports transport state.
 */
export function mountConnectionBanner(container) {
  bannerRoot = container || document.getElementById('conn-banner');
  if (!bannerRoot) return;
  bannerRoot.setAttribute('role', 'status');
  bannerRoot.setAttribute('aria-live', 'polite');

  const paint = (state) => {
    const effective = navigator.onLine === false ? 'offline' : state;
    clear(bannerRoot);

    if (effective === 'open') {
      if (bannerRoot.dataset.state && bannerRoot.dataset.state !== 'connected') {
        bannerRoot.dataset.state = 'connected';
        bannerRoot.append(icon('wifi', { size: 15 }), el('span', { text: 'Connected' }));
        setTimeout(() => {
          if (bannerRoot.dataset.state === 'connected') bannerRoot.dataset.state = '';
        }, 1800);
      } else {
        bannerRoot.dataset.state = '';
      }
      return;
    }

    if (effective === 'offline') {
      bannerRoot.dataset.state = 'offline';
      bannerRoot.append(icon('wifi-off', { size: 15 }), el('span', { text: "You're offline. Messages will not send until you reconnect." }));
      return;
    }

    if (effective === 'reconnecting' || effective === 'connecting') {
      bannerRoot.dataset.state = 'reconnecting';
      bannerRoot.append(el('span', { class: 'spinner', style: { width: '13px', height: '13px' } }), el('span', { text: connectionLabel(effective) }));
      return;
    }

    if (effective === 'closed') {
      bannerRoot.dataset.state = 'error';
      bannerRoot.append(icon('alert-circle', { size: 15 }), el('span', { text: 'Disconnected from live updates.' }));
      const retry = el('button', { type: 'button', class: 'btn btn--sm btn--ghost', text: 'Reconnect' });
      retry.addEventListener('click', () => realtime.restart());
      bannerRoot.append(retry);
      return;
    }

    bannerRoot.dataset.state = '';
  };

  socketEvents.on('state', paint);
  window.addEventListener('online', () => paint(realtime.state));
  window.addEventListener('offline', () => paint('offline'));
  apiEvents.on('offline', () => paint('offline'));
  paint(realtime.state);
}

/* ============================================================
   Session expiry handling (shared by every authenticated page)
   ============================================================ */

let expiryNotified = false;

export function mountSessionGuards() {
  authEvents.on('session-expired', () => {
    if (expiryNotified) return;
    expiryNotified = true;
    toast('Your session has ended. Please sign in again.', { type: 'warning', duration: 0 });
    setTimeout(() => {
      window.location.replace('login.html?reason=expired');
    }, 1200);
  });

  apiEvents.on('forbidden', () => {
    toast('You are not authorized to perform this action.', { type: 'error' });
  });

  socketEvents.on('unauthorized', () => {
    authEvents.emit('session-expired');
  });
}

/* ============================================================
   Mobile pane helpers (chat list ⇄ thread)
   ============================================================ */

export const mobile = {
  get isSmall() {
    return window.matchMedia('(max-width: 767px)').matches;
  },
  showThread() {
    const shell = document.getElementById('app-shell');
    const layout = document.querySelector('.chat-layout');
    if (layout) layout.dataset.mobilePane = 'thread';
    if (shell) shell.dataset.mobileView = 'thread';
  },
  showList() {
    const shell = document.getElementById('app-shell');
    const layout = document.querySelector('.chat-layout');
    if (layout) layout.dataset.mobilePane = 'list';
    if (shell) shell.dataset.mobileView = 'list';
  },
};

export default { mountNavigation, mountConnectionBanner, mountSessionGuards, mobile, navToggleButton };
