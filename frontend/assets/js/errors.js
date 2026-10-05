/**
 * NEXORA — errors.js
 * Centralized frontend error capture and redaction.
 *
 * Forwards uncaught runtime exceptions, unhandled promise rejections, and
 * unexpected API / WebSocket failures to the backend sink (`api.clientErrors.report`)
 * so they appear in Render logs alongside `nexora.client` records.
 *
 * Security invariants:
 *  - Never logs or transmits PINs, passwords, tokens, cookies, or message bodies.
 *  - Bounded per-tab rate limit and signature deduplication prevent log floods.
 *  - Reporting failures are swallowed so telemetry never breaks the UI.
 */

import { ApiError, api } from './api.js';

const MAX_REPORTS_PER_PAGE = 10;
const DEDUPE_TTL_MS = 60_000;

let installed = false;
let reportCount = 0;
const seenSignatures = new Map();

const SECRET_KV_RE =
  /\b(pin|current_pin|new_pin|confirm_pin|old_pin|password|passwd|secret|token|access|refresh|cookie|csrftoken|authorization)\b(\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,&;}\]]+)/gi;
const JWT_RE = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g;
const BEARER_RE = /\bBearer\s+[A-Za-z0-9._~+/=-]+/gi;
const SIX_DIGIT_PIN_RE = /\b\d{6}\b/g;

/**
 * Redact credentials, tokens, and 6-digit PINs from any string before it leaves
 * the browser.
 * @param {unknown} value
 * @param {number} [limit]
 * @returns {string}
 */
export function redactClientText(value, limit = 500) {
  if (value === null || value === undefined) return '';
  let text = String(value);
  text = text.replace(SECRET_KV_RE, '$1$2[REDACTED]');
  text = text.replace(JWT_RE, '[REDACTED_JWT]');
  text = text.replace(BEARER_RE, 'Bearer [REDACTED]');
  text = text.replace(SIX_DIGIT_PIN_RE, '[REDACTED_PIN]');
  return text.slice(0, limit);
}

function safePageUrl() {
  if (typeof window === 'undefined' || !window.location) return '';
  // Strip query parameters/fragments that could carry user-supplied tokens.
  return `${window.location.origin}${window.location.pathname}`.slice(0, 300);
}

function shouldReportSignature(sig) {
  const now = Date.now();
  const prev = seenSignatures.get(sig);
  if (prev && now - prev < DEDUPE_TTL_MS) return false;
  seenSignatures.set(sig, now);
  return true;
}

/**
 * Send one sanitized client error report to the backend sink.
 * @param {object} entry
 * @param {string} [entry.kind]
 * @param {string} [entry.message]
 * @param {string} [entry.stack]
 * @param {string} [entry.url]
 * @param {number|string} [entry.status]
 * @param {string} [entry.code]
 * @param {string} [entry.endpoint]
 */
export async function reportClientError(entry = {}) {
  if (reportCount >= MAX_REPORTS_PER_PAGE) return false;
  const kind = redactClientText(entry.kind || 'error', 40) || 'error';
  const message = redactClientText(entry.message || 'Unhandled client error', 500);
  const stack = redactClientText(entry.stack || '', 3000);
  const endpoint = redactClientText(entry.endpoint || '', 200);
  const code = redactClientText(entry.code || '', 60);
  const status = entry.status !== undefined && entry.status !== null ? Number(entry.status) || '' : '';
  const url = redactClientText(entry.url || safePageUrl(), 300);

  const signature = `${kind}:${code}:${status}:${endpoint}:${message.slice(0, 120)}`;
  if (!shouldReportSignature(signature)) return false;

  reportCount += 1;
  try {
    await api.clientErrors.report({
      kind,
      message,
      stack,
      url,
      status,
      code,
      endpoint,
    });
    return true;
  } catch {
    return false;
  }
}

/**
 * Install global window error and unhandledrejection listeners (idempotent).
 */
export function installGlobalErrorHandlers() {
  if (installed || typeof window === 'undefined') return;
  installed = true;

  window.addEventListener('error', (event) => {
    const err = event?.error;
    const message = err?.message || event?.message || 'Uncaught script error';
    const stack = err?.stack || `${event?.filename || ''}:${event?.lineno || 0}:${event?.colno || 0}`;
    reportClientError({
      kind: 'window.error',
      message,
      stack,
    });
  });

  window.addEventListener('unhandledrejection', (event) => {
    const reason = event?.reason;
    if (reason instanceof ApiError) {
      // Expected auth/validation/cancel outcomes are handled by the UI and do
      // not represent crashes; only report 5xx server failures.
      if (reason.isAborted || reason.isAuth || reason.isValidation || reason.status < 500) {
        return;
      }
      reportClientError({
        kind: 'api.unhandled',
        message: reason.message,
        code: reason.code,
        status: reason.status,
        stack: reason.stack || '',
      });
      return;
    }
    const message = reason?.message || String(reason || 'Unhandled promise rejection');
    const stack = reason?.stack || '';
    reportClientError({
      kind: 'unhandledrejection',
      message,
      stack,
    });
  });
}

export function __resetErrorReporterForTests() {
  reportCount = 0;
  seenSignatures.clear();
}

if (typeof window !== 'undefined') {
  installGlobalErrorHandlers();
}

export default {
  redactClientText,
  reportClientError,
  installGlobalErrorHandlers,
};
