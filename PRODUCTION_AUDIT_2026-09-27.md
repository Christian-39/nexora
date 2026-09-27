# NEXORA production audit and fix report

**Audit date:** 2026-09-27 (Africa/Lagos)  
**Source audited:** `Christian-39/nexora` at `4d1059a`  
**Production:** Vercel frontend + Render/Django ASGI backend

## Executive result

This pass found four new high-impact causes behind the reported experience, in addition to the fixes already present at current HEAD:

1. The installed PWA still put a network request in front of every navigation and every JS/CSS module, despite having a complete precache.
2. A successful login always made an unnecessary second blocking `/api/me/` request because the frontend did not recognise the backend's direct profile response shape.
3. The manifest launched through a network-dependent session-check page and the login page did not install the service worker.
4. Android's dynamic viewport could remain at its keyboard-resized height, producing the detached composer and large blank area shown in the supplied screenshot.

The changes make the installed shell cache-first and version-coherent, make the login UI immediately interactive, add sixth-digit PIN submission with duplicate protection, avoid the redundant post-login request, restore safe presentation state between HTML documents, preserve unread display state, correct mobile composer sizing, and accept documented Nigerian local phone inputs.

No authentication secret, PIN, token, cookie, private message, or private API response is persisted in Cache Storage/localStorage. The HttpOnly cookie and backend remain authoritative.

## Root causes and fixes

### PWA startup and navigation

**Problem:** Warm installed launches and page transitions could still wait for Vercel/network.  
**Root cause:** `sw.js` precached the full application but used network-first for navigations and application code. Navigation preload also started a request even when a shell document existed locally.  
**Fix:** Version `v1.4.0` serves known HTML documents and JS/CSS/manifest from one coherent versioned precache. A new worker downloads a complete new release during install and activates only through the existing update prompt. Unknown routes retain a network/offline fallback. Navigation preload is disabled. `/api/*` remains network-only.  
**Files:** `frontend/sw.js`, `frontend/tests/pwa-performance.test.mjs`.

### Login critical path

**Problem:** Sign-in remained slow after valid credentials.  
**Root cause:** Django returns the profile directly in the login envelope's `data`. `auth.login()` only recognised `{user: ...}` or `{me: ...}`, treated the direct profile as absent, then blocked on `/api/me/`.  
**Fix:** Recognise a direct profile (`payload.id`) and reuse it. `/api/me/` remains a fallback for compatibility only.  
**File:** `frontend/assets/js/auth.js`.

### Login readiness, PWA installation, and auto-submit

**Problem:** The login route started `/api/me/` immediately, service-worker installation only began after authentication on application pages, and six PIN digits did not submit.  
**Root cause:** Session discovery ran eagerly; login did not register the worker; the PIN component exposed no completion event.  
**Fix:** Focus and enable the form before any backend operation; move session discovery to idle/background work and cancel it when login starts; register the worker on login/index; add completion notification to the accessible PIN component; call `form.requestSubmit()` on the sixth digit; guard the handler with `loginInFlight`; preserve the manual button. Input, rapid typing, paste/autofill distribution, backspace, and corrections continue to use the shared PIN component.  
**Files:** `frontend/login.html`, `frontend/index.html`, `frontend/assets/js/pin.js`, `frontend/manifest.webmanifest`.

### Login error classification

**Problem:** A CSRF 403 could be displayed as an inactive-account failure, and 5xx errors were not given a specific temporary-service message.  
**Root cause:** The login error branch grouped every 403 with account inactivity.  
**Fix:** `ACCOUNT_INACTIVE` remains account-specific; a generic 403 reports secure-request verification failure; transport/timeouts and server failures have separate safe messages. Invalid credentials remain generic to prevent enumeration.  
**File:** `frontend/login.html`.

### Safe multi-page presentation state

**Problem:** Role/name/avatar shell state disappeared across HTML navigation, and unread badges reset before reconciliation.  
**Root cause:** Both were memory-only.  
**Fix:** Store a minimal presentation-only profile and unread snapshot in `sessionStorage`, restore them synchronously, revalidate through `/api/me/` and unread APIs, and clear them on logout. Stored profile fields exclude phone, email, PIN, tokens, cookies and authentication secrets. `sessionStorage` limits state to the current tab/session rather than sharing it indefinitely.  
**Files:** `frontend/assets/js/auth.js`, `frontend/assets/js/notifications.js`.

### Detached mobile composer

**Problem:** After Android keyboard transitions the composer could sit above a large blank region.  
**Root cause:** The shell depended only on `100dvh`; affected Android Chrome/PWA lifecycle states can leave that unit at a stale keyboard-resized height. The flex/grid composer structure itself was otherwise correct.  
**Fix:** Mirror the authoritative `VisualViewport.height` into `--app-viewport-height` on viewport resize/scroll and window resize. The body/shell use that dynamic value with `100dvh` fallback. No device-specific fixed height or global overflow hack was added.  
**Files:** `frontend/assets/js/navigation.js`, `frontend/assets/css/main.css`.

### Message wrapping

**Problem:** `overflow-wrap:anywhere` participated in intrinsic bubble sizing and could make ordinary text wrap too aggressively.  
**Fix:** Normal message/caption bubbles now use `overflow-wrap:break-word` plus `word-break:normal`; links alone retain `anywhere` so long URLs break safely. Long unbroken strings still cannot overflow.  
**File:** `frontend/assets/css/chat.css`.

### Member phone and initial PIN flow

**Problem:** Member creation/login rejected Nigerian local numbers such as `080…`, although the required test matrix includes them.  
**Root cause:** `phonenumbers.parse(..., None)` only accepted explicit international input.  
**Fix:** Inputs without `+` use the Nigerian product region; explicit international numbers retain their country. All identities are still stored as E.164. Existing repository documentation/tests define the initial PIN as the first six digits of canonical E.164 (for `+234801…`, `234801`); that established rule was retained rather than guessed or silently changed. Creation still hashes the PIN and sets `INITIAL`; existing tests enforce forced change and session revocation.  
**Files:** `backend/apps/accounts/services.py`, `backend/tests/test_member_contract.py`, `backend/tests/test_security.py`.

## Existing fixes verified and intentionally preserved

Current HEAD already contains: centralized API in-flight GET deduplication; CSRF bootstrap deduplication; backend query batching for conversation latest-message/presence data; unread refresh deduplication; optimistic/idempotent text and media sending; bounded single-socket reconnect lifecycle; delayed reconnect UX; media retry retention; dashboard contract repair; member form error handling; bottom navigation restoration; shell-before-session mounting; safe private-media handling; DB connection reuse; receipt indexes; ASGI/Redis configuration checks; push subscription/error paths.

## Measurements

### Production measurements taken during this audit

| Path | Observed result |
|---|---:|
| Vercel `/` | 0.436 s total |
| Vercel `/login.html` | 0.348 s total |
| Render first observed request after idle/deploy (`/api/health/`, invalid path) | 25.305 s to first byte |
| Render `/health/live/` warm, 3 samples | 0.105–0.163 s |
| Render `/health/ready/` warm, 3 samples | 1.795–2.130 s |
| Render `/api/public/config/` | 2.063 s first sample; 0.083–0.113 s subsequent |
| Render `/api/auth/csrf/` after wake | 2.791 s |

The 25.3 s outlier and multi-second first API/config response are upstream/backend wake or external-service readiness costs, not frontend rendering. The new service worker prevents those costs from blocking an already-installed application shell. Actual authenticated production endpoint timings could not be measured without credentials.

### Expected code-path reduction

* Warm PWA shell: network-first navigation + module revalidation → local Cache Storage lookup for known shell documents/code.
* Successful login: CSRF + login + `/api/me/` → CSRF + login (the login response already contains the profile).
* Login UI readiness: immediate DOM/module setup; `/api/me/` is idle/background and never gates typing.

Browser Performance API/mobile-device before/after numbers are not claimed because no real Android browser automation or production credentials were available in this environment.

## Files changed

| File | Purpose |
|---|---|
| `frontend/sw.js` | Versioned cache-first application shell/code; disable wasteful navigation preload; preserve network-only APIs. |
| `frontend/manifest.webmanifest` | Launch the immediately interactive login shell. |
| `frontend/index.html` | Register SW and route from safe cached profile without blocking startup. |
| `frontend/login.html` | Immediate readiness, SW registration, idle session probe, auto-submit lock, precise errors. |
| `frontend/assets/js/pin.js` | Completion callback supporting digit entry, paste and autofill. |
| `frontend/assets/js/auth.js` | Safe session profile snapshot; remove redundant post-login `/api/me/`. |
| `frontend/assets/js/notifications.js` | Immediate session-scoped unread snapshot with background authority. |
| `frontend/assets/js/navigation.js` | VisualViewport-based mobile shell sizing. |
| `frontend/assets/css/main.css` | Consume dynamic viewport height with `100dvh` fallback. |
| `frontend/assets/css/chat.css` | Natural message wrapping and safe URL breaking. |
| `backend/apps/accounts/services.py` | Nigerian local-number normalization while retaining canonical E.164/PIN rule. |
| `backend/tests/test_member_contract.py` | Local, formatted, E.164 and international normalization cases. |
| `backend/tests/test_security.py` | Local-input initial-PIN/hash/state regression case. |
| `frontend/tests/pwa-performance.test.mjs` | PWA, login critical-path and wrapping regression guards. |

## Verification performed

| Check | Result |
|---|---|
| Frontend Node tests | **58 passed, 0 failed** |
| Backend pytest suite (SQLite test environment) | **155 passed, 0 failed** |
| Django `manage.py check` | **Passed** |
| `makemigrations --check --dry-run` | **No changes detected** |
| JS/service-worker syntax (`node --check`) | **Passed** |
| Manifest JSON parse | **Passed** |
| Git whitespace check | **Passed** |
| Static production routes served locally | **Passed** for login, chat, SW, manifest, auth JS and chat CSS |
| Live public production probes | **Passed** for liveness, readiness, CSRF and public config |

Not executed and not claimed: authenticated production login (no credentials), real production WebSocket 101 with authenticated cookies, actual Android push delivery, installed-PWA keyboard testing on physical devices, attachment/voice/video upload against private production storage, and the full requested physical-device viewport matrix.

## Remaining external/deployment actions

### Push is disabled by deployment configuration

Production `/api/public/config/` currently reports:

```json
{"features":{"push":false},"push":{"vapid_public_key":null},"vapid_public_key":null}
```

This exactly explains the screenshot. Source code must not fake enabled state. Configure the following on the Render web service **and push worker**, then redeploy both:

* `PUSH_PUBLIC_KEY=<URL-safe VAPID public key>`
* `PUSH_PRIVATE_KEY=<matching VAPID private key>`
* `PUSH_CONTACT=mailto:<operational email>`

Also ensure the database platform setting `push_enabled` is true and the `nexora-push-worker` is running with the same database/Redis/environment. Then verify permission, `PushManager.subscribe()`, `/api/push/subscribe/`, a queued delivery, worker send, and receipt on a real Android device.

### Backend first-request latency

The production backend showed a 25.3 s first response and 2+ s first config/readiness operations, while subsequent ordinary requests were about 0.08–0.16 s. Confirm in Render metrics that the web service is always-on and not restarting; inspect deploy/restart events, CPU/memory, MySQL region/connection TLS latency, and Redis region. Keep Render, MySQL and Redis in the same region. `DATABASE_CONN_MAX_AGE=300` is already implemented; do not reduce it. If the current service can idle, use an always-on paid instance/minimum instance count. Frontend caching can hide shell latency but cannot make an authoritative login complete while the backend is asleep.

### Deployment required

These source changes are local workspace changes and are not live until committed/pushed and both Vercel (frontend) and Render (backend phone normalization) deploy the new revision. After deployment, accept the `v1.4.0` service-worker update prompt once; subsequent installed launches use the new cache-first shell.
