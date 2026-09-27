/**
 * NEXORA — members.js
 * Administrator member management: search, filter, pagination, creation,
 * activation state, credential reset and member detail.
 *
 * Every control rendered here is also authorized server-side. A member who
 * reaches this page receives 403 from the API for each request.
 */

import { ApiError, api, normalizePage } from './api.js';
import {
  avatar,
  confirmDialog,
  emptyState,
  errorState,
  icon,
  iconButton,
  loadingRow,
  openMenu,
  openModal,
  setBusy,
  setFieldError,
  showAlert,
  skeletonList,
  toast,
  toastApiError,
} from './ui.js';
import { clear, debounce, el, formatDate, formatRelative, uid } from './utils.js';
import { getPresence, seedPresence } from './presence.js';

const PAGE_SIZE = 25;

export class MembersController {
  /** @param {{listEl:HTMLElement, searchInput:HTMLInputElement, statusFilter:HTMLSelectElement, paginationEl:HTMLElement, createBtn:HTMLElement, countEl?:HTMLElement}} refs */
  constructor(refs) {
    this.refs = refs;
    this.items = [];
    this.next = null;
    this.previous = null;
    this.count = 0;
    this.page = 1;
    this.query = '';
    this.status = 'all';
    this.loading = false;
    this.abort = null;
  }

  async init() {
    this.bind();
    await this.load({ reset: true });

    const requested = new URLSearchParams(window.location.search).get('m');
    if (requested) this.openDetail(requested);
  }

  bind() {
    const { searchInput, statusFilter, createBtn } = this.refs;

    if (searchInput) {
      const run = debounce((value) => {
        this.query = value.trim();
        this.page = 1;
        this.load({ reset: true });
      }, 320);
      searchInput.addEventListener('input', (event) => run(event.target.value));
      searchInput.addEventListener('search', (event) => run(event.target.value));
    }

    statusFilter?.addEventListener('change', (event) => {
      this.status = event.target.value;
      this.page = 1;
      this.load({ reset: true });
    });

    createBtn?.addEventListener('click', () => this.openCreateDialog());
  }

  async load({ reset = false, cursor = null } = {}) {
    const { listEl } = this.refs;
    if (this.loading) return;
    this.loading = true;
    this.abort?.abort();
    this.abort = new AbortController();

    if (reset) {
      clear(listEl);
      listEl.append(skeletonList(6));
    }

    try {
      const params = { limit: PAGE_SIZE };
      if (this.query) params.search = this.query;
      if (this.status !== 'all') params.is_active = this.status === 'active';
      if (cursor) params.cursor = cursor;
      else if (!reset && this.page > 1) params.offset = (this.page - 1) * PAGE_SIZE;

      const response = await api.members.list(params, { signal: this.abort.signal });
      const page = normalizePage(response.data ?? response);
      this.items = page.items;
      this.next = page.next;
      this.previous = page.previous;
      this.count = page.count;
      seedPresence(this.items);
      this.render();
    } catch (error) {
      if (error instanceof ApiError && error.isAborted) return;
      clear(listEl);
      listEl.append(errorState({ title: 'Unable to load members', text: error.message, onRetry: () => this.load({ reset: true }) }));
    } finally {
      this.loading = false;
    }
  }

  render() {
    const { listEl, paginationEl, countEl } = this.refs;
    clear(listEl);

    if (countEl) {
      countEl.textContent = this.count ? `${this.count} member${this.count === 1 ? '' : 's'}` : '';
    }

    if (!this.items.length) {
      listEl.append(
        this.query || this.status !== 'all'
          ? emptyState({ icon: 'search', title: 'No members found', text: 'Try a different search term or filter.' })
          : emptyState({
              icon: 'users',
              title: 'No members yet',
              text: 'Members are created by administrators. Create the first member to begin.',
              action: { label: 'Create member', onClick: () => this.openCreateDialog() },
            })
      );
      clear(paginationEl);
      return;
    }

    const wrap = el('div', { class: 'table-wrap table-wrap--stack' });
    const table = el('table', { class: 'table' });
    table.append(
      el('thead', {}, [
        el('tr', {}, [
          el('th', { scope: 'col', text: 'Member' }),
          el('th', { scope: 'col', text: 'Phone' }),
          el('th', { scope: 'col', text: 'Status' }),
          el('th', { scope: 'col', text: 'Last seen' }),
          el('th', { scope: 'col', class: 'sr-only', text: 'Actions' }),
        ]),
      ])
    );

    const tbody = el('tbody');
    for (const member of this.items) tbody.append(this.renderRow(member));
    table.append(tbody);
    wrap.append(table);
    listEl.append(wrap);

    this.renderPagination();
  }

  renderRow(member) {
    const active = member.is_active !== false;
    const presence = getPresence(member.id);

    const nameCell = el('td', { dataset: { label: 'Member' } }, [
      el('div', { class: 'row' }, [
        avatar(member.display_name || member.name, member.avatar_url, {
          size: 'sm',
          presence: presence.status === 'online' ? 'online' : null,
        }),
        el('div', { class: 'truncate' }, [
          el('div', { class: 'fw-medium truncate', text: member.display_name || member.name || 'Member' }),
          member.is_admin ? el('div', { class: 'text-xs text-muted', text: 'Administrator' }) : null,
        ]),
      ]),
    ]);

    const statusTag = el('span', {
      class: `tag tag--dot ${active ? 'tag--success' : 'tag--danger'}`,
      text: active ? 'Active' : 'Inactive',
    });
    const statusCell = el('td', { dataset: { label: 'Status' } }, [
      el('div', { class: 'row row-wrap', style: { gap: '6px' } }, [
        statusTag,
        member.must_change_pin ? el('span', { class: 'tag tag--warning', text: 'PIN reset pending' }) : null,
      ]),
    ]);

    const actionsCell = el('td', { dataset: { label: 'Actions' } });
    const actions = el('div', { class: 'table__actions' });

    const viewBtn = iconButton('user', `View ${member.display_name || 'member'}`, {
      className: 'icon-btn--sm',
      onClick: () => this.openDetail(member.id),
    });
    const chatBtn = iconButton('message-square', `Open conversation with ${member.display_name || 'member'}`, {
      className: 'icon-btn--sm',
      onClick: () => this.openConversation(member),
    });
    const moreBtn = iconButton('more-vertical', `More actions for ${member.display_name || 'member'}`, { className: 'icon-btn--sm' });
    moreBtn.addEventListener('click', () =>
      openMenu(moreBtn, [
        { label: 'Edit member', icon: 'pencil', onClick: () => this.openEditDialog(member) },
        { label: 'Reset PIN', icon: 'key', onClick: () => this.resetCredential(member) },
        { label: 'View activity', icon: 'activity', onClick: () => this.openActivity(member) },
        { separator: true },
        {
          label: active ? 'Deactivate' : 'Activate',
          icon: active ? 'user-x' : 'user-check',
          danger: active,
          onClick: () => this.toggleActive(member),
        },
      ])
    );
    actions.append(viewBtn, chatBtn, moreBtn);
    actionsCell.append(actions);

    return el('tr', { dataset: { id: String(member.id) } }, [
      nameCell,
      el('td', { dataset: { label: 'Phone' }, text: member.phone || '—' }),
      statusCell,
      el('td', { dataset: { label: 'Last seen' }, text: member.last_seen ? formatRelative(member.last_seen) : 'Never' }),
      actionsCell,
    ]);
  }

  renderPagination() {
    const { paginationEl } = this.refs;
    if (!paginationEl) return;
    clear(paginationEl);
    if (!this.next && !this.previous) return;

    const info = el('div', { class: 'pagination__info', text: this.count ? `${this.count} total` : '' });
    const controls = el('div', { class: 'pagination__controls' });

    const prev = el('button', { type: 'button', class: 'btn btn--sm', text: 'Previous', disabled: !this.previous });
    prev.addEventListener('click', () => {
      this.page = Math.max(1, this.page - 1);
      this.load({ cursor: typeof this.previous === 'string' && !/^https?:/.test(this.previous) ? this.previous : null });
    });

    const next = el('button', { type: 'button', class: 'btn btn--sm', text: 'Next', disabled: !this.next });
    next.addEventListener('click', () => {
      this.page += 1;
      this.load({ cursor: typeof this.next === 'string' && !/^https?:/.test(this.next) ? this.next : null });
    });

    controls.append(prev, next);
    paginationEl.append(info, controls);
  }

  /* ============================================================
     Actions
     ============================================================ */

  async openConversation(member) {
    try {
      const conv = await api.members.conversation(member.id);
      const id = conv?.id ?? conv?.conversation_id;
      if (id) window.location.href = `chat.html?c=${encodeURIComponent(id)}`;
      else toast('No conversation is available for this member yet.', { type: 'info' });
    } catch (error) {
      toastApiError(error);
    }
  }

  async toggleActive(member) {
    const active = member.is_active !== false;
    const ok = await confirmDialog({
      title: active ? 'Deactivate member?' : 'Activate member?',
      message: active
        ? `${member.display_name || 'This member'} will no longer be able to sign in. Existing conversation history is retained.`
        : `${member.display_name || 'This member'} will be able to sign in again.`,
      confirmLabel: active ? 'Deactivate' : 'Activate',
      danger: active,
    });
    if (!ok) return;
    try {
      await api.members.setActive(member.id, !active);
      toast(active ? 'Member deactivated.' : 'Member activated.', { type: 'success' });
      this.load({ reset: true });
    } catch (error) {
      toastApiError(error);
    }
  }

  async resetCredential(member) {
    const ok = await confirmDialog({
      title: 'Reset this member’s PIN?',
      message: 'A new initial PIN will be issued by the backend. The member must change it at next sign-in.',
      detail: 'The PIN is generated and delivered by the backend according to your deployment policy. It is never displayed here in plain text unless the backend explicitly returns it for one-time handover.',
      confirmLabel: 'Reset PIN',
      danger: true,
    });
    if (!ok) return;
    try {
      const result = await api.members.resetCredential(member.id);
      // Some deployments return a one-time PIN for offline handover.
      if (result?.initial_pin) {
        openModal({
          title: 'Initial PIN issued',
          size: 'sm',
          body: el('div', { class: 'stack-sm' }, [
            el('p', { text: 'Share this one-time PIN with the member through your approved channel. It will not be shown again.' }),
            el('div', {
              class: 'card__body text-center',
              style: { fontSize: '1.6rem', fontWeight: '600', letterSpacing: '0.2em', fontFamily: 'var(--font-mono)' },
              text: String(result.initial_pin),
            }),
          ]),
          actions: [{ label: 'Done', variant: 'primary' }],
        });
      } else {
        toast('PIN reset. The member must set a new PIN at next sign-in.', { type: 'success' });
      }
      this.load({ reset: true });
    } catch (error) {
      toastApiError(error);
    }
  }

  /* ============================================================
     Dialogs
     ============================================================ */

  openCreateDialog() {
    const form = buildMemberForm();
    const { close } = openModal({
      title: 'Create member',
      body: form.root,
      actions: [
        { label: 'Cancel', value: null },
        {
          label: 'Create member',
          variant: 'primary',
          closeOnClick: false,
          busyLabel: 'Creating member…',
          onClick: async ({ button }) => {
            const payload = form.collect();
            if (!payload) return false;
            setBusy(button, true);
            try {
              const member = await api.members.create(payload);
              toast('Member created.', { type: 'success' });
              close(true);
              // Reconcile locally: the backend just returned the created
              // member, so a full list reload is unnecessary when the current
              // view would show it. Backend stays authoritative for anything
              // the local view cannot represent (filters, ordering position).
              this.applyCreatedMember(member);
            } catch (error) {
              form.applyErrors(error);
              return false;
            } finally {
              setBusy(button, false);
            }
            return false;
          },
        },
      ],
    });
  }

  /** Insert a freshly created member without a network round-trip. */
  applyCreatedMember(member) {
    if (!member || !member.id) {
      this.load({ reset: true });
      return;
    }
    const matchesView =
      !this.query &&
      (this.status === 'all' || this.status === (member.is_active !== false ? 'active' : 'inactive'));
    this.count += 1;
    if (matchesView) {
      // Keep the backend's ordering (full_name) locally: insert, sort, render.
      this.items = [...this.items, member].sort((a, b) =>
        String(a.display_name || a.full_name || '').localeCompare(String(b.display_name || b.full_name || ''))
      );
      this.render();
    } else {
      // The current filter would hide the new row; re-fetch quietly so the
      // counts and pagination stay truthful.
      this.load({ reset: true });
    }
  }

  openEditDialog(member) {
    const form = buildMemberForm(member);
    const { close } = openModal({
      title: 'Edit member',
      body: form.root,
      actions: [
        { label: 'Cancel', value: null },
        {
          label: 'Save changes',
          variant: 'primary',
          closeOnClick: false,
          busyLabel: 'Saving…',
          onClick: async ({ button }) => {
            const payload = form.collect();
            if (!payload) return false;
            setBusy(button, true);
            try {
              const updated = await api.members.update(member.id, payload);
              toast('Member updated.', { type: 'success' });
              close(true);
              // Patch the row in place — no unrelated reloads, no full refetch.
              const merged = { ...member, ...payload, ...(updated || {}) };
              const index = this.items.findIndex((m) => String(m.id) === String(member.id));
              if (index >= 0) {
                this.items[index] = merged;
                this.render();
              } else {
                this.load({ reset: true });
              }
            } catch (error) {
              form.applyErrors(error);
              return false;
            } finally {
              setBusy(button, false);
            }
            return false;
          },
        },
      ],
    });
  }

  async openDetail(memberId) {
    const body = el('div', {}, [loadingRow('Loading member…')]);
    const { close } = openModal({ title: 'Member', body, size: 'lg' });

    try {
      const member = await api.members.get(memberId);
      clear(body);
      const presence = getPresence(member.id);

      body.append(
        el('div', { class: 'stack' }, [
          el('div', { class: 'row' }, [
            avatar(member.display_name, member.avatar_url, { size: 'lg' }),
            el('div', { class: 'truncate' }, [
              el('h3', { class: 'truncate', text: member.display_name || 'Member' }),
              el('div', { class: 'text-sm text-muted', text: member.phone || '' }),
            ]),
          ]),
          el('dl', { class: 'dl' }, [
            el('dt', { text: 'Status' }),
            el('dd', {}, [
              el('span', {
                class: `tag tag--dot ${member.is_active !== false ? 'tag--success' : 'tag--danger'}`,
                text: member.is_active !== false ? 'Active' : 'Inactive',
              }),
            ]),
            el('dt', { text: 'Role' }),
            el('dd', { text: member.is_admin ? 'Administrator' : 'Member' }),
            el('dt', { text: 'Login identifier' }),
            el('dd', { text: member.login_identifier || member.phone || '—' }),
            el('dt', { text: 'Created' }),
            el('dd', { text: member.date_joined ? formatDate(member.date_joined) : '—' }),
            el('dt', { text: 'Last seen' }),
            el('dd', { text: presence.status === 'online' ? 'Online now' : member.last_seen ? formatRelative(member.last_seen) : 'Never' }),
            el('dt', { text: 'Groups' }),
            el('dd', { text: Array.isArray(member.groups) ? String(member.groups.length) : '—' }),
          ]),
          el('div', { class: 'row row-wrap' }, [
            buttonLink('Open conversation', 'message-square', () => {
              close();
              this.openConversation(member);
            }),
            buttonLink('Edit', 'pencil', () => {
              close();
              this.openEditDialog(member);
            }),
            buttonLink('Activity', 'activity', () => {
              close();
              this.openActivity(member);
            }),
          ]),
        ])
      );
    } catch (error) {
      clear(body);
      body.append(errorState({ text: error.message }));
    }
  }

  async openActivity(member) {
    const body = el('div', {}, [loadingRow('Loading activity…')]);
    openModal({ title: `Activity — ${member.display_name || 'Member'}`, body, size: 'lg' });
    try {
      const response = await api.members.activity(member.id, { limit: 40 });
      const page = normalizePage(response.data ?? response);
      clear(body);
      if (!page.items.length) {
        body.append(emptyState({ icon: 'activity', title: 'No recorded activity', text: 'Activity will appear here as the member uses the platform.' }));
        return;
      }
      const list = el('div', { class: 'activity-list' });
      for (const entry of page.items) {
        list.append(
          el('div', { class: 'activity-item' }, [
            el('div', { class: 'activity-item__icon', dataset: { severity: entry.severity || '' } }, [icon(activityIcon(entry.kind), { size: 15 })]),
            el('div', { class: 'activity-item__body' }, [
              el('div', { class: 'activity-item__title', text: entry.description || entry.action || 'Activity' }),
              el('div', { class: 'activity-item__meta', text: formatRelative(entry.created_at) }),
            ]),
          ])
        );
      }
      body.append(list);
    } catch (error) {
      clear(body);
      body.append(errorState({ text: error.message }));
    }
  }
}

/* ============================================================
   Member form

   API contract (backend MemberCreateSerializer / MemberUpdateSerializer):
     display_name  -> model full_name   (required on create)
     phone         -> normalized E.164  (required on create)
     email         -> optional
     is_active     -> create only; updates use activate/deactivate endpoints
   `login_identifier` is NOT part of the contract: sign-in is by phone and the
   backend has no such field, so the form never sends it.
   ============================================================ */

/**
 * Pure payload builder — the DOM form delegates to this so the contract is
 * unit-testable without a browser.
 * @param {{displayName?:string, phone?:string, email?:string, isActive?:boolean}} input
 * @param {{isEdit?:boolean}} [options]
 * @returns {{payload:object|null, problems:{displayName?:string, phone?:string, email?:string}}}
 */
export function collectMemberPayload(input = {}, { isEdit = false } = {}) {
  const displayName = String(input.displayName || '').trim();
  const phone = String(input.phone || '').trim();
  const email = String(input.email || '').trim();

  const problems = {};
  if (!displayName) problems.displayName = 'Enter a display name.';
  if (!phone) problems.phone = 'Enter a phone number.';
  else if (!/^[+0-9 ()-]{6,32}$/.test(phone)) problems.phone = 'Enter a valid phone number.';
  if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) problems.email = 'Enter a valid email address.';

  if (Object.keys(problems).length) return { payload: null, problems };

  const payload = { display_name: displayName, phone };
  if (email) payload.email = email;
  if (!isEdit) payload.is_active = input.isActive !== false;
  return { payload, problems };
}

/**
 * Pure backend-error mapper. Accepts anything ApiError-shaped and returns the
 * message for each form slot: `displayName` (display_name/full_name), `phone`,
 * `email`, plus `form` for everything that must be shown at form level
 * (non-field errors, is_active, 403/409/500/network/timeout messages).
 * @param {{errors?:object, message?:string}} error
 */
export function mapMemberErrors(error) {
  const fields = (error && error.errors) || {};
  const first = (keys) => {
    for (const key of keys) {
      const value = fields[key];
      if (value) return [].concat(value)[0];
    }
    return null;
  };
  const displayName = first(['display_name', 'full_name']);
  const phone = first(['phone']);
  const email = first(['email']);
  const form = first(['__all__', 'non_field_errors', 'is_active', 'detail']) || (error && error.message) || null;
  return { displayName, phone, email, form };
}

function buildMemberForm(member = null) {
  const isEdit = !!member;
  const nameId = uid('f');
  const phoneId = uid('f');
  const emailId = uid('f');

  const nameInput = el('input', { class: 'input', id: nameId, value: member?.display_name || member?.full_name || '', maxLength: 120, autocomplete: 'off' });
  const nameError = el('div', { class: 'field__error' });

  const phoneInput = el('input', {
    class: 'input',
    id: phoneId,
    type: 'tel',
    inputMode: 'tel',
    value: member?.phone || '',
    maxLength: 32,
    autocomplete: 'off',
    placeholder: '+234 801 234 5678',
  });
  const phoneError = el('div', { class: 'field__error' });

  const emailInput = el('input', {
    class: 'input',
    id: emailId,
    type: 'email',
    inputMode: 'email',
    value: member?.email || '',
    maxLength: 254,
    autocomplete: 'off',
  });
  const emailError = el('div', { class: 'field__error' });

  const activeToggle = el('input', { type: 'checkbox', checked: member ? member.is_active !== false : true });

  /** Form-level error strip for non-field failures (403/409/500/network…). */
  const formAlert = el('div', { class: 'alert alert--error', role: 'alert', hidden: true });

  const root = el('div', { class: 'stack' }, [
    formAlert,
    el('div', { class: 'field' }, [
      el('label', { class: 'field__label', for: nameId, text: 'Display name' }),
      nameInput,
      nameError,
    ]),
    el('div', { class: 'field' }, [
      el('label', { class: 'field__label', for: phoneId, text: 'Phone number' }),
      phoneInput,
      el('div', { class: 'field__hint', text: 'International format, e.g. +2348012345678. This is the member’s sign-in identifier.' }),
      phoneError,
    ]),
    el('div', { class: 'field' }, [
      el('label', { class: 'field__label', for: emailId, text: 'Email (optional)' }),
      emailInput,
      emailError,
    ]),
    isEdit
      ? el('div', { class: 'alert alert--info' }, [
          icon('info', { size: 16 }),
          el('span', {
            text: 'Activation is managed with the Activate/Deactivate action in the member menu.',
          }),
        ])
      : el('label', { class: 'check' }, [activeToggle, el('span', { class: 'check__text', text: 'Account is active' })]),
    el('div', { class: 'alert alert--info' }, [
      icon('info', { size: 16 }),
      el('span', {
        text: 'The initial six-digit PIN is generated by the backend. The member is required to set their own PIN at first sign-in.',
      }),
    ]),
  ]);

  return {
    root,
    collect() {
      formAlert.hidden = true;
      setFieldError(nameInput, nameError, '');
      setFieldError(phoneInput, phoneError, '');
      setFieldError(emailInput, emailError, '');

      const { payload, problems } = collectMemberPayload(
        {
          displayName: nameInput.value,
          phone: phoneInput.value,
          email: emailInput.value,
          isActive: activeToggle.checked,
        },
        { isEdit }
      );

      if (!payload) {
        if (problems.displayName) setFieldError(nameInput, nameError, problems.displayName);
        if (problems.phone) setFieldError(phoneInput, phoneError, problems.phone);
        if (problems.email) setFieldError(emailInput, emailError, problems.email);
        (problems.displayName ? nameInput : problems.phone ? phoneInput : emailInput).focus();
        return null;
      }
      return payload;
    },
    /**
     * Map an ApiError onto the form. Field errors land next to their input;
     * everything else (non-field, authorization, conflict, server, network,
     * timeout) lands in the always-visible form alert so a failure is never
     * silent. Returns true when a field error was pinned.
     */
    applyErrors(error) {
      if (!(error instanceof ApiError)) {
        showAlert(formAlert, error?.message || 'The member could not be saved. Please try again.');
        return;
      }
      const mapped = mapMemberErrors(error);

      if (mapped.displayName) setFieldError(nameInput, nameError, mapped.displayName);
      if (mapped.phone) setFieldError(phoneInput, phoneError, mapped.phone);
      if (mapped.email) setFieldError(emailInput, emailError, mapped.email);
      showAlert(formAlert, mapped.form || 'The member could not be saved. Please try again.');

      // Focus the first invalid field so retrying is one keystroke away.
      if (mapped.displayName) nameInput.focus();
      else if (mapped.phone) phoneInput.focus();
      else if (mapped.email) emailInput.focus();
      return Boolean(mapped.displayName || mapped.phone || mapped.email);
    },
  };
}

function buttonLink(label, iconName, onClick) {
  const btn = el('button', { type: 'button', class: 'btn btn--sm' });
  btn.append(icon(iconName, { size: 15 }), el('span', { text: label }));
  btn.addEventListener('click', onClick);
  return btn;
}

function activityIcon(kind) {
  const k = String(kind || '').toLowerCase();
  if (k.includes('login')) return 'log-in';
  if (k.includes('message')) return 'message-square';
  if (k.includes('group')) return 'messages-square';
  if (k.includes('security') || k.includes('pin')) return 'shield-alert';
  return 'activity';
}

export default MembersController;
