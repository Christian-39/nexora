/**
 * Member form contract tests (pure functions — no DOM).
 *
 *   node --test tests/members-form.test.mjs
 *
 * Guards the frontend↔backend member-creation contract:
 *   display_name | phone | email | is_active
 * and the error mapping that previously swallowed the backend's `full_name`
 * validation error, which is why a failed creation looked like nothing.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

/* ---- minimal browser environment (members.js transitively imports api.js,
   which binds window listeners at module scope) ---- */
function installEnvironment() {
  const listeners = new Map();
  const target = {
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    removeEventListener() {},
  };
  globalThis.location = {
    hostname: 'nexora-eight-lilac.vercel.app',
    port: '',
    protocol: 'https:',
    origin: 'https://nexora-eight-lilac.vercel.app',
  };
  globalThis.window = Object.assign(target, { location: globalThis.location });
  globalThis.document = {
    visibilityState: 'visible',
    addEventListener: target.addEventListener,
    removeEventListener() {},
    querySelector: () => null,
    getElementById: () => null,
    createElement: () => ({ style: {}, setAttribute() {}, append() {}, addEventListener() {} }),
  };
  globalThis.navigator = { onLine: true };
  globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
}

installEnvironment();
const members = await import(new URL('../assets/js/members.js', import.meta.url).href);

/* ---------------------------------------------------------- payload ---- */

test('valid member builds the canonical create payload', () => {
  const { payload, problems } = members.collectMemberPayload({
    displayName: 'Ada Obi',
    phone: '+234 801 234 5678',
    email: 'ada@example.com',
    isActive: true,
  });
  assert.deepEqual(problems, {});
  assert.deepEqual(payload, {
    display_name: 'Ada Obi',
    phone: '+234 801 234 5678',
    email: 'ada@example.com',
    is_active: true,
  });
});

test('email is optional and omitted when blank', () => {
  const { payload } = members.collectMemberPayload({ displayName: 'Ada', phone: '+2348012345678' });
  assert.equal(payload.email, undefined);
  assert.equal(payload.is_active, true);
});

test('create sends is_active; edit does not (dedicated activate/deactivate endpoints)', () => {
  const created = members.collectMemberPayload({ displayName: 'A', phone: '+2348012345678', isActive: false });
  assert.equal(created.payload.is_active, false);
  const edited = members.collectMemberPayload({ displayName: 'A', phone: '+2348012345678', isActive: false }, { isEdit: true });
  assert.equal(edited.payload.is_active, undefined);
});

test('missing name and phone are client-side validation problems', () => {
  const { payload, problems } = members.collectMemberPayload({ displayName: '  ', phone: '' });
  assert.equal(payload, null);
  assert.equal(problems.displayName, 'Enter a display name.');
  assert.equal(problems.phone, 'Enter a phone number.');
});

test('a malformed phone is rejected before the network', () => {
  const { payload, problems } = members.collectMemberPayload({ displayName: 'A', phone: 'nope' });
  assert.equal(payload, null);
  assert.match(problems.phone, /valid phone number/);
});

test('a malformed email is rejected before the network', () => {
  const { payload, problems } = members.collectMemberPayload({ displayName: 'A', phone: '+2348012345678', email: 'not-an-email' });
  assert.equal(payload, null);
  assert.match(problems.email, /valid email/);
});

test('the payload never contains login_identifier — it is not part of the contract', () => {
  const { payload } = members.collectMemberPayload({ displayName: 'A', phone: '+2348012345678', loginIdentifier: 'zzz' });
  assert.equal('login_identifier' in payload, false);
});

/* ------------------------------------------------------- error mapping ---- */

test('a backend full_name error is shown on the name field (the old silent-failure bug)', () => {
  const mapped = members.mapMemberErrors({
    errors: { full_name: ['This field is required.'] },
    message: 'Some of the information provided is not valid.',
  });
  assert.equal(mapped.displayName, 'This field is required.');
  assert.ok(mapped.form, 'the form-level message is still present');
});

test('display_name and phone errors map to their inputs', () => {
  const mapped = members.mapMemberErrors({
    errors: { display_name: ['A display name is required.'], phone: ['Enter a valid international phone number.'] },
    message: '…',
  });
  assert.equal(mapped.displayName, 'A display name is required.');
  assert.match(mapped.phone, /valid international phone/);
});

test('duplicate phone surfaces on the phone field', () => {
  const mapped = members.mapMemberErrors({
    errors: { phone: ['A user with that phone number already exists.'] },
    message: '…',
  });
  assert.match(mapped.phone, /already exists/);
});

test('email errors map to the email field', () => {
  const mapped = members.mapMemberErrors({ errors: { email: ['Enter a valid email address.'] } });
  assert.match(mapped.email, /valid email/);
});

test('non-field errors are surfaced at form level, never swallowed', () => {
  const mapped = members.mapMemberErrors({
    errors: { non_field_errors: ['Something conflicted.'] },
    message: 'This action conflicts with the current state.',
  });
  assert.equal(mapped.form, 'Something conflicted.');
});

test('an authorization failure (403) becomes a visible form message', () => {
  const mapped = members.mapMemberErrors({ errors: {}, message: 'You are not authorized to perform this action.' });
  assert.equal(mapped.form, 'You are not authorized to perform this action.');
});

test('a transport failure with no body still produces a form message', () => {
  const mapped = members.mapMemberErrors({ message: 'The request timed out. Your connection may be unstable.' });
  assert.match(mapped.form, /timed out/);
});
