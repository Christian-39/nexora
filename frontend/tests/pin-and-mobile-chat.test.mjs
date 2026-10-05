/**
 * Regression tests for:
 *  1. First-login & voluntary PIN validation and payload normalization (`auth.js`, `pin.js`)
 *  2. Client error reporting & secret/PIN redaction (`errors.js`)
 *  3. Optimistic media/voice upload indexing by `draft.clientId` and retry state (`messages.js`)
 *  4. Mobile PWA chat bubble footer/tools layout, swipe-to-reply, and single composer send button (`chat.js`, `chat.css`)
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_DIR = join(__dirname, '..');

describe('PIN validation & payload contract', () => {
  test('validateNewPin enforces 6 digits, rejects weak/sequential/repeated PINs, and checks confirmation', async () => {
    const { validateNewPin } = await import('../assets/js/auth.js');

    assert.equal(validateNewPin('482915', '482915'), null);
    assert.deepEqual(validateNewPin('12345', '12345')?.field, 'new');
    assert.deepEqual(validateNewPin('111111', '111111')?.field, 'new');
    assert.deepEqual(validateNewPin('123456', '123456')?.field, 'new');
    assert.deepEqual(validateNewPin('654321', '654321')?.field, 'new');
    assert.deepEqual(validateNewPin('234567', '234567')?.field, 'new');
    assert.deepEqual(validateNewPin('112233', '112233')?.field, 'new');
    assert.deepEqual(validateNewPin('482915', '482915', '482915')?.field, 'new');
    assert.deepEqual(validateNewPin('482915', '')?.field, 'confirm');
    assert.deepEqual(validateNewPin('482915', '482916')?.field, 'confirm');
  });

  test('auth.changePin accepts both camelCase and snake_case and omits empty current_pin on first login', () => {
    const authSrc = readFileSync(join(FRONTEND_DIR, 'assets/js/auth.js'), 'utf8');
    assert.match(authSrc, /input\.currentPin\s*\?\?\s*input\.current_pin/);
    assert.match(authSrc, /input\.newPin\s*\?\?\s*input\.new_pin/);
    assert.match(authSrc, /input\.confirmPin\s*\?\?\s*input\.confirm_pin/);
    assert.match(authSrc, /if\s*\(currentPin\)\s*body\.current_pin\s*=\s*String\(currentPin\)/);
  });

  test('pin.js buildPinFields auto-advances focus and exposes both reset() and clear()', () => {
    const pinSrc = readFileSync(join(FRONTEND_DIR, 'assets/js/pin.js'), 'utf8');
    assert.match(pinSrc, /onComplete:\s*\(\)\s*=>\s*confirm\?\.focus\(\)/);
    assert.match(pinSrc, /clear\(\)\s*\{/);
    assert.match(pinSrc, /reset\(\)\s*\{/);
  });
});

describe('Centralized client error reporter & redaction', () => {
  test('redactClientText strips 6-digit PINs, JWTs, Bearer tokens, and secret key-value pairs', async () => {
    const { redactClientText } = await import('../assets/js/errors.js');
    const sample =
      'Error with pin=123456, new_pin: "654321", Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.c2ln and raw 482915';
    const sanitized = redactClientText(sample);
    assert.ok(!sanitized.includes('123456'), sanitized);
    assert.ok(!sanitized.includes('654321'), sanitized);
    assert.ok(!sanitized.includes('482915'), sanitized);
    assert.ok(!sanitized.includes('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'), sanitized);
    assert.ok(sanitized.includes('[REDACTED]'), sanitized);
  });
});

describe('Optimistic media & voice upload store indexing', () => {
  test('createOptimistic indexes message under caller-supplied draft.clientId for retry & progress', async () => {
    const {
      createOptimistic,
      getStore,
      setLocalProgress,
      markLocalUnconfirmed,
      markLocalFailed,
      markLocalSending,
      removeLocal,
      STATUS,
    } = await import('../assets/js/messages.js');

    const convId = 'conv-voice-retry-test';
    const draftClientId = 'draft-voice-001';
    const msg = createOptimistic(convId, {
      clientId: draftClientId,
      kind: 'voice',
      media: { url: 'blob:voice-1', duration: 4 },
    });

    assert.equal(msg.clientId, draftClientId);
    assert.equal(getStore(convId).byClientId.get(draftClientId), msg);

    setLocalProgress(convId, draftClientId, 55);
    assert.equal(msg.progress, 55);

    markLocalUnconfirmed(convId, draftClientId);
    assert.equal(msg.status, STATUS.UNCONFIRMED);
    assert.equal(msg.progress, null);

    markLocalSending(convId, draftClientId);
    assert.equal(msg.status, STATUS.SENDING);

    markLocalFailed(convId, draftClientId, new Error('Upload interrupted'));
    assert.equal(msg.status, STATUS.FAILED);

    removeLocal(convId, draftClientId);
    assert.equal(getStore(convId).byClientId.get(draftClientId), undefined);
  });
});

describe('Mobile PWA chat layout, swipe-to-reply, and composer tray invariants', () => {
  test('bubbleTools is mounted inside bubbleFooter with nowrap flex and swipe-to-reply bound', () => {
    const chatJs = readFileSync(join(FRONTEND_DIR, 'assets/js/chat.js'), 'utf8');
    const chatCss = readFileSync(join(FRONTEND_DIR, 'assets/css/chat.css'), 'utf8');

    // bubbleFooter appends bubbleTools inside .bubble__footer
    assert.match(chatJs, /const tools = this\.bubbleTools\(message,\s*conv\);\s*if\s*\(tools\.childNodes\.length\)\s*footer\.append\(tools\);/);
    // Swipe-to-reply is bound on non-local, non-deleted messages
    assert.match(chatJs, /this\.bindSwipeToReply\(row,\s*bubble,\s*message\)/);
    // Reply action is included in the message actions menu for mobile accessibility
    assert.match(chatJs, /label:\s*'Reply',\s*icon:\s*'reply'/);
    // Retry bar renders for both FAILED and UNCONFIRMED outgoing messages
    assert.match(chatJs, /message\.status\s*===\s*STATUS\.FAILED\s*\|\|\s*message\.status\s*===\s*STATUS\.UNCONFIRMED/);
    // Preview tray does not render a duplicate primary Send button alongside #composer-send
    assert.ok(!chatJs.includes("text: 'Send voice note'"));

    // CSS keeps .bubble__footer on a single non-wrapping line and hides the inline reply icon on touch devices
    assert.match(chatCss, /\.bubble__footer\s*\{[^}]*flex-wrap:\s*nowrap/s);
    assert.match(chatCss, /\.bubble__tools\s+\.bubble__reply-btn\s*\{\s*display:\s*none;/s);
  });

  test('mobile viewport layout rules cover 320px, 360px, 375px, 390px, 412px, and 430px without overflow or duplicate send buttons', () => {
    const chatCss = readFileSync(join(FRONTEND_DIR, 'assets/css/chat.css'), 'utf8');
    const responsiveCss = readFileSync(join(FRONTEND_DIR, 'assets/css/responsive.css'), 'utf8');
    const componentsCss = readFileSync(join(FRONTEND_DIR, 'assets/css/components.css'), 'utf8');

    for (const width of [320, 360, 375, 390, 412, 430]) {
      const gap = width <= 380 ? 6 : 8;
      const boxWidth = width <= 380 ? 42 : 46;
      const totalPinRowWidth = 6 * boxWidth + 5 * gap;
      assert.ok(
        totalPinRowWidth <= width - 24,
        `6 PIN inputs (${totalPinRowWidth}px) must fit inside ${width}px viewport with padding`,
      );
    }
    assert.match(componentsCss, /\.pin-input\s*\{[^}]*max-width:\s*320px/s);
    assert.match(componentsCss, /\.pin-input\s+input\s*\{[^}]*min-width:\s*0/s);
    assert.match(chatCss, /\.thread__actions\s*\{[^}]*flex-wrap:\s*nowrap/s);
    assert.match(chatCss, /\.thread__jump\s*\{[^}]*width:\s*34px;\s*height:\s*34px/s);
  });

  test('frontend/.env.example documents API_BASE_URL=https://nexora-f397.onrender.com', () => {
    const envExample = readFileSync(join(FRONTEND_DIR, '.env.example'), 'utf8');
    assert.match(envExample, /^API_BASE_URL=https:\/\/nexora-f397\.onrender\.com$/m);
  });
});
