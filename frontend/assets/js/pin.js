/**
 * NEXORA — pin.js
 * Accessible six-digit PIN entry.
 *
 * Guarantees:
 *  - Values are masked on screen and never echoed back after submission.
 *  - Nothing is written to storage, the URL, or the console.
 *  - reset() overwrites the field values so they do not linger in the DOM.
 */

import { el, uid } from './utils.js';

const LENGTH = 6;

/**
 * A single six-digit input group.
 * @param {object} options { label, name, autocomplete, describedBy }
 */
export function buildPinInput({ label = 'PIN', autocomplete = 'off', required = true, onComplete = null } = {}) {
  const groupId = uid('pin');
  const errorId = `${groupId}-error`;

  const group = el('div', {
    class: 'pin-input',
    role: 'group',
    'aria-label': `${label}, six digits`,
    'aria-describedby': errorId,
  });

  /** @type {HTMLInputElement[]} */
  const cells = [];

  for (let i = 0; i < LENGTH; i += 1) {
    const input = el('input', {
      type: 'text',
      inputMode: 'numeric',
      pattern: '[0-9]*',
      maxLength: 1,
      autocomplete: i === 0 ? autocomplete : 'off',
      'aria-label': `${label} digit ${i + 1} of ${LENGTH}`,
      required,
      spellcheck: false,
      autocapitalize: 'off',
      autocorrect: 'off',
    });

    input.addEventListener('input', () => {
      const digits = input.value.replace(/\D/g, '');
      if (digits.length > 1) {
        // A paste or an autofill landed in one cell — distribute it.
        distribute(digits, i);
        return;
      }
      input.value = digits;
      clearError();
      if (digits && i < LENGTH - 1) cells[i + 1].focus();
      notifyComplete();
    });

    input.addEventListener('keydown', (event) => {
      if (event.key === 'Backspace' && !input.value && i > 0) {
        event.preventDefault();
        cells[i - 1].value = '';
        cells[i - 1].focus();
      } else if (event.key === 'ArrowLeft' && i > 0) {
        event.preventDefault();
        cells[i - 1].focus();
      } else if (event.key === 'ArrowRight' && i < LENGTH - 1) {
        event.preventDefault();
        cells[i + 1].focus();
      }
    });

    input.addEventListener('paste', (event) => {
      const text = (event.clipboardData?.getData('text') || '').replace(/\D/g, '');
      if (!text) return;
      event.preventDefault();
      distribute(text, i);
    });

    input.addEventListener('focus', () => input.select());

    cells.push(input);
    group.append(input);
  }

  function distribute(digits, startIndex) {
    for (let k = 0; k < digits.length && startIndex + k < LENGTH; k += 1) {
      cells[startIndex + k].value = digits[k];
    }
    const nextEmpty = cells.findIndex((c) => !c.value);
    (nextEmpty === -1 ? cells[LENGTH - 1] : cells[nextEmpty]).focus();
    clearError();
    notifyComplete();
  }

  let completionQueued = false;
  function notifyComplete() {
    if (typeof onComplete !== 'function' || completionQueued) return;
    const value = cells.map((cell) => cell.value).join('');
    if (!/^\d{6}$/.test(value)) return;
    completionQueued = true;
    queueMicrotask(() => {
      completionQueued = false;
      // Re-check because the user can press Backspace before the microtask.
      const current = cells.map((cell) => cell.value).join('');
      if (/^\d{6}$/.test(current)) onComplete(current);
    });
  }

  const errorEl = el('div', { class: 'field__error', id: errorId, role: 'alert' });

  function clearError() {
    if (errorEl.textContent) {
      errorEl.textContent = '';
      group.dataset.invalid = 'false';
    }
  }

  return {
    group,
    errorEl,
    focus: () => cells[0].focus(),
    value: () => cells.map((c) => c.value).join(''),
    reset() {
      for (const cell of cells) cell.value = '';
      clearError();
    },
    setError(message) {
      errorEl.textContent = message || '';
      group.dataset.invalid = message ? 'true' : 'false';
      if (message) cells[0].focus();
    },
    setDisabled(disabled) {
      for (const cell of cells) cell.disabled = !!disabled;
    },
  };
}

/**
 * A complete credential-change field set.
 * @param {object} options { includeCurrent, onSubmit }
 */
export function buildPinFields({ includeCurrent = true, onSubmit = null } = {}) {
  let next;
  let confirm;
  const current = includeCurrent
    ? buildPinInput({
        label: 'Current PIN',
        autocomplete: 'current-password',
        onComplete: () => next?.focus(),
      })
    : null;
  next = buildPinInput({
    label: 'New PIN',
    autocomplete: 'new-password',
    onComplete: () => confirm?.focus(),
  });
  confirm = buildPinInput({
    label: 'Confirm new PIN',
    autocomplete: 'new-password',
    onComplete: () => onSubmit?.(),
  });

  const root = el('div', { class: 'stack' }, [
    current
      ? el('div', { class: 'field' }, [
          el('span', { class: 'field__label', id: 'pin-current-label', text: 'Current PIN' }),
          current.group,
          current.errorEl,
        ])
      : null,
    el('div', { class: 'field' }, [
      el('span', { class: 'field__label', text: 'New PIN' }),
      next.group,
      el('div', { class: 'field__hint', text: 'Exactly six digits. Avoid repeated digits and simple sequences.' }),
      next.errorEl,
    ]),
    el('div', { class: 'field' }, [
      el('span', { class: 'field__label', text: 'Confirm new PIN' }),
      confirm.group,
      confirm.errorEl,
    ]),
  ]);

  return {
    root,
    values: () => ({
      currentPin: current ? current.value() : null,
      newPin: next.value(),
      confirmPin: confirm.value(),
    }),
    setError(field, message) {
      if (field === 'current' && current) current.setError(message);
      else if (field === 'confirm') confirm.setError(message);
      else next.setError(message);
    },
    clearErrors() {
      current?.setError('');
      next.setError('');
      confirm.setError('');
    },
    reset() {
      current?.reset();
      next.reset();
      confirm.reset();
    },
    clear() {
      current?.reset();
      next.reset();
      confirm.reset();
    },
    setDisabled(disabled) {
      current?.setDisabled(disabled);
      next.setDisabled(disabled);
      confirm.setDisabled(disabled);
    },
    focus() {
      (current || next).focus();
    },
  };
}

export default { buildPinInput, buildPinFields };
