/**
 * NEXORA — navigation.js
 * Application shell: application header (hamburger + top-right theme/profile),
 * primary navigation, the mobile navigation drawer, role-aware entries,
 * unread badges, connection banner and sign-out.
 *
 * Layout contract (see responsive.css):
 *   >= 1024px  full sidebar               + slim header carrying theme/profile
 *   768-1023px icon-only nav rail         + slim header carrying theme/profile
 *   <= 767px   fixed BOTTOM navigation bar + slim header carrying theme/profile
 *              (secondary destinations live in the top-right profile menu; the
 *               drawer below is a legacy fallback and is not surfaced at this
 *               breakpoint — the hamburger is hidden by responsive.css).
 *
 * Role filtering here is UX only. Every hidden destination is still enforced
 * by the backend; a member typing admin.html receives 403 from the API.
 */

import { authEvents, getUser, isAdmin, logout } from './auth.js';
import { apiEvents, resolveMediaUrl } from './api.js';
import { getThemePreference, setTheme, applyBranding, getConfig } from './theme.js';
import { createConnectionUX } from './connection-ux.js';
import { avatar, icon, iconButton, openMenu, toast } from './ui.js';
import { clear, el, formatCount, prefs, trapFocus } from './utils.js';
import { realtime, socketEvents } from './websocket.js';
import { unreadEvents, getUnread } from './notifications.js';

const NAV_COLLAPSE_KEY = 'navCollapsed';

/** page key -> nav definition */
const NAV_ITEMS = [
  { key: 'admin', href: 'admin.html', label: 'Dashboard', icon: 'layout-dashboard', adminOnly: true },
  { key: 'chat', href: 'chat.html', label: 'Chats', icon: 'message-square', badge: 'conversations' },
  { key: 'groups', href: 'groups.html', label: 'Groups', icon: 'messages-square', badge: 'groups' },
  { key: 'members', href: 'members.html', label: 'Members', icon: 'users', adminOnly: true },
];

/** Secondary destinations. Profile now lives in the header profile menu too. */
const FOOTER_ITEMS = [
  { key: 'settings', href: 'settings.html', label: 'Settings', icon: 'settings' },
];

/** Destinations offered inside the mobile drawer, in order. */
const DRAWER_ITEMS = [
  ...NAV_ITEMS,
  { key: 'profile', href: 'profile.html', label: 'Profile', icon: 'user' },
  { key: 'settings', href: 'settings.html', label: 'Settings', icon: 'settings' },
];

let navRoot = null;
let bannerRoot = null;

/**
 * Paint the current (cached or live) branding into brand slots that were just
 * created by the shell. applyBranding queries the whole document, so calling it
 * after the nav/header/drawer nodes exist is what stops freshly-mounted logo
 * and organization-name slots from rendering empty. Side effects (favicon /
 * PWA manifest) are skipped — those are owned by theme.loadBranding().
 */
function paintBrand() {
  applyBranding(getConfig(), { sideEffects: false });
}
let activeKey = null;
let headerRoot = null;
let drawer = null;
let viewportSizingMounted = false;

/**
 * Android Chrome/PWA occasionally leaves 100dvh at the keyboard-resized value
 * after the keyboard closes. VisualViewport is the browser's authoritative
 * visible area, so mirror it into CSS without fixed device-specific heights.
 */
function mountViewportSizing() {
  if (viewportSizingMounted) return;
  viewportSizingMounted = true;
  const viewport = window.visualViewport;
  const update = () => {
    const height = Math.round(viewport?.height || window.innerHeight || 0);
    if (height > 0) document.documentElement.style.setProperty('--app-viewport-height', `${height}px`);
  };
  update();
  viewport?.addEventListener('resize', update, { passive: true });
  viewport?.addEventListener('scroll', update, { passive: true });
  window.addEventListener('resize', update, { passive: true });
}

/**
 * Mount the primary navigation.
 * @param {object} options { active: string, container?: HTMLElement }
 */
export function mountNavigation(options = {}) {
  mountViewportSizing();
  activeKey = options.active || document.body.dataset.page || null;
  navRoot = options.container || document.getElementById('app-nav');
  if (!navRoot) return;

  const shell = document.getElementById('app-shell');
  if (shell && prefs.get(NAV_COLLAPSE_KEY, false)) shell.dataset.nav = 'collapsed';

  mountAppHeader();
  render();
  authEvents.on('user', renderAll);
  authEvents.on('user-updated', renderAll);
  unreadEvents.on('change', updateBadges);
}

function renderAll() {
  render();
  renderHeader();
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

  /* ---- footer ----
     Profile, Appearance and Sign out deliberately live in the header's
     top-right control group (and in the mobile drawer), not buried here. */
  const footer = el('div', { class: 'app-nav__footer' });
  for (const item of FOOTER_ITEMS) footer.append(navEntry(item, { bare: true }));

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
  // The brand slots above were created empty; paint the current branding into
  // them (they exist now, so this is not a no-op like an earlier applyBranding).
  paintBrand();
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
   Application header — hamburger (mobile) + top-right controls
   ============================================================ */

/**
 * Insert the application header at the top of the work area of every
 * authenticated page. It is created here rather than in each HTML file so the
 * markup, the roles and the behaviour cannot drift apart between pages.
 */
function mountAppHeader() {
  headerRoot = document.getElementById('app-header');
  if (!headerRoot) {
    const main = document.querySelector('.app-main');
    if (!main) return;
    headerRoot = el('header', { class: 'app-header', id: 'app-header' });
    main.prepend(headerRoot);
  }
  renderHeader();
}

function renderHeader() {
  if (!headerRoot) return;
  clear(headerRoot);

  /* ---- hamburger (mobile only; hidden with CSS from 768px up) ---- */
  const menuBtn = el('button', {
    type: 'button',
    class: 'app-header__menu',
    id: 'nav-toggle',
    'aria-label': 'Open navigation menu',
    'aria-haspopup': 'dialog',
    'aria-expanded': 'false',
    'aria-controls': 'app-drawer',
  });
  menuBtn.append(icon('menu'), el('span', { class: 'sr-only', text: 'Menu' }));
  menuBtn.addEventListener('click', () => openDrawer(menuBtn));
  headerRoot.append(menuBtn);

  /* ---- compact brand (mobile context) ---- */
  const brand = el('div', { class: 'app-header__brand' });
  brand.append(
    el('span', { class: 'app-header__logo-slot', 'data-brand': 'logo' }),
    el('span', { class: 'app-header__name truncate', 'data-brand': 'org-name', text: '' })
  );
  headerRoot.append(brand);

  headerRoot.append(el('span', { class: 'spacer' }));

  /* ---- top-right control group: [Theme] [Profile] ---- */
  const controls = el('div', { class: 'app-header__controls' });

  const themeBtn = iconButton(themeIconName(), 'Change appearance', {
    className: 'app-header__control',
  });
  themeBtn.id = 'theme-toggle';
  themeBtn.addEventListener('click', () => openThemeMenu(themeBtn));
  controls.append(themeBtn);

  const user = getUser();
  const profileBtn = el('button', {
    type: 'button',
    class: 'app-header__profile',
    id: 'profile-menu-button',
    'aria-label': 'Open your account menu',
    'aria-haspopup': 'menu',
  });
  profileBtn.append(avatar(user?.full_name || user?.name || 'You', resolveAvatarUrl(user), { size: 'sm' }));
  profileBtn.addEventListener('click', () => openProfileMenu(profileBtn));
  controls.append(profileBtn);

  headerRoot.append(controls);
  // Paint branding into the header's freshly-created logo/name slots.
  paintBrand();
}

function resolveAvatarUrl(user) {
  const raw = user?.avatar_url || user?.avatar || null;
  return raw ? resolveMediaUrl(raw) : null;
}

/* ============================================================
   Profile menu (header, both breakpoints)
   ============================================================ */

function openProfileMenu(anchor) {
  const user = getUser();
  const admin = isAdmin();
  const name = user?.full_name || user?.name || 'Signed in';
  const role = admin ? 'Administrator' : 'Member';

  openMenu(anchor, [
    { label: `${name} · ${role}`, icon: admin ? 'shield-check' : 'user', disabled: true },
    { separator: true },
    { label: 'Profile', icon: 'user', onClick: () => navigateTo('profile.html') },
    { label: 'Settings', icon: 'settings', onClick: () => navigateTo('settings.html') },
    { separator: true },
    { label: 'Sign out', icon: 'log-out', danger: true, onClick: () => logout() },
  ]);
}

function navigateTo(href) {
  window.location.assign(href);
}

/* ============================================================
   Mobile navigation drawer
   ============================================================ */

let releaseDrawerFocus = null;
let drawerOpener = null;

function buildDrawer() {
  const admin = isAdmin();
  const user = getUser();

  const backdrop = el('div', { class: 'nav-drawer__backdrop', 'data-drawer-backdrop': '' });
  const panel = el('div', {
    class: 'nav-drawer__panel',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': 'Navigation',
  });

  /* header row: brand + close */
  const head = el('div', { class: 'nav-drawer__head' });
  head.append(
    el('span', { class: 'app-header__logo-slot', 'data-brand': 'logo' }),
    el('span', { class: 'nav-drawer__title truncate', 'data-brand': 'org-name', text: 'NEXORA' }),
    el('span', { class: 'spacer' })
  );
  const closeBtn = iconButton('x', 'Close navigation menu', { className: 'nav-drawer__close' });
  closeBtn.addEventListener('click', () => closeDrawer());
  head.append(closeBtn);
  panel.append(head);

  /* identity */
  if (user) {
    const who = el('div', { class: 'nav-drawer__user' });
    who.append(avatar(user.full_name || user.name || 'You', resolveAvatarUrl(user), {}));
    who.append(
      el('div', { class: 'nav-drawer__identity' }, [
        el('span', { class: 'nav-drawer__name truncate', text: user.full_name || user.name || 'Signed in' }),
        el('span', { class: 'nav-drawer__role', text: admin ? 'Administrator' : 'Member' }),
      ])
    );
    panel.append(who);
  }

  /* destinations (role aware — the backend still enforces access) */
  const list = el('nav', { class: 'nav-drawer__list', 'aria-label': 'Primary' });
  const unread = getUnread();
  for (const item of DRAWER_ITEMS) {
    if (item.adminOnly && !admin) continue;
    const link = el('a', { class: 'nav-drawer__item', href: item.href });
    if (activeKey === item.key) link.setAttribute('aria-current', 'page');
    link.append(icon(item.icon), el('span', { class: 'nav-drawer__label', text: item.label }));
    const count = Number(unread[item.badge] || 0);
    if (item.badge && count > 0) {
      link.append(el('span', { class: 'badge', text: formatCount(count), 'aria-label': `${count} unread` }));
    }
    // Close before navigating so focus is restored even for in-page targets.
    link.addEventListener('click', () => closeDrawer({ restoreFocus: false }));
    list.append(link);
  }
  panel.append(list);

  /* appearance + sign out */
  const foot = el('div', { class: 'nav-drawer__footer' });
  const themeBtn = el('button', { type: 'button', class: 'nav-drawer__item', 'aria-label': 'Change appearance' });
  themeBtn.append(icon(themeIconName()), el('span', { class: 'nav-drawer__label', text: 'Appearance' }));
  themeBtn.addEventListener('click', () => openThemeMenu(themeBtn));
  foot.append(themeBtn);

  const outBtn = el('button', { type: 'button', class: 'nav-drawer__item nav-drawer__item--danger' });
  outBtn.append(icon('log-out'), el('span', { class: 'nav-drawer__label', text: 'Sign out' }));
  outBtn.addEventListener('click', async () => {
    outBtn.disabled = true;
    await logout();
  });
  foot.append(outBtn);
  panel.append(foot);

  const root = el('div', { class: 'nav-drawer', id: 'app-drawer', hidden: true });
  root.append(backdrop, panel);
  backdrop.addEventListener('click', () => closeDrawer());
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      closeDrawer();
    }
  });
  return root;
}

export function openDrawer(opener = null) {
  closeDrawer({ restoreFocus: false });
  drawerOpener = opener || document.getElementById('nav-toggle');
  drawer = buildDrawer();
  document.body.append(drawer);
  // Paint branding into the drawer's brand slots now that they are in the DOM.
  paintBrand();
  // Force a frame so the CSS transition runs from the closed position.
  drawer.hidden = false;
  requestAnimationFrame(() => drawer?.setAttribute('data-open', 'true'));
  document.body.classList.add('no-scroll');
  drawerOpener?.setAttribute('aria-expanded', 'true');
  releaseDrawerFocus = trapFocus(drawer);
  drawer.querySelector('.nav-drawer__close')?.focus();
}

export function closeDrawer({ restoreFocus = true } = {}) {
  if (!drawer) return;
  releaseDrawerFocus?.();
  releaseDrawerFocus = null;
  drawer.remove();
  drawer = null;
  document.body.classList.remove('no-scroll');
  const opener = drawerOpener || document.getElementById('nav-toggle');
  opener?.setAttribute('aria-expanded', 'false');
  if (restoreFocus) opener?.focus();
  drawerOpener = null;
}

export function isDrawerOpen() {
  return !!drawer;
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
  // theme.js remains the single source of truth for the theme; this only
  // repaints the controls that show which preference is active.
  setTheme(pref);
  render();
  renderHeader();
  if (isDrawerOpen()) {
    const opener = document.getElementById('nav-toggle');
    closeDrawer({ restoreFocus: false });
    openDrawer(opener);
  }
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
 * Mount the connection indicator.
 *
 * Design contract (see connection-ux.js for the policy):
 *   - normal operation shows NOTHING;
 *   - a blip shorter than the grace period shows NOTHING;
 *   - a persistent disruption shows a compact, fixed, non-blocking pill that
 *     never pushes layout, never covers the composer, never steals focus and
 *     never interrupts modals (it sits below the modal layer);
 *   - offline shows a compact pill immediately;
 *   - recovery is one subtle, self-fading hint — no repeated flashing.
 *
 * The transport keeps working regardless: REST stays authoritative and the
 * socket reconnects on its own schedule; this is display only.
 */
export function mountConnectionBanner(container) {
  bannerRoot = container || document.getElementById('conn-banner');
  if (!bannerRoot) return;
  bannerRoot.setAttribute('role', 'status');
  bannerRoot.setAttribute('aria-live', 'polite');

  const paint = (view) => {
    clear(bannerRoot);
    bannerRoot.dataset.state = '';

    switch (view.mode) {
      case 'offline':
        bannerRoot.dataset.state = 'offline';
        bannerRoot.append(
          icon('wifi-off', { size: 15 }),
          el('span', { text: "You're offline. Messages will send when you reconnect." })
        );
        return;

      case 'disrupted':
        bannerRoot.dataset.state = 'reconnecting';
        bannerRoot.append(
          el('span', { class: 'spinner spinner--sm' }),
          el('span', { text: 'Reconnecting…' }),
          el('span', { class: 'conn-banner__hint', text: 'Messages still send.' })
        );
        return;

      case 'failed':
        bannerRoot.dataset.state = 'error';
        bannerRoot.append(
          icon('cloud-off', { size: 15 }),
          el('span', { text: 'Live updates are disconnected.' })
        );
        const retry = el('button', { type: 'button', class: 'btn btn--sm btn--ghost', text: 'Reconnect' });
        retry.addEventListener('click', () => realtime.restart());
        bannerRoot.append(retry);
        return;

      case 'recovered':
        bannerRoot.dataset.state = 'recovered';
        bannerRoot.append(icon('wifi', { size: 15 }), el('span', { text: 'Back online' }));
        return;

      case 'hidden':
      default:
        return;
    }
  };

  const ux = createConnectionUX({ onChange: paint });

  socketEvents.on('state', (state, detail) => {
    // stop() during logout/session-expiry is expected: no permanent "failed"
    // pill for a page that is navigating away to the sign-in screen.
    if (state === 'closed' && detail && (detail.reason === 'logout' || detail.reason === 'session-expired')) {
      ux.destroy();
      paint({ mode: 'hidden' });
      return;
    }
    ux.handleState(state, detail);
  });
  window.addEventListener('online', () => ux.handleOnline(realtime.state));
  window.addEventListener('offline', () => ux.handleOffline());
  apiEvents.on('offline', () => ux.handleOffline());
  ux.handleState(realtime.state);
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

export default {
  mountNavigation,
  mountConnectionBanner,
  mountSessionGuards,
  mobile,
  navToggleButton,
  openDrawer,
  closeDrawer,
};
