/**
 * NEXORA — groups.js
 * Group directory, creation with member selection, and group information.
 *
 * Admins see management controls; members see only what the backend permits.
 * Inactive / ineligible members are shown but are not selectable.
 */

import { ApiError, api, normalizePage, resolveMediaUrl } from './api.js';
import { isAdmin } from './auth.js';
import { getFeatures } from './theme.js';
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
  skeletonList,
  toast,
  toastApiError,
} from './ui.js';
import { clear, debounce, el, formatRelative, uid } from './utils.js';

export class GroupsController {
  /** @param {{listEl:HTMLElement, searchInput?:HTMLInputElement, createBtn?:HTMLElement, countEl?:HTMLElement}} refs */
  constructor(refs) {
    this.refs = refs;
    this.items = [];
    this.next = null;
    this.query = '';
    this.includeArchived = false;
    this.loading = false;
    this.abort = null;
  }

  async init() {
    this.bind();
    await this.load({ reset: true });

    const requested = new URLSearchParams(window.location.search).get('g');
    if (requested) this.openGroup(requested);
  }

  bind() {
    const { searchInput, createBtn, archivedToggle } = this.refs;
    if (searchInput) {
      const run = debounce((value) => {
        this.query = value.trim();
        this.load({ reset: true });
      }, 300);
      searchInput.addEventListener('input', (event) => run(event.target.value));
    }
    archivedToggle?.addEventListener('change', (event) => {
      this.includeArchived = event.target.checked;
      this.load({ reset: true });
    });
    createBtn?.addEventListener('click', () => this.openCreateDialog());
  }

  async load({ reset = false } = {}) {
    const { listEl } = this.refs;
    if (this.loading) return;
    this.loading = true;
    this.abort?.abort();
    this.abort = new AbortController();

    if (reset) {
      clear(listEl);
      listEl.append(skeletonList(4));
    }

    try {
      const params = { limit: 40 };
      if (this.query) params.search = this.query;
      if (this.includeArchived) params.include_archived = true;
      const response = await api.groups.list(params, { signal: this.abort.signal });
      const page = normalizePage(response.data ?? response);
      this.items = page.items;
      this.next = page.next;
      this.render();
    } catch (error) {
      if (error instanceof ApiError && error.isAborted) return;
      clear(listEl);
      listEl.append(errorState({ title: 'Unable to load groups', text: error.message, onRetry: () => this.load({ reset: true }) }));
    } finally {
      this.loading = false;
    }
  }

  render() {
    const { listEl, countEl } = this.refs;
    clear(listEl);

    if (countEl) countEl.textContent = this.items.length ? `${this.items.length} group${this.items.length === 1 ? '' : 's'}` : '';

    if (!this.items.length) {
      listEl.append(
        this.query
          ? emptyState({ icon: 'search', title: 'No groups found', text: 'Try a different search term.' })
          : emptyState({
              icon: 'messages-square',
              title: 'No groups yet',
              text: isAdmin()
                ? 'Create a group and assign members to start a shared conversation.'
                : 'Groups you are assigned to will appear here.',
              action: isAdmin() ? { label: 'Create group', onClick: () => this.openCreateDialog() } : null,
            })
      );
      return;
    }

    const list = el('div', { class: 'record-list' });
    for (const group of this.items) list.append(this.renderCard(group));
    listEl.append(list);
  }

  renderCard(group) {
    const card = el('div', { class: 'record-card' });
    card.append(avatar(group.name, resolveMediaUrl(group.image_url), { size: '', square: true }));

    const meta = el('div', { class: 'record-card__meta' });
    meta.append(el('span', { text: `${group.member_count ?? group.members_count ?? 0} members` }));
    if (group.last_activity_at) meta.append(el('span', { text: `Active ${formatRelative(group.last_activity_at)}` }));
    if (group.is_archived) meta.append(el('span', { class: 'tag tag--warning', text: 'Archived' }));

    const body = el('div', { class: 'record-card__body' }, [
      el('div', { class: 'record-card__title', text: group.name || 'Group' }),
      group.description ? el('div', { class: 'text-sm text-muted clamp-2', text: group.description }) : null,
      meta,
    ]);
    card.append(body);

    const actions = el('div', { class: 'record-card__actions' });
    if (group.conversation_id) {
      actions.append(
        iconButton('message-square', `Open ${group.name} conversation`, {
          className: 'icon-btn--sm',
          onClick: () => {
            window.location.href = `chat.html?c=${encodeURIComponent(group.conversation_id)}`;
          },
        })
      );
    }
    actions.append(
      iconButton('info', `${group.name} information`, {
        className: 'icon-btn--sm',
        onClick: () => this.openGroup(group.id),
      })
    );

    if (isAdmin()) {
      const more = iconButton('more-vertical', `Manage ${group.name}`, { className: 'icon-btn--sm' });
      more.addEventListener('click', () =>
        openMenu(more, [
          { label: 'Rename', icon: 'pencil', onClick: () => this.renameGroup(group) },
          { label: 'Change image', icon: 'image', onClick: () => this.changeImage(group) },
          { label: 'Manage members', icon: 'users', onClick: () => this.openGroup(group.id) },
          { separator: true },
          {
            label: group.is_archived ? 'Restore group' : 'Archive group',
            icon: 'folder-open',
            danger: !group.is_archived,
            onClick: () => this.toggleArchive(group),
          },
        ])
      );
      actions.append(more);
    }

    card.append(actions);
    return card;
  }

  /* ============================================================
     Creation
     ============================================================ */

  openCreateDialog() {
    if (!isAdmin()) return;

    const nameId = uid('g');
    const descId = uid('g');
    const nameInput = el('input', { class: 'input', id: nameId, maxLength: 120, placeholder: 'Executive Committee' });
    const nameError = el('div', { class: 'field__error' });
    const descInput = el('textarea', { class: 'textarea', id: descId, maxLength: 500, rows: 2 });

    const picker = buildMemberPicker();

    const body = el('div', { class: 'stack' }, [
      el('div', { class: 'field' }, [el('label', { class: 'field__label', for: nameId, text: 'Group name' }), nameInput, nameError]),
      el('div', { class: 'field' }, [el('label', { class: 'field__label', for: descId, text: 'Description (optional)' }), descInput]),
      picker.root,
    ]);

    const { close } = openModal({
      title: 'Create group',
      body,
      size: 'lg',
      actions: [
        { label: 'Cancel', value: null },
        {
          label: 'Create group',
          variant: 'primary',
          closeOnClick: false,
          onClick: async ({ button }) => {
            const name = nameInput.value.trim();
            setFieldError(nameInput, nameError, '');
            if (!name) {
              setFieldError(nameInput, nameError, 'Enter a group name.');
              return false;
            }
            const members = picker.selected();
            setBusy(button, true);
            try {
              await api.groups.create({
                name,
                description: descInput.value.trim(),
                members,
              });
              toast('Group created.', { type: 'success' });
              close(true);
              this.load({ reset: true });
            } catch (error) {
              if (error instanceof ApiError && error.errors?.name) {
                setFieldError(nameInput, nameError, [].concat(error.errors.name)[0]);
              } else {
                toastApiError(error);
              }
              return false;
            } finally {
              setBusy(button, false);
            }
            return false;
          },
        },
      ],
    });

    picker.load();
  }

  async renameGroup(group) {
    const { promptDialog } = await import('./ui.js');
    const name = await promptDialog({
      title: 'Rename group',
      label: 'Group name',
      value: group.name || '',
      confirmLabel: 'Save',
      maxLength: 120,
      validate: (v) => (v ? null : 'Enter a group name.'),
    });
    if (!name) return;
    try {
      await api.groups.update(group.id, { name });
      toast('Group renamed.', { type: 'success' });
      this.load({ reset: true });
    } catch (error) {
      toastApiError(error);
    }
  }

  async changeImage(group) {
    const { pickFiles, validateFile } = await import('./media.js');
    const [file] = await pickFiles({ accept: 'image/*' });
    if (!file) return;
    const check = validateFile(file, 'image');
    if (!check.ok) {
      toast(check.message, { type: 'error' });
      return;
    }
    const form = new FormData();
    form.append('image', file, file.name);
    try {
      await api.groups.updateImage(group.id, form);
      toast('Group image updated.', { type: 'success' });
      this.load({ reset: true });
    } catch (error) {
      toastApiError(error);
    }
  }

  async toggleArchive(group) {
    const archiving = !group.is_archived;
    const ok = await confirmDialog({
      title: archiving ? 'Archive this group?' : 'Restore this group?',
      message: archiving
        ? 'Members will no longer be able to send messages in this group. History is retained.'
        : 'Members will be able to send messages in this group again.',
      confirmLabel: archiving ? 'Archive' : 'Restore',
      danger: archiving,
    });
    if (!ok) return;
    try {
      await api.groups.archive(group.id, archiving);
      toast(archiving ? 'Group archived.' : 'Group restored.', { type: 'success' });
      this.load({ reset: true });
    } catch (error) {
      toastApiError(error);
    }
  }

  /* ============================================================
     Group information
     ============================================================ */

  async openGroup(groupId) {
    const body = el('div', {}, [loadingRow('Loading group…')]);
    const { close } = openModal({ title: 'Group information', body, size: 'lg' });

    let group;
    try {
      group = await api.groups.get(groupId);
    } catch (error) {
      clear(body);
      body.append(
        error instanceof ApiError && error.isForbidden
          ? errorState({ title: 'Not authorized', text: 'You do not have access to this group.' })
          : errorState({ text: error.message })
      );
      return;
    }

    const admin = isAdmin();
    clear(body);

    const header = el('div', { class: 'group-header' }, [
      avatar(group.name, resolveMediaUrl(group.image_url), { size: 'lg', square: true }),
      el('div', { class: 'group-header__body' }, [
        el('h3', { text: group.name || 'Group' }),
        group.description ? el('p', { class: 'text-sm text-muted', text: group.description }) : null,
        el('div', { class: 'text-xs text-muted', text: `${group.member_count ?? 0} members` }),
      ]),
    ]);
    if (group.conversation_id) {
      const open = el('a', { class: 'btn btn--primary btn--sm', href: `chat.html?c=${encodeURIComponent(group.conversation_id)}`, text: 'Open conversation' });
      header.append(open);
    }
    body.append(header);

    /* ---- tabs ---- */
    const tabs = el('div', { class: 'tabs', role: 'tablist' });
    const panels = el('div', { style: { paddingTop: 'var(--sp-4)' } });
    const tabDefs = [
      { key: 'members', label: 'Members' },
      { key: 'permissions', label: 'Permissions' },
      { key: 'activity', label: 'Activity' },
    ];
    if (admin) tabDefs.push({ key: 'settings', label: 'Settings' });

    const select = (key) => {
      for (const btn of tabs.querySelectorAll('.tab')) btn.setAttribute('aria-selected', String(btn.dataset.key === key));
      clear(panels);
      if (key === 'members') this.renderMembersPanel(panels, group, admin);
      else if (key === 'permissions') this.renderPermissionsPanel(panels, group);
      else if (key === 'activity') this.renderActivityPanel(panels, group);
      else this.renderSettingsPanel(panels, group, close);
    };

    for (const def of tabDefs) {
      const btn = el('button', { type: 'button', class: 'tab', role: 'tab', dataset: { key: def.key }, text: def.label, 'aria-selected': 'false' });
      btn.addEventListener('click', () => select(def.key));
      tabs.append(btn);
    }
    body.append(tabs, panels);
    select('members');
  }

  async renderMembersPanel(container, group, admin) {
    clear(container);
    container.append(loadingRow('Loading members…'));
    try {
      const response = await api.groups.members(group.id, { limit: 200 });
      const page = normalizePage(response.data ?? response);
      clear(container);

      if (admin) {
        const add = el('button', { type: 'button', class: 'btn btn--sm' });
        add.append(icon('user-plus', { size: 15 }), el('span', { text: 'Add members' }));
        add.addEventListener('click', () => this.openAddMembers(group, container));
        container.append(el('div', { class: 'row', style: { marginBottom: 'var(--sp-3)' } }, [add]));
      }

      if (!page.items.length) {
        container.append(emptyState({ icon: 'users', title: 'No members', text: 'This group has no members yet.' }));
        return;
      }

      const list = el('div', { class: 'record-list' });
      for (const member of page.items) {
        const row = el('div', { class: 'record-card' });
        row.append(avatar(member.display_name, resolveMediaUrl(member.avatar_url), { size: 'sm' }));
        row.append(
          el('div', { class: 'record-card__body' }, [
            el('div', { class: 'record-card__title text-sm', text: member.display_name || 'Member' }),
            el('div', { class: 'record-card__meta' }, [
              member.is_admin ? el('span', { class: 'tag tag--info', text: 'Administrator' }) : null,
              member.is_active === false ? el('span', { class: 'tag tag--danger', text: 'Inactive' }) : null,
            ]),
          ])
        );
        if (admin && !member.is_admin) {
          row.append(
            el('div', { class: 'record-card__actions' }, [
              iconButton('user-x', `Remove ${member.display_name || 'member'} from group`, {
                className: 'icon-btn--sm icon-btn--danger',
                onClick: async () => {
                  const ok = await confirmDialog({
                    title: 'Remove from group?',
                    message: `${member.display_name || 'This member'} will lose access to this group and its messages.`,
                    confirmLabel: 'Remove',
                    danger: true,
                  });
                  if (!ok) return;
                  try {
                    await api.groups.removeMember(group.id, member.id);
                    toast('Member removed from group.', { type: 'success' });
                    this.renderMembersPanel(container, group, admin);
                  } catch (error) {
                    toastApiError(error);
                  }
                },
              }),
            ])
          );
        }
        list.append(row);
      }
      container.append(list);
    } catch (error) {
      clear(container);
      container.append(errorState({ text: error.message }));
    }
  }

  openAddMembers(group, panelContainer) {
    const picker = buildMemberPicker({ excludeIds: [] });
    const { close } = openModal({
      title: 'Add members',
      body: picker.root,
      size: 'lg',
      actions: [
        { label: 'Cancel', value: null },
        {
          label: 'Add selected',
          variant: 'primary',
          closeOnClick: false,
          onClick: async ({ button }) => {
            const ids = picker.selected();
            if (!ids.length) {
              toast('Select at least one member.', { type: 'warning' });
              return false;
            }
            setBusy(button, true);
            try {
              await api.groups.addMembers(group.id, ids);
              toast(`${ids.length} member${ids.length === 1 ? '' : 's'} added.`, { type: 'success' });
              close(true);
              this.renderMembersPanel(panelContainer, group, true);
            } catch (error) {
              toastApiError(error);
              return false;
            } finally {
              setBusy(button, false);
            }
            return false;
          },
        },
      ],
    });
    picker.load();
  }

  renderPermissionsPanel(container, group) {
    clear(container);
    const perms = group.permissions || {};
    const rows = [
      ['Members can send messages', perms.members_can_send !== false],
      ['Members can send media', perms.members_can_send_media !== false],
      ['Members can send voice notes', perms.members_can_send_voice !== false],
      ['Members can see the member list', perms.members_can_view_members !== false],
      ['Members can leave this group', !!(perms.members_can_leave ?? getFeatures().memberLeaveGroup)],
    ];
    const list = el('div', {});
    for (const [label, enabled] of rows) {
      list.append(
        el('div', { class: 'setting-row' }, [
          el('div', { class: 'setting-row__body' }, [el('div', { class: 'setting-row__label', text: label })]),
          el('div', { class: 'setting-row__control' }, [
            el('span', { class: `tag tag--dot ${enabled ? 'tag--success' : 'tag--danger'}`, text: enabled ? 'Allowed' : 'Not allowed' }),
          ]),
        ])
      );
    }
    container.append(list);
    container.append(
      el('p', { class: 'text-xs text-muted', text: 'Permissions are enforced by the backend. Controls shown elsewhere in the app reflect these values but are never the security boundary.' })
    );
  }

  async renderActivityPanel(container, group) {
    clear(container);
    container.append(loadingRow('Loading activity…'));
    try {
      const response = await api.groups.activity(group.id, { limit: 40 });
      const page = normalizePage(response.data ?? response);
      clear(container);
      if (!page.items.length) {
        container.append(emptyState({ icon: 'activity', title: 'No activity recorded', text: 'Membership and setting changes will appear here.' }));
        return;
      }
      const list = el('div', { class: 'activity-list' });
      for (const entry of page.items) {
        list.append(
          el('div', { class: 'activity-item' }, [
            el('div', { class: 'activity-item__icon' }, [icon('activity', { size: 15 })]),
            el('div', { class: 'activity-item__body' }, [
              el('div', { class: 'activity-item__title', text: entry.description || entry.action || 'Activity' }),
              el('div', { class: 'activity-item__meta', text: formatRelative(entry.created_at) }),
            ]),
          ])
        );
      }
      container.append(list);
    } catch (error) {
      clear(container);
      container.append(errorState({ text: error.message }));
    }
  }

  renderSettingsPanel(container, group, closeModalFn) {
    clear(container);
    const nameId = uid('gs');
    const descId = uid('gs');
    const nameInput = el('input', { class: 'input', id: nameId, value: group.name || '', maxLength: 120 });
    const descInput = el('textarea', { class: 'textarea', id: descId, value: group.description || '', maxLength: 500, rows: 3 });

    const save = el('button', { type: 'button', class: 'btn btn--primary', text: 'Save changes' });
    save.addEventListener('click', async () => {
      setBusy(save, true);
      try {
        await api.groups.update(group.id, { name: nameInput.value.trim(), description: descInput.value.trim() });
        toast('Group updated.', { type: 'success' });
        this.load({ reset: true });
      } catch (error) {
        toastApiError(error);
      } finally {
        setBusy(save, false);
      }
    });

    const archive = el('button', { type: 'button', class: 'btn btn--danger', text: group.is_archived ? 'Restore group' : 'Archive group' });
    archive.addEventListener('click', async () => {
      await this.toggleArchive(group);
      closeModalFn?.();
    });

    container.append(
      el('div', { class: 'stack' }, [
        el('div', { class: 'field' }, [el('label', { class: 'field__label', for: nameId, text: 'Group name' }), nameInput]),
        el('div', { class: 'field' }, [el('label', { class: 'field__label', for: descId, text: 'Description' }), descInput]),
        el('div', { class: 'row' }, [save]),
        el('hr'),
        el('div', { class: 'stack-sm' }, [
          el('div', { class: 'fw-semibold', text: 'Danger zone' }),
          el('p', { class: 'text-sm text-muted', text: 'Archiving stops new messages but preserves all history.' }),
          el('div', { class: 'row' }, [archive]),
        ]),
      ])
    );
  }
}

/* ============================================================
   Member selection widget
   ============================================================ */

/**
 * Searchable, paginated member picker with a live selected count.
 * Members the backend marks ineligible are displayed but not selectable.
 */
export function buildMemberPicker({ excludeIds = [] } = {}) {
  const selectedIds = new Set();
  const searchId = uid('mp');

  const searchInput = el('input', { class: 'input', id: searchId, type: 'search', placeholder: 'Search members', autocomplete: 'off' });
  const countEl = el('div', { class: 'text-sm text-muted', text: '0 selected' });
  const list = el('div', { class: 'pick-list' });
  const moreWrap = el('div', {});

  let cursor = null;
  let loading = false;

  const updateCount = () => {
    countEl.textContent = `${selectedIds.size} selected`;
  };

  async function load(reset = true) {
    if (loading) return;
    loading = true;
    if (reset) {
      cursor = null;
      clear(list);
      list.append(skeletonList(4));
    }
    clear(moreWrap);
    try {
      const params = { limit: 30, is_active: true };
      const q = searchInput.value.trim();
      if (q) params.search = q;
      if (cursor) params.cursor = cursor;

      const response = await api.members.selectable(params);
      const page = normalizePage(response.data ?? response);
      if (reset) clear(list);
      cursor = page.next;

      const visible = page.items.filter((m) => !excludeIds.includes(String(m.id)) && !m.is_admin);
      if (!visible.length && reset) {
        list.append(emptyState({ icon: 'users', title: 'No members available', text: 'Create members before assigning them to a group.' }));
        return;
      }

      for (const member of visible) {
        const eligible = member.is_active !== false && member.is_selectable !== false;
        const id = String(member.id);
        const checkbox = el('input', { type: 'checkbox', disabled: !eligible, checked: selectedIds.has(id) });

        const item = el('label', {
          class: 'pick-item',
          'aria-disabled': String(!eligible),
        });
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) selectedIds.add(id);
          else selectedIds.delete(id);
          updateCount();
        });

        item.append(
          checkbox,
          avatar(member.display_name, resolveMediaUrl(member.avatar_url), { size: 'sm' }),
          el('span', { class: 'pick-item__body' }, [
            el('span', { class: 'pick-item__name truncate', text: member.display_name || 'Member' }),
            el('span', { class: 'pick-item__meta', text: eligible ? member.phone || '' : 'Inactive — cannot be added' }),
          ])
        );
        list.append(item);
      }

      if (cursor) {
        const more = el('button', { type: 'button', class: 'btn btn--sm btn--block', text: 'Load more members' });
        more.addEventListener('click', () => load(false));
        moreWrap.append(more);
      }
    } catch (error) {
      if (reset) clear(list);
      list.append(errorState({ text: error.message, onRetry: () => load(true) }));
    } finally {
      loading = false;
    }
  }

  searchInput.addEventListener(
    'input',
    debounce(() => load(true), 300)
  );

  const root = el('div', { class: 'stack-sm' }, [
    el('div', { class: 'row' }, [
      el('label', { class: 'field__label', for: searchId, text: 'Select members' }),
      el('span', { class: 'spacer' }),
      countEl,
    ]),
    searchInput,
    list,
    moreWrap,
  ]);

  return {
    root,
    load,
    selected: () => Array.from(selectedIds),
  };
}

export default GroupsController;
