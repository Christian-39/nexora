/**
 * NEXORA — settings.js
 * Settings surfaces for both roles.
 *
 * Member: profile, appearance, notifications, security (PIN), sessions.
 * Admin: everything above plus organization, branding, policies, messaging
 *        limits, notification policy and security policy.
 *
 * Only fields the backend reports as editable are rendered as editable.
 * Infrastructure/environment secrets are never surfaced here.
 */

import { ApiError, api, resolveMediaUrl } from './api.js';
import { changePin, getUser, isAdmin, listSessions, patchUser, revokeSession, validateNewPin } from './auth.js';
import { applyBranding, getConfig, getPolicies, getThemePreference, loadBranding, setTheme } from './theme.js';
import {
  avatar,
  confirmDialog,
  emptyState,
  errorState,
  icon,
  iconButton,
  loadingRow,
  openModal,
  setBusy,
  setFieldError,
  showAlert,
  hideAlert,
  toast,
  toastApiError,
} from './ui.js';
import { clear, el, formatRelative, isHexColor, uid } from './utils.js';
import { isPushSupported, isSubscribed, permissionState, subscribe, unsubscribe } from './push.js';
import { isBadgeSupported } from './notifications.js';
import { pickFiles, validateFile } from './media.js';
import { buildPinFields } from './pin.js';

/* ============================================================
   Controller
   ============================================================ */

export class SettingsController {
  /** @param {{navEl:HTMLElement, panelEl:HTMLElement}} refs */
  constructor(refs) {
    this.refs = refs;
    this.sections = [];
    this.active = null;
    this.serverSettings = null;
  }

  async init() {
    const admin = isAdmin();
    this.sections = [
      { key: 'profile', label: 'Profile', render: (c) => this.renderProfile(c) },
      { key: 'appearance', label: 'Appearance', render: (c) => this.renderAppearance(c) },
      { key: 'notifications', label: 'Notifications', render: (c) => this.renderNotifications(c) },
      { key: 'security', label: 'Security', render: (c) => this.renderSecurity(c) },
      { key: 'sessions', label: 'Active sessions', render: (c) => this.renderSessions(c) },
      { key: 'policies', label: 'Policies', render: (c) => this.renderPolicies(c) },
    ];

    if (admin) {
      this.sections.splice(
        1,
        0,
        { key: 'organization', label: 'Organization', render: (c) => this.renderOrganization(c) },
        { key: 'branding', label: 'Branding', render: (c) => this.renderBranding(c) },
        { key: 'messaging', label: 'Messaging', render: (c) => this.renderMessaging(c) },
        { key: 'securityPolicy', label: 'Security policy', render: (c) => this.renderSecurityPolicy(c) }
      );
    }

    this.renderNav();
    const requested = new URLSearchParams(window.location.search).get('s');
    this.select(this.sections.some((s) => s.key === requested) ? requested : this.sections[0].key);
  }

  renderNav() {
    const { navEl } = this.refs;
    clear(navEl);
    for (const section of this.sections) {
      const btn = el('button', { type: 'button', text: section.label, dataset: { key: section.key } });
      btn.addEventListener('click', () => this.select(section.key));
      navEl.append(btn);
    }
  }

  select(key) {
    this.active = key;
    for (const btn of this.refs.navEl.querySelectorAll('button')) {
      btn.setAttribute('aria-current', String(btn.dataset.key === key));
    }
    const section = this.sections.find((s) => s.key === key);
    const { panelEl } = this.refs;
    clear(panelEl);
    const container = el('div', { class: 'settings-section stack' });
    panelEl.append(container);
    section?.render(container);
  }

  /** Lazily load the admin settings document once. */
  async ensureSettings() {
    if (this.serverSettings) return this.serverSettings;
    this.serverSettings = await api.settings.get();
    return this.serverSettings;
  }

  /* ============================================================
     Profile
     ============================================================ */

  async renderProfile(container) {
    clear(container);

    // The session bootstrap (auth.js) already fetched /api/me/ before this
    // page rendered — the ProfileSerializer document includes everything the
    // profile form needs (editable_fields, privacy flags, avatar). Re-fetch
    // only when the cached user genuinely lacks the data (older backend).
    let me = getUser();
    if (!me || !Array.isArray(me.editable_fields)) {
      try {
        me = await api.me.get();
        patchUser(me);
      } catch (error) {
        clear(container);
        container.append(errorState({ text: error.message }));
        return;
      }
    }

    const editable = new Set(me.editable_fields || ['display_name', 'avatar', 'phone_visible']);

    const nameId = uid('p');
    const nameInput = el('input', {
      class: 'input',
      id: nameId,
      value: me.display_name || '',
      maxLength: 120,
      disabled: !editable.has('display_name'),
    });
    const nameError = el('div', { class: 'field__error' });

    const avatarNode = avatar(me.display_name, resolveMediaUrl(me.avatar_url), { size: 'xl' });
    const changePhoto = el('button', { type: 'button', class: 'btn btn--sm', text: 'Change photo' });
    changePhoto.hidden = !editable.has('avatar');
    changePhoto.addEventListener('click', async () => {
      const [file] = await pickFiles({ accept: 'image/*' });
      if (!file) return;
      const check = validateFile(file, 'image');
      if (!check.ok) {
        toast(check.message, { type: 'error' });
        return;
      }
      const form = new FormData();
      form.append('avatar', file, file.name);
      setBusy(changePhoto, true);
      try {
        const updated = await api.me.updateAvatar(form);
        patchUser(updated);
        toast('Profile photo updated.', { type: 'success' });
        this.select('profile');
      } catch (error) {
        toastApiError(error);
      } finally {
        setBusy(changePhoto, false);
      }
    });

    const phoneVisible = el('input', { type: 'checkbox', checked: me.phone_visible !== false, disabled: !editable.has('phone_visible') });
    const presenceVisible = el('input', { type: 'checkbox', checked: me.presence_visible !== false, disabled: !editable.has('presence_visible') });

    const save = el('button', { type: 'button', class: 'btn btn--primary', text: 'Save profile' });
    save.addEventListener('click', async () => {
      setFieldError(nameInput, nameError, '');
      const displayName = nameInput.value.trim();
      if (editable.has('display_name') && !displayName) {
        setFieldError(nameInput, nameError, 'Enter a display name.');
        return;
      }
      setBusy(save, true);
      try {
        const payload = {};
        if (editable.has('display_name')) payload.display_name = displayName;
        if (editable.has('phone_visible')) payload.phone_visible = phoneVisible.checked;
        if (editable.has('presence_visible')) payload.presence_visible = presenceVisible.checked;
        const updated = await api.me.update(payload);
        patchUser(updated);
        toast('Profile saved.', { type: 'success' });
      } catch (error) {
        if (error instanceof ApiError && error.errors?.display_name) {
          setFieldError(nameInput, nameError, [].concat(error.errors.display_name)[0]);
        } else {
          toastApiError(error);
        }
      } finally {
        setBusy(save, false);
      }
    });

    container.append(
      el('h2', { text: 'Profile' }),
      el('div', { class: 'row' }, [avatarNode, el('div', { class: 'stack-sm' }, [changePhoto, el('div', { class: 'text-xs text-muted', text: 'JPG, PNG or WebP.' })])]),
      el('div', { class: 'field' }, [el('label', { class: 'field__label', for: nameId, text: 'Display name' }), nameInput, nameError]),
      el('dl', { class: 'dl' }, [
        el('dt', { text: 'Phone' }),
        el('dd', { text: me.phone || '—' }),
        el('dt', { text: 'Login identifier' }),
        el('dd', { text: me.login_identifier || me.phone || '—' }),
        el('dt', { text: 'Role' }),
        el('dd', { text: me.is_admin ? 'Administrator' : 'Member' }),
      ]),
      el('div', { class: 'setting-row' }, [
        el('div', { class: 'setting-row__body' }, [
          el('div', { class: 'setting-row__label', text: 'Show my phone number' }),
          el('div', { class: 'setting-row__desc', text: 'Controls whether other authorized participants can see your number.' }),
        ]),
        el('label', { class: 'check setting-row__control' }, [phoneVisible, el('span', { class: 'check__text', text: 'Visible' })]),
      ]),
      el('div', { class: 'setting-row' }, [
        el('div', { class: 'setting-row__body' }, [
          el('div', { class: 'setting-row__label', text: 'Show my online status' }),
          el('div', { class: 'setting-row__desc', text: 'When off, others see neither your presence nor your last-seen time.' }),
        ]),
        el('label', { class: 'check setting-row__control' }, [presenceVisible, el('span', { class: 'check__text', text: 'Visible' })]),
      ]),
      el('div', { class: 'row' }, [save])
    );
  }

  /* ============================================================
     Appearance
     ============================================================ */

  renderAppearance(container) {
    clear(container);
    const current = getThemePreference();
    const group = el('div', { class: 'segmented', role: 'group', 'aria-label': 'Theme' });
    for (const [value, label, iconName] of [
      ['light', 'Light', 'sun'],
      ['dark', 'Dark', 'moon'],
      ['system', 'System', 'monitor'],
    ]) {
      const btn = el('button', { type: 'button', 'aria-pressed': String(current === value) });
      btn.append(icon(iconName, { size: 15 }), el('span', { text: label }));
      btn.addEventListener('click', () => {
        setTheme(value);
        this.renderAppearance(container);
      });
      group.append(btn);
    }

    container.append(
      el('h2', { text: 'Appearance' }),
      el('div', { class: 'setting-row' }, [
        el('div', { class: 'setting-row__body' }, [
          el('div', { class: 'setting-row__label', text: 'Theme' }),
          el('div', { class: 'setting-row__desc', text: 'System follows your device setting and updates automatically.' }),
        ]),
        el('div', { class: 'setting-row__control' }, [group]),
      ]),
      el('p', { class: 'text-xs text-muted', text: 'Reduced-motion and high-contrast preferences from your operating system are always respected.' })
    );
  }

  /* ============================================================
     Notifications (device-level)
     ============================================================ */

  async renderNotifications(container) {
    clear(container);
    container.append(el('h2', { text: 'Notifications' }));

    if (!isPushSupported()) {
      container.append(
        el('div', { class: 'alert alert--info' }, [
          icon('info', { size: 16 }),
          el('span', { text: 'Push notifications are not supported in this browser. In-app notifications still work while the app is open.' }),
        ])
      );
    } else {
      const state = permissionState();
      const subscribed = await isSubscribed();

      const toggle = el('button', {
        type: 'button',
        class: subscribed ? 'btn' : 'btn btn--primary',
        text: subscribed ? 'Disable on this device' : 'Enable on this device',
      });
      toggle.disabled = state === 'denied';
      toggle.addEventListener('click', async () => {
        setBusy(toggle, true);
        try {
          if (subscribed) {
            await unsubscribe();
            toast('Notifications disabled on this device.', { type: 'success' });
          } else {
            const result = await subscribe();
            if (result.ok) toast('Notifications enabled on this device.', { type: 'success' });
            else if (result.reason === 'denied') toast('Notifications are blocked in your browser settings.', { type: 'warning' });
            else if (result.reason === 'push-disabled') toast('Push notifications are not enabled for this deployment.', { type: 'info' });
            else toast('Notifications could not be enabled.', { type: 'error' });
          }
          this.select('notifications');
        } finally {
          setBusy(toggle, false);
        }
      });

      container.append(
        el('div', { class: 'setting-row' }, [
          el('div', { class: 'setting-row__body' }, [
            el('div', { class: 'setting-row__label', text: 'Push notifications' }),
            el('div', {
              class: 'setting-row__desc',
              text:
                state === 'denied'
                  ? 'Blocked by your browser. Change the site permission to enable notifications.'
                  : 'Receive alerts about new messages when the app is closed.',
            }),
          ]),
          el('div', { class: 'setting-row__control' }, [toggle]),
        ])
      );
    }

    /* ---- preview privacy + per-user preferences ---- */
    let prefsData = {};
    try {
      prefsData = (await api.me.preferences()) || {};
    } catch {
      prefsData = {};
    }

    const previewToggle = el('input', { type: 'checkbox', checked: prefsData.notification_previews !== false });
    const soundToggle = el('input', { type: 'checkbox', checked: prefsData.notification_sound !== false });
    const groupToggle = el('input', { type: 'checkbox', checked: prefsData.group_notifications !== false });

    const savePrefs = el('button', { type: 'button', class: 'btn btn--primary', text: 'Save preferences' });
    savePrefs.addEventListener('click', async () => {
      setBusy(savePrefs, true);
      try {
        await api.me.savePreferences({
          notification_previews: previewToggle.checked,
          notification_sound: soundToggle.checked,
          group_notifications: groupToggle.checked,
        });
        toast('Notification preferences saved.', { type: 'success' });
      } catch (error) {
        toastApiError(error);
      } finally {
        setBusy(savePrefs, false);
      }
    });

    container.append(
      settingToggleRow(previewToggle, 'Show message previews', 'When off, notifications say only that a new message arrived — never its content.'),
      settingToggleRow(soundToggle, 'Notification sound', 'Play a sound for in-app notifications.'),
      settingToggleRow(groupToggle, 'Group notifications', 'Receive notifications for group messages.'),
      el('div', { class: 'row' }, [savePrefs])
    );

    if (!isBadgeSupported()) {
      container.append(
        el('p', { class: 'text-xs text-muted', text: 'App icon badges are not available on this platform. Unread counts are still shown inside the app.' })
      );
    }
  }

  /* ============================================================
     Security — credential change
     ============================================================ */

  renderSecurity(container) {
    clear(container);
    const alertEl = el('div', { class: 'alert', hidden: true, role: 'alert' });

    const fields = buildPinFields({ includeCurrent: true });
    const submit = el('button', { type: 'button', class: 'btn btn--primary', text: 'Update PIN' });

    submit.addEventListener('click', async () => {
      hideAlert(alertEl);
      fields.clearErrors();
      const values = fields.values();
      if (!values.currentPin || values.currentPin.length !== 6) {
        fields.setError('current', 'Enter your current six-digit PIN.');
        return;
      }
      const problem = validateNewPin(values.newPin, values.confirmPin, values.currentPin);
      if (problem) {
        fields.setError(problem.field, problem.message);
        return;
      }

      setBusy(submit, true);
      fields.setDisabled(true);
      try {
        await changePin({
          currentPin: values.currentPin,
          newPin: values.newPin,
          confirmPin: values.confirmPin,
        });
        fields.reset();
        showAlert(alertEl, 'Your PIN has been updated.', 'success');
        toast('PIN updated.', { type: 'success' });
      } catch (error) {
        fields.setDisabled(false);
        if (error instanceof ApiError && error.isValidation) {
          const fieldError = error.firstFieldError();
          if (fieldError?.field === 'current_pin') fields.setError('current', fieldError.message);
          else if (fieldError?.field === 'new_pin') fields.setError('new', fieldError.message);
          else if (fieldError?.field === 'confirm_pin') fields.setError('confirm', fieldError.message);
          else showAlert(alertEl, error.message, 'error');
        } else if (error instanceof ApiError && error.isRateLimited) {
          showAlert(alertEl, 'Too many attempts. Please wait before trying again.', 'error');
        } else {
          showAlert(alertEl, error.message, 'error');
        }
      } finally {
        setBusy(submit, false);
        fields.setDisabled(false);
      }
    });

    container.append(
      el('h2', { text: 'Security' }),
      el('p', { class: 'text-sm text-muted', text: 'Your PIN is six digits. It is never displayed, stored in this browser, or included in logs.' }),
      alertEl,
      fields.root,
      el('div', { class: 'row' }, [submit])
    );
  }

  /* ============================================================
     Active sessions
     ============================================================ */

  async renderSessions(container) {
    clear(container);
    container.append(el('h2', { text: 'Active sessions' }), loadingRow('Loading sessions…'));

    let sessions;
    try {
      sessions = await listSessions();
    } catch (error) {
      clear(container);
      container.append(el('h2', { text: 'Active sessions' }));
      if (error instanceof ApiError && error.isNotFound) {
        container.append(el('p', { class: 'text-sm text-muted', text: 'Session management is not enabled for this deployment.' }));
      } else {
        container.append(errorState({ text: error.message }));
      }
      return;
    }

    const items = Array.isArray(sessions) ? sessions : sessions?.results || [];
    clear(container);
    container.append(
      el('h2', { text: 'Active sessions' }),
      el('p', { class: 'text-sm text-muted', text: 'Devices currently signed in to your account. Revoke any you do not recognise.' })
    );

    if (!items.length) {
      container.append(emptyState({ icon: 'smartphone', title: 'No other sessions', text: 'You are signed in on this device only.' }));
      return;
    }

    const list = el('div', {});
    for (const session of items) {
      const row = el('div', { class: 'session-item' });
      row.append(el('span', {}, [icon(session.platform?.toLowerCase().includes('win') || session.platform?.toLowerCase().includes('mac') ? 'monitor' : 'smartphone')]));
      row.append(
        el('div', { class: 'session-item__body' }, [
          el('div', { class: 'session-item__device', text: [session.platform, session.browser].filter(Boolean).join(' · ') || 'Unknown device' }),
          el('div', { class: 'session-item__meta', text: session.last_active_at ? `Last active ${formatRelative(session.last_active_at)}` : '' }),
        ])
      );
      if (session.is_current) {
        row.append(el('span', { class: 'tag tag--success', text: 'This device' }));
      } else {
        row.append(
          iconButton('log-out', 'Revoke this session', {
            className: 'icon-btn--danger',
            onClick: async () => {
              const ok = await confirmDialog({
                title: 'Revoke this session?',
                message: 'That device will be signed out immediately.',
                confirmLabel: 'Revoke',
                danger: true,
              });
              if (!ok) return;
              try {
                await revokeSession(session.id);
                toast('Session revoked.', { type: 'success' });
                this.select('sessions');
              } catch (error) {
                toastApiError(error);
              }
            },
          })
        );
      }
      list.append(row);
    }
    container.append(list);
  }

  /* ============================================================
     Policies (read-only for members, editable for admins)
     ============================================================ */

  async renderPolicies(container) {
    clear(container);
    container.append(el('h2', { text: 'Policies' }));
    await loadBranding();
    const policies = getPolicies();

    if (!policies.length) {
      container.append(emptyState({ icon: 'file-text', title: 'No policies published', text: 'Published policies will appear here.' }));
    } else {
      for (const policy of policies) {
        const card = el('div', { class: 'card' });
        card.append(el('div', { class: 'card__header' }, [el('h3', { class: 'card__title', text: policy.title })]));
        const body = el('div', { class: 'card__body' });
        if (policy.url) {
          body.append(el('a', { href: policy.url, target: '_blank', rel: 'noopener noreferrer', text: 'Read the full policy' }));
        }
        if (policy.body) {
          // Policy text is backend-authored plain text; rendered as text only.
          for (const paragraph of policy.body.split(/\n{2,}/)) {
            body.append(el('p', { class: 'text-sm', text: paragraph.trim() }));
          }
        }
        card.append(body);
        container.append(card);
      }
    }

    if (isAdmin()) {
      const edit = el('button', { type: 'button', class: 'btn', text: 'Edit policies' });
      edit.addEventListener('click', () => this.openPolicyEditor());
      container.append(el('div', { class: 'row' }, [edit]));
    }
  }

  async openPolicyEditor() {
    const body = el('div', {}, [loadingRow('Loading policies…')]);
    const { close } = openModal({ title: 'Edit policies', body, size: 'lg' });
    let current;
    try {
      current = await api.settings.policies();
    } catch (error) {
      clear(body);
      body.append(errorState({ text: error.message }));
      return;
    }

    const entries = Array.isArray(current) ? current : current?.policies || [];
    const known = [
      { key: 'privacy', title: 'Privacy Policy' },
      { key: 'terms', title: 'Terms of Use' },
      { key: 'community', title: 'Community Rules' },
      { key: 'data', title: 'Data Policy' },
    ];

    clear(body);
    const inputs = new Map();
    for (const def of known) {
      const existing = entries.find((p) => (p.key || p.slug) === def.key) || {};
      const id = uid('pol');
      const textarea = el('textarea', { class: 'textarea', id, rows: 5, value: existing.body || existing.content || '' });
      inputs.set(def.key, textarea);
      body.append(el('div', { class: 'field' }, [el('label', { class: 'field__label', for: id, text: def.title }), textarea]));
    }

    const footer = el('div', { class: 'row' });
    const save = el('button', { type: 'button', class: 'btn btn--primary', text: 'Save policies' });
    save.addEventListener('click', async () => {
      setBusy(save, true);
      try {
        await api.settings.updatePolicies({
          policies: known.map((def) => ({ key: def.key, title: def.title, body: inputs.get(def.key).value })),
        });
        await loadBranding({ force: true });
        toast('Policies saved.', { type: 'success' });
        close(true);
        this.select('policies');
      } catch (error) {
        toastApiError(error);
      } finally {
        setBusy(save, false);
      }
    });
    footer.append(save);
    body.append(footer);
  }

  /* ============================================================
     ADMIN — organization
     ============================================================ */

  async renderOrganization(container) {
    clear(container);
    container.append(el('h2', { text: 'Organization' }), loadingRow('Loading settings…'));

    let settings;
    try {
      settings = await this.ensureSettings();
    } catch (error) {
      clear(container);
      container.append(el('h2', { text: 'Organization' }), errorState({ text: error.message }));
      return;
    }

    clear(container);
    const org = settings.organization || settings;
    const fields = {
      organization_name: textField('Organization name', org.organization_name || org.name || '', { maxLength: 160 }),
      contact_phone: textField('Contact phone', org.contact_phone || '', { type: 'tel', maxLength: 40 }),
      contact_email: textField('Contact email', org.contact_email || '', { type: 'email', maxLength: 160, hint: 'Displayed as contact information only. NEXORA sends no email.' }),
      website: textField('Website', org.website || '', { type: 'url', maxLength: 200 }),
      address: textareaField('Address', org.address || '', { rows: 3, maxLength: 400 }),
      about: textareaField('About', org.about || '', { rows: 4, maxLength: 2000 }),
      support: textareaField('Support information', org.support || '', { rows: 3, maxLength: 1000 }),
    };

    const save = el('button', { type: 'button', class: 'btn btn--primary', text: 'Save organization' });
    save.addEventListener('click', async () => {
      setBusy(save, true);
      try {
        const payload = {};
        for (const [key, field] of Object.entries(fields)) payload[key] = field.value();
        this.serverSettings = await api.settings.update({ organization: payload, ...payload });
        await loadBranding({ force: true });
        toast('Organization settings saved.', { type: 'success' });
      } catch (error) {
        toastApiError(error);
      } finally {
        setBusy(save, false);
      }
    });

    container.append(
      el('h2', { text: 'Organization' }),
      el('div', { class: 'form-grid' }, [fields.organization_name.root, fields.contact_phone.root, fields.contact_email.root, fields.website.root]),
      fields.address.root,
      fields.about.root,
      fields.support.root,
      el('div', { class: 'row' }, [save])
    );
  }

  /* ============================================================
     ADMIN — branding
     ============================================================ */

  async renderBranding(container) {
    clear(container);
    container.append(el('h2', { text: 'Branding' }), loadingRow('Loading branding…'));

    let settings;
    try {
      settings = await this.ensureSettings();
    } catch (error) {
      clear(container);
      container.append(el('h2', { text: 'Branding' }), errorState({ text: error.message }));
      return;
    }

    clear(container);
    const branding = settings.branding || settings;
    const config = getConfig();

    const appName = textField('Application name', branding.app_name || config.app_name || '', { maxLength: 60 });
    const shortName = textField('Short name (home screen)', branding.app_short_name || config.app_short_name || '', { maxLength: 24 });
    const primary = colorField('Primary colour', branding.primary_color || config.primary_color || '#33526E');
    const secondary = colorField('Secondary colour', branding.secondary_color || config.secondary_color || '#1E7A4E');

    const logoPreview = el('img', {
      alt: 'Current logo',
      style: { maxHeight: '48px', width: 'auto', objectFit: 'contain' },
    });
    const logoUrl = resolveMediaUrl(branding.logo_url || config.logo_url);
    if (logoUrl) logoPreview.src = logoUrl;
    else logoPreview.hidden = true;

    const uploadAsset = (kind, label) => {
      const btn = el('button', { type: 'button', class: 'btn btn--sm', text: label });
      btn.addEventListener('click', async () => {
        const [file] = await pickFiles({ accept: 'image/png,image/jpeg,image/webp,image/svg+xml,image/x-icon' });
        if (!file) return;
        const form = new FormData();
        form.append('file', file, file.name);
        setBusy(btn, true);
        try {
          await api.settings.uploadAsset(kind, form);
          this.serverSettings = null;
          await loadBranding({ force: true });
          toast(`${label.replace('Upload ', '')} updated.`, { type: 'success' });
          this.select('branding');
        } catch (error) {
          toastApiError(error);
        } finally {
          setBusy(btn, false);
        }
      });
      return btn;
    };

    const save = el('button', { type: 'button', class: 'btn btn--primary', text: 'Save branding' });
    save.addEventListener('click', async () => {
      if (!isHexColor(primary.value()) || !isHexColor(secondary.value())) {
        toast('Colours must be in #RRGGBB format.', { type: 'error' });
        return;
      }
      setBusy(save, true);
      try {
        const payload = {
          app_name: appName.value(),
          app_short_name: shortName.value(),
          primary_color: primary.value(),
          secondary_color: secondary.value(),
        };
        this.serverSettings = await api.settings.update({ branding: payload, ...payload });
        await loadBranding({ force: true });
        applyBranding();
        toast('Branding saved.', { type: 'success' });
      } catch (error) {
        toastApiError(error);
      } finally {
        setBusy(save, false);
      }
    });

    container.append(
      el('h2', { text: 'Branding' }),
      el('div', { class: 'form-grid' }, [appName.root, shortName.root, primary.root, secondary.root]),
      el('div', { class: 'setting-row' }, [
        el('div', { class: 'setting-row__body' }, [
          el('div', { class: 'setting-row__label', text: 'Logo' }),
          el('div', { class: 'setting-row__desc', text: 'Shown in navigation, sign-in and installable app metadata.' }),
          logoPreview,
        ]),
        el('div', { class: 'setting-row__control' }, [uploadAsset('logo', 'Upload logo')]),
      ]),
      el('div', { class: 'setting-row' }, [
        el('div', { class: 'setting-row__body' }, [
          el('div', { class: 'setting-row__label', text: 'Favicon' }),
          el('div', { class: 'setting-row__desc', text: 'Used in the browser tab and as an installable app icon fallback.' }),
        ]),
        el('div', { class: 'setting-row__control' }, [uploadAsset('favicon', 'Upload favicon')]),
      ]),
      el('div', { class: 'row' }, [save]),
      el('p', { class: 'text-xs text-muted', text: 'Only validated #RRGGBB colour values are applied. Configuration is never evaluated as CSS.' })
    );
  }

  /* ============================================================
     ADMIN — messaging limits
     ============================================================ */

  async renderMessaging(container) {
    clear(container);
    container.append(el('h2', { text: 'Messaging' }), loadingRow('Loading…'));

    let settings;
    try {
      settings = await this.ensureSettings();
    } catch (error) {
      clear(container);
      container.append(el('h2', { text: 'Messaging' }), errorState({ text: error.message }));
      return;
    }

    clear(container);
    const m = settings.messaging || settings.limits || {};

    const maxLength = numberField('Maximum message length (characters)', m.max_message_length ?? 4000, { min: 1, max: 100000 });
    const maxImage = numberField('Maximum image size (MB)', bytesToMb(m.max_image_size ?? 10485760), { min: 1, max: 200 });
    const maxVideo = numberField('Maximum video size (MB)', bytesToMb(m.max_video_size ?? 104857600), { min: 1, max: 2048 });
    const maxVoice = numberField('Maximum voice note length (seconds)', m.max_voice_duration ?? 300, { min: 5, max: 3600 });

    const deleteEveryone = el('input', { type: 'checkbox', checked: m.delete_for_everyone !== false });
    const editing = el('input', { type: 'checkbox', checked: !!m.message_editing });
    const editWindow = numberField('Edit window (minutes)', m.edit_window_minutes ?? 15, { min: 1, max: 1440 });
    const reactions = el('input', { type: 'checkbox', checked: !!m.reactions });
    const memberLeave = el('input', { type: 'checkbox', checked: !!m.member_leave_group });

    const save = el('button', { type: 'button', class: 'btn btn--primary', text: 'Save messaging settings' });
    save.addEventListener('click', async () => {
      setBusy(save, true);
      try {
        this.serverSettings = await api.settings.update({
          messaging: {
            max_message_length: Number(maxLength.value()),
            max_image_size: mbToBytes(maxImage.value()),
            max_video_size: mbToBytes(maxVideo.value()),
            max_voice_duration: Number(maxVoice.value()),
            delete_for_everyone: deleteEveryone.checked,
            message_editing: editing.checked,
            edit_window_minutes: Number(editWindow.value()),
            reactions: reactions.checked,
            member_leave_group: memberLeave.checked,
          },
        });
        await loadBranding({ force: true });
        toast('Messaging settings saved.', { type: 'success' });
      } catch (error) {
        toastApiError(error);
      } finally {
        setBusy(save, false);
      }
    });

    container.append(
      el('h2', { text: 'Messaging' }),
      el('div', { class: 'form-grid' }, [maxLength.root, maxImage.root, maxVideo.root, maxVoice.root, editWindow.root]),
      settingToggleRow(deleteEveryone, 'Allow delete for everyone', 'Authorized senders may remove a message for all participants.'),
      settingToggleRow(editing, 'Allow message editing', 'Text messages only, within the configured edit window.'),
      settingToggleRow(reactions, 'Allow reactions', 'Participants may react to messages with a controlled set of reactions.'),
      settingToggleRow(memberLeave, 'Members may leave groups', 'When off, only administrators change group membership.'),
      el('div', { class: 'row' }, [save])
    );
  }

  /* ============================================================
     ADMIN — security policy
     ============================================================ */

  async renderSecurityPolicy(container) {
    clear(container);
    container.append(el('h2', { text: 'Security policy' }), loadingRow('Loading…'));

    let security;
    try {
      security = await api.security.settings();
    } catch (error) {
      clear(container);
      container.append(el('h2', { text: 'Security policy' }));
      container.append(
        error instanceof ApiError && error.isNotFound
          ? el('p', { class: 'text-sm text-muted', text: 'Security policy management is not exposed by this deployment.' })
          : errorState({ text: error.message })
      );
      return;
    }

    clear(container);
    const attempts = numberField('Failed sign-in attempts before lockout', security.max_login_attempts ?? 5, { min: 3, max: 20 });
    const lockout = numberField('Lockout duration (minutes)', security.lockout_minutes ?? 15, { min: 1, max: 1440 });
    const sessionIdle = numberField('Session idle timeout (minutes)', security.session_idle_minutes ?? 60, { min: 5, max: 10080 });
    const rateWindow = numberField('Sign-in rate limit window (minutes)', security.login_rate_window_minutes ?? 15, { min: 1, max: 120 });
    const pinExpiry = numberField('Require PIN change every (days, 0 = never)', security.pin_max_age_days ?? 0, { min: 0, max: 3650 });

    const save = el('button', { type: 'button', class: 'btn btn--primary', text: 'Save security policy' });
    save.addEventListener('click', async () => {
      setBusy(save, true);
      try {
        await api.security.updateSettings({
          max_login_attempts: Number(attempts.value()),
          lockout_minutes: Number(lockout.value()),
          session_idle_minutes: Number(sessionIdle.value()),
          login_rate_window_minutes: Number(rateWindow.value()),
          pin_max_age_days: Number(pinExpiry.value()),
        });
        toast('Security policy saved.', { type: 'success' });
      } catch (error) {
        toastApiError(error);
      } finally {
        setBusy(save, false);
      }
    });

    container.append(
      el('h2', { text: 'Security policy' }),
      el('div', { class: 'form-grid' }, [attempts.root, lockout.root, sessionIdle.root, rateWindow.root, pinExpiry.root]),
      el('div', { class: 'row' }, [save]),
      el('div', { class: 'alert alert--info' }, [
        icon('shield-check', { size: 16 }),
        el('span', { text: 'These values are enforced by the backend. Infrastructure credentials and environment secrets are never shown or editable here.' }),
      ])
    );
  }
}

/* ============================================================
   Small field builders
   ============================================================ */

function textField(label, value, { type = 'text', maxLength = 200, hint = null } = {}) {
  const id = uid('sf');
  const input = el('input', { class: 'input', id, type, value: value || '', maxLength });
  const root = el('div', { class: 'field' }, [
    el('label', { class: 'field__label', for: id, text: label }),
    input,
    hint ? el('div', { class: 'field__hint', text: hint }) : null,
  ]);
  return { root, input, value: () => input.value.trim() };
}

function textareaField(label, value, { rows = 3, maxLength = 1000 } = {}) {
  const id = uid('sf');
  const input = el('textarea', { class: 'textarea', id, rows, maxLength, value: value || '' });
  const root = el('div', { class: 'field' }, [el('label', { class: 'field__label', for: id, text: label }), input]);
  return { root, input, value: () => input.value.trim() };
}

function numberField(label, value, { min = 0, max = 1000000 } = {}) {
  const id = uid('sf');
  const input = el('input', { class: 'input', id, type: 'number', value: String(value ?? ''), min, max, inputMode: 'numeric' });
  const root = el('div', { class: 'field' }, [el('label', { class: 'field__label', for: id, text: label }), input]);
  return { root, input, value: () => input.value };
}

function colorField(label, value) {
  const id = uid('sf');
  const swatch = el('input', { type: 'color', value: isHexColor(value) ? value : '#33526E', 'aria-label': `${label} swatch` });
  const text = el('input', { class: 'input', id, value: isHexColor(value) ? value.toUpperCase() : '#33526E', maxLength: 7 });
  swatch.addEventListener('input', () => {
    text.value = swatch.value.toUpperCase();
  });
  text.addEventListener('input', () => {
    if (isHexColor(text.value)) swatch.value = text.value;
    text.setAttribute('aria-invalid', String(!isHexColor(text.value)));
  });
  const root = el('div', { class: 'field' }, [
    el('label', { class: 'field__label', for: id, text: label }),
    el('div', { class: 'color-field' }, [swatch, text]),
    el('div', { class: 'field__hint', text: 'Format: #RRGGBB' }),
  ]);
  return { root, value: () => text.value.trim() };
}

function settingToggleRow(inputEl, label, description) {
  return el('div', { class: 'setting-row' }, [
    el('div', { class: 'setting-row__body' }, [
      el('div', { class: 'setting-row__label', text: label }),
      description ? el('div', { class: 'setting-row__desc', text: description }) : null,
    ]),
    el('label', { class: 'check setting-row__control' }, [inputEl, el('span', { class: 'check__text', text: 'Enabled' })]),
  ]);
}

const bytesToMb = (bytes) => Math.round((Number(bytes) || 0) / 1048576);
const mbToBytes = (mb) => Math.round((Number(mb) || 0) * 1048576);

export default SettingsController;
