/**
 * NEXORA — ui.js
 * Shared UI primitives: icons, toasts, modals, confirmation, lightbox,
 * skeletons, empty states, live-region announcements, busy states.
 * No network calls, no application state.
 */

import { $, clear, el, trapFocus, uid } from './utils.js';

/* ============================================================
   Icons — Lucide-compatible outline paths, inlined as SVG.
   Never use emoji as functional icons.
   ============================================================ */

const ICON_PATHS = {
  'message-square': 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z',
  'messages-square': 'M14 9a2 2 0 0 1-2 2H6l-4 4V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2z|M18 9h2a2 2 0 0 1 2 2v11l-4-4h-6a2 2 0 0 1-2-2v-1',
  users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2|M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z|M22 21v-2a4 4 0 0 0-3-3.87|M16 3.13a4 4 0 0 1 0 7.75',
  user: 'M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2|M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
  'user-plus': 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2|M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z|M19 8v6|M22 11h-6',
  'layout-dashboard': 'M3 3h7v9H3z|M14 3h7v5h-7z|M14 12h7v9h-7z|M3 16h7v5H3z',
  settings: 'M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z|M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  bell: 'M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9|M10.3 21a1.94 1.94 0 0 0 3.4 0',
  'bell-off': 'M8.7 3A6 6 0 0 1 18 8c0 2.3.4 4 .9 5.3|M17 17H3s3-2 3-9a6 6 0 0 1 .5-2.4|M10.3 21a1.94 1.94 0 0 0 3.4 0|M2 2l20 20',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z|M21 21l-4.35-4.35',
  x: 'M18 6 6 18|M6 6l12 12',
  menu: 'M3 6h18|M3 12h18|M3 18h18',
  check: 'M20 6 9 17l-5-5',
  'check-check': 'M18 6 7 17l-4-4|M22 10l-7.5 7.5L13 16',
  clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z|M12 6v6l4 2',
  'alert-circle': 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z|M12 8v4|M12 16h.01',
  'alert-triangle': 'M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z|M12 9v4|M12 17h.01',
  info: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z|M12 16v-4|M12 8h.01',
  'check-circle': 'M22 11.08V12a10 10 0 1 1-5.93-9.14|M22 4 12 14.01l-3-3',
  send: 'M22 2 11 13|M22 2l-7 20-4-9-9-4z',
  paperclip: 'M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48',
  image: 'M3 3h18v18H3z|M8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z|M21 15l-5-5L5 21',
  video: 'M23 7l-7 5 7 5V7z|M14 5H3a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2z',
  mic: 'M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z|M19 10v2a7 7 0 0 1-14 0v-2|M12 19v4|M8 23h8',
  'mic-off': 'M2 2l20 20|M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V5a3 3 0 0 0-5.94-.6|M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23|M12 19v4|M8 23h8',
  play: 'M5 3l14 9-14 9V3z',
  pause: 'M6 4h4v16H6z|M14 4h4v16h-4z',
  trash: 'M3 6h18|M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2|M10 11v6|M14 11v6',
  pencil: 'M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z',
  reply: 'M9 17l-6-6 6-6|M3 11h11a5 5 0 0 1 5 5v4',
  'chevron-left': 'M15 18l-6-6 6-6',
  'chevron-right': 'M9 18l6-6-6-6',
  'chevron-down': 'M6 9l6 6 6-6',
  'arrow-down': 'M12 5v14|M19 12l-7 7-7-7',
  'arrow-left': 'M19 12H5|M12 19l-7-7 7-7',
  'more-vertical': 'M12 13a1 1 0 1 0 0-2 1 1 0 0 0 0 2z|M12 6a1 1 0 1 0 0-2 1 1 0 0 0 0 2z|M12 20a1 1 0 1 0 0-2 1 1 0 0 0 0 2z',
  'log-out': 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4|M16 17l5-5-5-5|M21 12H9',
  'shield-check': 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z|M9 12l2 2 4-4',
  'shield-alert': 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z|M12 8v4|M12 16h.01',
  'wifi-off': 'M2 2l20 20|M8.5 16.5a5 5 0 0 1 7 0|M5 12.86a10 10 0 0 1 3.5-2.33|M2 8.82a15 15 0 0 1 4.17-2.65|M10.66 5c4.01-.36 8.14.9 11.34 3.76|M16.85 11.25a10 10 0 0 1 2.22 1.68|M12 20h.01',
  wifi: 'M5 12.55a11 11 0 0 1 14.08 0|M1.42 9a16 16 0 0 1 21.16 0|M8.53 16.11a6 6 0 0 1 6.95 0|M12 20h.01',
  'refresh-cw': 'M21 12a9 9 0 1 1-2.64-6.36L21 8|M21 3v5h-5',
  download: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4|M7 10l5 5 5-5|M12 15V3',
  'user-x': 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2|M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z|M17 8l5 5|M22 8l-5 5',
  'user-check': 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2|M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z|M16 11l2 2 4-4',
  key: 'M14.5 10.5a4.5 4.5 0 1 0-4.24 4.49L11 16l2 2-1 2 2 2 3-3V10.5z',
  'file-text': 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z|M14 2v6h6|M16 13H8|M16 17H8|M10 9H8',
  'plus': 'M12 5v14|M5 12h14',
  'log-in': 'M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4|M10 17l5-5-5-5|M15 12H3',
  circle: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z',
  'inbox': 'M22 12h-6l-2 3h-4l-2-3H2|M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z',
  'folder-open': 'M6 14l1.45-2.9A2 2 0 0 1 9.24 10H22l-2.55 6.9A2 2 0 0 1 17.56 18H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2',
  sun: 'M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z|M12 1v2|M12 21v2|M4.22 4.22l1.42 1.42|M18.36 18.36l1.42 1.42|M1 12h2|M21 12h2|M4.22 19.78l1.42-1.42|M18.36 5.64l1.42-1.42',
  moon: 'M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z',
  monitor: 'M20 3H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z|M8 21h8|M12 17v4',
  smartphone: 'M17 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2z|M12 18h.01',
  maximize: 'M8 3H5a2 2 0 0 0-2 2v3|M21 8V5a2 2 0 0 0-2-2h-3|M3 16v3a2 2 0 0 0 2 2h3|M16 21h3a2 2 0 0 0 2-2v-3',
  'panel-left': 'M3 3h18v18H3z|M9 3v18',
  'activity': 'M22 12h-4l-3 9L9 3l-3 9H2',
  'ban': 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z|M4.93 4.93l14.14 14.14',
  'volume-2': 'M11 5 6 9H2v6h4l5 4V5z|M19.07 4.93a10 10 0 0 1 0 14.14|M15.54 8.46a5 5 0 0 1 0 7.07',
};

/**
 * Build an inline SVG icon element.
 * @param {string} name key of ICON_PATHS
 * @param {object} [opts] { size, className, label } — label makes it non-decorative
 */
export function icon(name, opts = {}) {
  const { size, className = '', label = null } = opts;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', `icon ${className}`.trim());
  if (size) {
    svg.style.width = `${size}px`;
    svg.style.height = `${size}px`;
  }
  if (label) {
    svg.setAttribute('role', 'img');
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = label;
    svg.append(title);
  } else {
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
  }
  const d = ICON_PATHS[name];
  if (d) {
    for (const segment of d.split('|')) {
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', segment);
      svg.append(path);
    }
  }
  return svg;
}

/** Icon-only button; aria-label is mandatory. */
export function iconButton(name, label, { onClick, className = '', size, title } = {}) {
  if (!label) throw new Error('ui.iconButton: an aria-label is required');
  const btn = el('button', {
    type: 'button',
    class: `icon-btn ${className}`.trim(),
    'aria-label': label,
    title: title || label,
  });
  btn.append(icon(name, { size }));
  if (onClick) btn.addEventListener('click', onClick);
  return btn;
}

/* ============================================================
   Busy state
   ============================================================ */

export function setBusy(button, busy, busyLabel) {
  if (!button) return;
  if (busy) {
    button.dataset.busy = 'true';
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    if (busyLabel) {
      button.dataset.prevLabel = button.getAttribute('aria-label') || '';
      button.setAttribute('aria-label', busyLabel);
    }
  } else {
    delete button.dataset.busy;
    button.disabled = false;
    button.removeAttribute('aria-busy');
    if (button.dataset.prevLabel !== undefined) {
      if (button.dataset.prevLabel) button.setAttribute('aria-label', button.dataset.prevLabel);
      delete button.dataset.prevLabel;
    }
  }
}

/* ============================================================
   Live region announcements (screen readers)
   ============================================================ */

let politeRegion = null;
let assertiveRegion = null;

function ensureRegions() {
  if (!politeRegion) {
    politeRegion = el('div', { class: 'sr-only', 'aria-live': 'polite', 'aria-atomic': 'true', id: 'nx-live-polite' });
    document.body.append(politeRegion);
  }
  if (!assertiveRegion) {
    assertiveRegion = el('div', { class: 'sr-only', role: 'alert', 'aria-live': 'assertive', 'aria-atomic': 'true', id: 'nx-live-assertive' });
    document.body.append(assertiveRegion);
  }
}

/** @param {'polite'|'assertive'} priority */
export function announce(message, priority = 'polite') {
  if (!message) return;
  ensureRegions();
  const region = priority === 'assertive' ? assertiveRegion : politeRegion;
  region.textContent = '';
  // Force a DOM change so repeated identical messages are re-announced.
  window.requestAnimationFrame(() => {
    region.textContent = String(message);
  });
}

/* ============================================================
   Toasts
   ============================================================ */

let toastRegion = null;

function ensureToastRegion() {
  if (toastRegion?.isConnected) return toastRegion;
  // Top-of-viewport region (see components.css). It sits BELOW the
  // application header so a toast can never cover the hamburger, the theme
  // control or the profile control.
  toastRegion = el('div', { class: 'toast-region', id: 'nx-toasts', 'aria-live': 'polite', 'aria-atomic': 'false' });
  document.body.append(toastRegion);
  return toastRegion;
}

const TOAST_ICONS = {
  success: 'check-circle',
  error: 'alert-circle',
  warning: 'alert-triangle',
  info: 'info',
};

/**
 * @param {string} message
 * @param {object} [opts] { type, title, duration, action: { label, onClick } }
 */
export function toast(message, opts = {}) {
  const { type = 'info', title = null, duration = type === 'error' ? 7000 : 4200, action = null } = opts;
  const region = ensureToastRegion();

  const node = el('div', { class: 'toast', role: type === 'error' ? 'alert' : 'status', dataset: { type } });
  node.append(icon(TOAST_ICONS[type] || 'info', { size: 18 }));

  const body = el('div', { class: 'toast__body' });
  if (title) body.append(el('div', { class: 'toast__title', text: title }));
  body.append(el('div', { text: message }));
  if (action?.label) {
    const btn = el('button', { type: 'button', class: 'btn btn--sm btn--ghost', text: action.label, style: { marginTop: '8px' } });
    btn.addEventListener('click', () => {
      action.onClick?.();
      dismiss();
    });
    body.append(btn);
  }
  node.append(body);

  const close = iconButton('x', 'Dismiss notification', { className: 'icon-btn--sm toast__close' });
  node.append(close);

  let timer = null;
  const dismiss = () => {
    clearTimeout(timer);
    node.remove();
  };
  close.addEventListener('click', dismiss);
  node.addEventListener('mouseenter', () => clearTimeout(timer));
  node.addEventListener('mouseleave', () => {
    if (duration > 0) timer = setTimeout(dismiss, 1500);
  });

  region.append(node);
  if (duration > 0) timer = setTimeout(dismiss, duration);

  // Keep the stack bounded.
  while (region.children.length > 4) region.firstElementChild.remove();

  return dismiss;
}

export const toastSuccess = (m, o) => toast(m, { ...o, type: 'success' });
export const toastError = (m, o) => toast(m, { ...o, type: 'error' });
export const toastWarning = (m, o) => toast(m, { ...o, type: 'warning' });

/** Render an ApiError with a user-safe message. */
export function toastApiError(error, fallback = 'Something went wrong.') {
  if (!error) return;
  if (error.isAborted) return; // user-initiated cancellation is not an error
  const message = error.message || fallback;
  toast(message, { type: error.isOffline || error.isNetwork ? 'warning' : 'error' });
}

/* ============================================================
   Modal / dialog
   ============================================================ */

const modalStack = [];

/**
 * @param {object} config
 * @param {string} config.title
 * @param {Node|string} config.body
 * @param {Array<{label:string,variant?:string,value?:any,busyLabel?:string,onClick?:Function,closeOnClick?:boolean}>} [config.actions]
 * @param {string} [config.size] 'sm' | '' | 'lg'
 * @param {boolean} [config.dismissible]
 * @returns {{ close: Function, root: HTMLElement, body: HTMLElement, footer: HTMLElement }}
 */
export function openModal(config) {
  const { title, body, actions = [], size = '', dismissible = true, onClose } = config;
  const titleId = uid('modal-title');

  const backdrop = el('div', { class: 'modal-backdrop' });
  const modal = el('div', {
    class: `modal ${size ? `modal--${size}` : ''}`.trim(),
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': titleId,
  });

  const header = el('div', { class: 'modal__header' }, [el('h2', { class: 'modal__title', id: titleId, text: title })]);
  const bodyEl = el('div', { class: 'modal__body' });
  if (body instanceof Node) bodyEl.append(body);
  else if (body !== undefined && body !== null) bodyEl.append(el('p', { text: String(body) }));
  const footer = el('div', { class: 'modal__footer' });

  const previousFocus = document.activeElement;
  let released = null;

  const close = (result) => {
    if (!backdrop.isConnected) return;
    released?.();
    backdrop.remove();
    const index = modalStack.indexOf(close);
    if (index >= 0) modalStack.splice(index, 1);
    if (!modalStack.length) document.body.classList.remove('no-scroll');
    document.removeEventListener('keydown', onKeydown, true);
    if (previousFocus?.focus) previousFocus.focus();
    onClose?.(result);
  };

  function onKeydown(event) {
    if (event.key === 'Escape' && dismissible && modalStack[modalStack.length - 1] === close) {
      event.stopPropagation();
      close(null);
    }
  }

  if (dismissible) {
    const closeBtn = iconButton('x', 'Close dialog', { className: 'modal__close', onClick: () => close(null) });
    header.append(closeBtn);
    backdrop.addEventListener('mousedown', (event) => {
      if (event.target === backdrop) close(null);
    });
  }

  for (const action of actions) {
    const btn = el('button', {
      type: 'button',
      class: `btn ${action.variant ? `btn--${action.variant}` : ''}`.trim(),
      text: action.label,
    });
    btn.addEventListener('click', async () => {
      if (action.onClick) {
        setBusy(btn, true, action.busyLabel);
        try {
          const result = await action.onClick({ close, body: bodyEl, button: btn });
          if (result !== false && action.closeOnClick !== false) close(action.value ?? result ?? true);
        } finally {
          if (btn.isConnected) setBusy(btn, false);
        }
      } else {
        close(action.value ?? true);
      }
    });
    footer.append(btn);
  }

  modal.append(header, bodyEl);
  if (actions.length) modal.append(footer);
  backdrop.append(modal);
  document.body.append(backdrop);
  document.body.classList.add('no-scroll');
  modalStack.push(close);
  released = trapFocus(modal);
  document.addEventListener('keydown', onKeydown, true);

  // Focus the first meaningful control.
  window.requestAnimationFrame(() => {
    const target = modal.querySelector('input:not([type="hidden"]), textarea, select, .btn--primary, button');
    target?.focus();
  });

  return { close, root: modal, body: bodyEl, footer };
}

/**
 * Confirmation dialog. Always used before destructive actions.
 * @returns {Promise<boolean>}
 */
export function confirmDialog({
  title = 'Are you sure?',
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  danger = false,
  detail = null,
} = {}) {
  return new Promise((resolve) => {
    const body = el('div', { class: 'stack-sm' }, [
      message ? el('p', { text: message }) : null,
      detail ? el('p', { class: 'text-sm text-muted', text: detail }) : null,
    ]);
    let settled = false;
    openModal({
      title,
      body,
      size: 'sm',
      actions: [
        { label: cancelLabel, value: false },
        { label: confirmLabel, variant: danger ? 'danger' : 'primary', value: true },
      ],
      onClose: (result) => {
        if (settled) return;
        settled = true;
        resolve(result === true);
      },
    });
  });
}

/** Prompt dialog with a single validated text field. */
export function promptDialog({
  title,
  label,
  value = '',
  placeholder = '',
  confirmLabel = 'Save',
  maxLength = 200,
  multiline = false,
  validate = null,
} = {}) {
  return new Promise((resolve) => {
    const inputId = uid('prompt');
    const errorEl = el('div', { class: 'field__error', role: 'alert' });
    const input = el(multiline ? 'textarea' : 'input', {
      class: multiline ? 'textarea' : 'input',
      id: inputId,
      value,
      placeholder,
      maxLength,
    });
    const body = el('div', { class: 'field' }, [
      el('label', { class: 'field__label', for: inputId, text: label }),
      input,
      errorEl,
    ]);
    let settled = false;
    const { close } = openModal({
      title,
      body,
      size: 'sm',
      actions: [
        { label: 'Cancel', value: null },
        {
          label: confirmLabel,
          variant: 'primary',
          closeOnClick: false,
          onClick: () => {
            const v = input.value.trim();
            const problem = validate ? validate(v) : null;
            if (problem) {
              errorEl.textContent = problem;
              input.setAttribute('aria-invalid', 'true');
              input.focus();
              return false;
            }
            settled = true;
            close(v);
            resolve(v);
            return false;
          },
        },
      ],
      onClose: () => {
        if (settled) return;
        settled = true;
        resolve(null);
      },
    });
  });
}

/* ============================================================
   Lightbox — fullscreen image/video preview
   ============================================================ */

/**
 * @param {object} item { kind:'image'|'video', url, title, downloadUrl, poster }
 */
export function openLightbox(item) {
  const { kind = 'image', url, title = '', downloadUrl = null, poster = null } = item;
  if (!url) return null;

  const root = el('div', { class: 'lightbox', role: 'dialog', 'aria-modal': 'true', 'aria-label': title || 'Media preview' });
  const bar = el('div', { class: 'lightbox__bar' });
  const closeBtn = iconButton('x', 'Close preview');
  bar.append(closeBtn, el('div', { class: 'lightbox__title truncate', text: title }));

  if (downloadUrl) {
    const dl = el('a', {
      class: 'icon-btn',
      href: downloadUrl,
      download: '',
      rel: 'noopener',
      'aria-label': 'Download media',
      style: { marginLeft: 'auto', color: '#fff' },
    });
    dl.append(icon('download'));
    bar.append(dl);
  }

  const stage = el('div', { class: 'lightbox__stage' });
  if (kind === 'video') {
    const video = el('video', { src: url, controls: true, playsInline: true, preload: 'metadata' });
    if (poster) video.poster = poster;
    stage.append(video);
  } else {
    const img = el('img', { src: url, alt: title || 'Image attachment' });
    img.addEventListener('error', () => {
      clear(stage);
      stage.append(el('p', { class: 'media-frame__error', style: { color: '#fff' }, text: 'This media is no longer available or you are not authorized to view it.' }));
    });
    stage.append(img);
  }

  root.append(bar, stage);

  const previousFocus = document.activeElement;
  const release = trapFocus(root);
  const close = () => {
    release();
    root.remove();
    document.removeEventListener('keydown', onKey, true);
    document.body.classList.remove('no-scroll');
    previousFocus?.focus?.();
  };
  function onKey(event) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close();
    }
  }
  closeBtn.addEventListener('click', close);
  document.addEventListener('keydown', onKey, true);
  document.body.append(root);
  document.body.classList.add('no-scroll');
  closeBtn.focus();
  return close;
}

/* ============================================================
   Placeholders
   ============================================================ */

export function skeletonList(count = 6, { avatar = true } = {}) {
  const frag = document.createDocumentFragment();
  for (let i = 0; i < count; i += 1) {
    const row = el('div', { class: 'skeleton-row', 'aria-hidden': 'true' });
    if (avatar) row.append(el('div', { class: 'skeleton skeleton--avatar' }));
    row.append(
      el('div', { class: 'skeleton-row__body' }, [
        el('div', { class: 'skeleton skeleton--text', style: { width: `${45 + ((i * 13) % 35)}%` } }),
        el('div', { class: 'skeleton skeleton--line', style: { width: `${60 + ((i * 7) % 30)}%` } }),
      ])
    );
    frag.append(row);
  }
  return frag;
}

/**
 * @param {object} config { icon, title, text, action: { label, onClick } }
 */
export function emptyState({ icon: iconName = 'inbox', title, text, action = null } = {}) {
  const node = el('div', { class: 'empty-state' });
  node.append(icon(iconName, { className: 'empty-state__icon' }));
  if (title) node.append(el('div', { class: 'empty-state__title', text: title }));
  if (text) node.append(el('p', { class: 'empty-state__text', text }));
  if (action?.label) {
    const btn = el('button', { type: 'button', class: 'btn btn--primary btn--sm', text: action.label });
    btn.addEventListener('click', action.onClick);
    node.append(btn);
  }
  return node;
}

export function errorState({ title = 'Unable to load', text, onRetry } = {}) {
  const node = el('div', { class: 'empty-state' });
  node.append(icon('alert-circle', { className: 'empty-state__icon' }));
  node.append(el('div', { class: 'empty-state__title', text: title }));
  if (text) node.append(el('p', { class: 'empty-state__text', text }));
  if (onRetry) {
    const btn = el('button', { type: 'button', class: 'btn btn--sm', text: 'Try again' });
    btn.addEventListener('click', onRetry);
    node.append(btn);
  }
  return node;
}

export function loadingRow(label = 'Loading…') {
  return el('div', { class: 'loading-row', role: 'status' }, [el('span', { class: 'spinner' }), el('span', { text: label })]);
}

/* ============================================================
   Popover menu anchored to a trigger
   ============================================================ */

let activeMenu = null;

export function closeMenu() {
  activeMenu?.remove();
  activeMenu = null;
}

/**
 * @param {HTMLElement} anchor
 * @param {Array<{label:string,icon?:string,danger?:boolean,onClick?:Function,separator?:boolean,disabled?:boolean}>} items
 */
export function openMenu(anchor, items) {
  closeMenu();
  const menu = el('div', { class: 'menu', role: 'menu' });

  for (const item of items) {
    if (!item) continue;
    if (item.separator) {
      menu.append(el('div', { class: 'menu__sep', role: 'separator' }));
      continue;
    }
    const btn = el('button', {
      type: 'button',
      class: `menu__item ${item.danger ? 'menu__item--danger' : ''}`.trim(),
      role: 'menuitem',
      disabled: !!item.disabled,
    });
    if (item.icon) btn.append(icon(item.icon, { size: 16 }));
    btn.append(el('span', { text: item.label }));
    btn.addEventListener('click', () => {
      closeMenu();
      item.onClick?.();
    });
    menu.append(btn);
  }

  document.body.append(menu);
  activeMenu = menu;

  const rect = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth;
  const mh = menu.offsetHeight;
  let left = Math.min(rect.left, window.innerWidth - mw - 8);
  left = Math.max(8, left);
  let top = rect.bottom + 4;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, rect.top - mh - 4);
  menu.style.left = `${left + window.scrollX}px`;
  menu.style.top = `${top + window.scrollY}px`;

  const dismiss = (event) => {
    if (menu.contains(event.target) || anchor.contains(event.target)) return;
    closeMenu();
    document.removeEventListener('mousedown', dismiss, true);
  };
  setTimeout(() => document.addEventListener('mousedown', dismiss, true), 0);
  menu.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeMenu();
      anchor.focus();
    }
  });
  menu.querySelector('button')?.focus();
  return closeMenu;
}

/* ============================================================
   Avatar element (image with graceful fallback to initials)
   ============================================================ */

export function avatar(name, url, { size = '', presence = null, alt, square = false } = {}) {
  const node = el('div', {
    class: ['avatar', size ? `avatar--${size}` : '', square ? 'avatar--square' : ''].filter(Boolean).join(' '),
  });
  const fallback = el('span', { text: initialsOf(name), 'aria-hidden': 'true' });
  node.append(fallback);
  if (url) {
    const img = el('img', { src: url, alt: alt || '', loading: 'lazy', decoding: 'async' });
    img.addEventListener('error', () => img.remove(), { once: true });
    img.addEventListener('load', () => fallback.remove(), { once: true });
    node.append(img);
  }
  if (presence) {
    node.append(el('span', { class: 'presence-dot', dataset: { presence }, title: presence === 'online' ? 'Online' : 'Offline' }));
  }
  return node;
}

function initialsOf(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  return parts.slice(0, 2).map((p) => [...p][0].toUpperCase()).join('');
}

/* ============================================================
   Inline form error helpers
   ============================================================ */

export function setFieldError(input, errorEl, message) {
  if (message) {
    input?.setAttribute('aria-invalid', 'true');
    if (errorEl) errorEl.textContent = message;
  } else {
    input?.removeAttribute('aria-invalid');
    if (errorEl) errorEl.textContent = '';
  }
}

export function showAlert(node, message, type = 'error') {
  if (!node) return;
  node.className = `alert alert--${type}`;
  clear(node);
  node.append(icon(TOAST_ICONS[type] || 'info', { size: 16 }), el('span', { text: message }));
  node.hidden = false;
}

export function hideAlert(node) {
  if (!node) return;
  node.hidden = true;
  clear(node);
}
