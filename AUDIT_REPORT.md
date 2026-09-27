# NEXORA — Production Audit & Root‑Cause Fix Report

**Branch:** `fix/production-audit`
**Scope of this pass (agreed triage):** the six highest‑impact, well‑diagnosed
root causes — auth logout regression, shell/topbar flicker, mobile bottom‑nav
regression, dashboard staleness, member‑creation feedback, media/voice retry —
plus the deployment/URL contract cleanup they surfaced. Delivered as a git
branch + patch. All changes are surgical; the vanilla‑JS ES‑module + Django
Channels architecture is untouched.

> **Verification honesty:** every result below was executed locally. What was
> **NOT** executed: anything requiring the live Vercel/Render deployment, the
> production MySQL/Redis, real object storage, a real browser, or a real mobile
> device (no push access, no credentials). Those items are marked **NOT
> EXECUTED** and reasoned about from the code, not claimed as passing.

## Test results (executed)

| Suite | Command | Before | After |
|---|---|---|---|
| Backend | `pytest` (SQLite in‑memory, PyMySQL fallback) | 152 pass / **1 fail** | **155 pass / 0 fail** |
| Django checks | `manage.py check` | clean | clean |
| Migrations | `makemigrations --check` | no drift | **no drift** (no schema change) |
| Frontend | `node --test frontend/tests/` | 48 pass / **5 fail** | **53 pass / 0 fail** |

The 6 baseline failures were all caused by an **obsolete backend URL**
(`nexora-backend-ptsc.onrender.com`) hard‑coded in test/deploy files while the
app already used the correct `nexora-f397.onrender.com`. A failing contract test
can block CI/auto‑deploy, so this was a real (not cosmetic) finding.

---

## Root causes & fixes

### 1. Authentication regression — false logout on a temporary backend outage
- **Symptom:** logged in, then logged out "for no reason."
- **Root cause (frontend):** `auth.js › bootstrap()` returns `null` for BOTH a
  real `401` and a network/timeout/5xx failure. `requireSession()` treated any
  `null` as "no session" → `redirectToLogin()`. On Render free‑tier cold starts
  (`/api/me/` times out), a **valid** session was thrown to the sign‑in screen.
- **Fix:** `bootstrap()` now records `sessionUnverified` when the failure is
  connectivity‑only (`isNetwork/isOffline/isTimeout/isServer`) vs a real `401`.
  `requireSession()` only redirects on a **confirmed** auth failure; on
  connectivity failure it keeps the (already‑mounted) shell and emits
  `session-unverified`. Backend authorization is unchanged and still
  authoritative. *(api.js already had correct 401→single‑refresh→retry, refresh
  de‑dup, and network classification — left intact.)*
- **Files:** `frontend/assets/js/auth.js`.

### 2. Application shell / topbar / sidebar disappears then reappears
- **Symptom:** navigate → chrome vanishes for seconds → returns.
- **Root cause (frontend):** every authenticated page did
  `await requireSession()` **before** `mountNavigation()`. The shell literally
  did not exist until `/api/me/` resolved — the "WAIT FOR SESSION → MOUNT SHELL"
  anti‑pattern the brief describes.
- **Fix:** reordered all six pages to **mount the shell first**
  (`mountNavigation` + `mountConnectionBanner` + `mountSessionGuards`), paint
  cached branding, then validate the session in the background and load page
  data. The nav renders as a guest and **reconciles** when `authEvents 'user'`
  fires (that subscription already existed). Combined with fix #1, a slow/cold
  backend no longer blanks or bounces the UI.
- **Files:** `admin.html`, `chat.html`, `members.html`, `groups.html`,
  `settings.html`, `profile.html`.

### 3. Sidebar logo / branding regression
- **Symptom:** org name/logo render empty in the sidebar/header.
- **Root cause (lifecycle):** exactly the brief's hypothesis —
  `loadBranding()`→`applyBranding()` queried `[data-brand="logo"]/[org-name]`
  **before** `mountNavigation()` created those nodes. When the branding cache was
  "fresh" (<5 min) the network revalidation path returned early and never
  re‑applied, so the freshly‑created slots stayed blank.
- **Fix:** `navigation.js` repaints branding into the shell nodes right after it
  creates them (`render`, `renderHeader`, `openDrawer`) via a new
  `applyBranding(config, { sideEffects: false })` mode in `theme.js` that
  repaints names/logo/colours **without** re‑fetching the manifest or
  cache‑busting the favicon on every re‑render. Public‑config caching &
  background revalidation are preserved; private data is never cached as public.
- **Files:** `frontend/assets/js/theme.js`, `frontend/assets/js/navigation.js`.

### 4. Mobile bottom navigation regression
- **Symptom:** the fixed bottom nav bar disappeared after the latest deploy.
- **Root cause (regression, confirmed via git):** the first commit rendered
  `.app-nav` as a **fixed bottom bar** at ≤767px ("Primary nav becomes a bottom
  bar"). Commit **`afc2d79 "updated"`** replaced it with
  `.app-nav { display:none }` + a header hamburger + drawer.
- **Fix:** restored the original bottom‑bar CSS (nav reflows to `grid-row: 2`,
  horizontal `flex`, icon+label items, `env(safe-area-inset-bottom)`, hidden only
  in full‑screen thread view) and hid the now‑redundant hamburger. **No hamburger
  or new drawer is introduced as the replacement** — existing icons/labels/links
  /active states are preserved. Because the bar is a normal grid row inside a
  `100dvh` shell, it stays pinned above the Android keyboard without
  `position:fixed`/`100vh` hacks. *(The legacy drawer code remains but is
  unreachable — smallest safe change; fully reversible.)*
- **Files:** `frontend/assets/css/responsive.css`, `navigation.js` (doc), and the
  contract test `test_frontend_contract.py` (it previously asserted the *buggy*
  hamburger layout — updated to assert the restored bottom bar).

### 5. Dashboard data not updating
- **Symptom:** create member / send message, dashboard doesn't reflect it.
- **Root cause (frontend↔backend contract mismatch, NOT cache):** the backend
  returned a **nested** payload (`members.total`, `messages.last_7_days`,
  `conversations.groups` …) but `admin.html` read **flat** keys
  (`total_members`, `messages_today`, `media_today` …). No key matched → every
  metric rendered `—`/`0` regardless of DB changes. A full refresh could never
  fix it. There is **no cache** on `/api/dashboard/`, so it was pure contract.
- **Fix:** the dashboard view now returns the flat, correctly‑scoped keys the UI
  reads **alongside** the nested structure (kept because a test asserts it).
  "Today" is computed on the deployment timezone's calendar day; all message
  counters (all‑time / 7‑day / today / media‑today / voice‑today) are folded into
  a **single aggregate query**; "unread conversations" counts **distinct**
  conversations. No hard‑coded values, no fabricated realtime. The dashboard
  refetches on navigation, the Refresh button, and socket‑open (all pre‑existing).
- **Files:** `backend/apps/accounts/views.py` (+ test coverage in
  `test_settings_and_push.py`).

### 6. Security events — real database pagination (exactly 10/page)
- **Symptom:** widget showed 8; brief requires exactly 10 with prev/next.
- **Root cause:** `admin.html` requested `{ limit: 8 }` — `limit` isn't even the
  cursor param (`cursor`/`page_size` are), so it was a client‑side cap over a
  30‑row default page: the "SELECT‑all then slice in JS" anti‑pattern.
- **Fix (DB‑level):** dedicated `SecurityEventPagination(CursorPagination)` with
  `page_size = 10`, ordered by the indexed `-created_at` (no new index needed —
  `SecurityEvent.created_at` is already `db_index=True`). Frontend now requests
  `page_size: 10` and renders working **Previous/Next** (disabled states, follows
  opaque cursors, no full‑page reload).
- **Files:** `backend/apps/security/views.py`, `frontend/admin.html` (+ tests in
  `test_security.py`).

### 7. Attachment / voice upload retry (confirmed bug)
- **Symptom:** upload fails → "Re‑attach the file to send it again." → file lost.
- **Root cause (frontend resource lifecycle):**
  1. `sendMedia()` called `this.clearDraft()` — which **disposes** the draft
     (revokes the preview object URL) — on the *same* draft it was uploading and
     the optimistic bubble was still showing.
  2. `finally { draft.dispose() }` destroyed the `File/Blob` on **every** outcome,
     including failure, so retry was impossible.
  3. `retryMessage()` for media just told the user to re‑attach and removed it.
  *(The plumbing was already correct: `uploadDraft` reuses the same `client_id`,
  guards concurrent uploads, and the backend dedupes on `client_id`.)*
- **Fix:** failed/unconfirmed media drafts are retained in a `pendingMedia`
  Map (keyed by `client_id`) holding the `File/Blob`, MIME/name/size, preview and
  poster URL. `detachDraft()` clears the composer tray **without** disposing the
  in‑flight resource. `retryMessage()` re‑sends the **same** draft with the
  **same `client_id`** — no reselection, no re‑recording, and the backend dedupe
  guarantees no duplicate server message (also safe for the "response‑lost then
  retry" and "success then WebSocket event then retry" races). Resources are
  disposed exactly once — on confirm, abort, explicit discard, or controller
  destroy — including the video poster object URL (fixes a leak). Optimistic
  rendering and true SENT/DELIVERED/READ status (never faked) are preserved.
- **Files:** `frontend/assets/js/chat.js`, `frontend/assets/js/messages.js`
  (new `markLocalSending`).

### 8. Obsolete backend URL / deployment contract
- **Finding:** `nexora-backend-ptsc.onrender.com` lingered in test files,
  `render.yaml` (`ALLOWED_HOSTS`), and docs; the app (`config.js`) already used
  the correct `nexora-f397.onrender.com`.
- **Fix:** normalized every reference to `nexora-f397.onrender.com`. Render still
  works either way because `settings.py` appends `RENDER_EXTERNAL_HOSTNAME` to
  `ALLOWED_HOSTS`, but the stale value was misleading and broke the URL contract
  tests. CORS/CSRF (`nexora-eight-lilac.vercel.app`) and cross‑site cookies
  (`SameSite=None; Secure`) were already correct — unchanged.
- **Files:** `render.yaml`, `README.md`, `DEPLOYMENT_REPORT.md`,
  `frontend/README.md`, `backend/tests/*`, `frontend/tests/*`.

---

## Cache strategy (unchanged, verified correct)
- **Cached (safe/public):** static JS/CSS (revalidated — see note), images
  (1 week), public branding/config (localStorage, 5‑min freshness window +
  background revalidation), the generated PWA manifest.
- **Never publicly cached:** `/api/me/`, messages, members, dashboard, security
  events, notifications, private media (all `cache: no-store`; SW does not cache
  authenticated responses).
- **Note on `vercel.json`:** JS/CSS use `max-age=0, must-revalidate` — this is
  **correct** for un‑hashed filenames; aggressive immutable caching would serve
  stale application code after a deploy (the exact hazard the brief warns about).
  Left as‑is deliberately.

## Verified good (audited, no change needed)
- **Member‑creation frontend** (`members.js`) already has success toast, field +
  form‑level error mapping (`mapMemberErrors`), busy‑state duplicate protection,
  and local reconciliation. Backend `create()` validates and returns typed 400s.
- **api.js**: in‑flight GET de‑dup, refresh de‑dup, 401 single‑refresh‑then‑retry,
  timeout/offline classification.
- **websocket.js**: single socket, bounded backoff + jitter, HTTP‑vs‑WS auth
  separation, one refresh attempt, heartbeat zombie recycling.

## NOT EXECUTED (require live infra / real devices — reasoned, not claimed)
- End‑to‑end login/logout, session survival across refresh, and refresh‑storm
  behavior against the live Render backend.
- Real Android keyboard / `visualViewport` behavior and the 320–430px breakpoint
  matrix in a real browser (changes are CSS‑grid + `dvh` + safe‑area, designed to
  satisfy them).
- Live dashboard realtime, WebSocket reconnect UX, and media/voice round‑trip
  through real object storage + FFmpeg workers.
- MySQL‑specific query plans (tests run on SQLite; no schema/index change was
  made, so production query shape is unchanged except the dashboard's single
  combined aggregate).

## How to apply
```bash
git checkout -b fix/production-audit
git apply nexora-production-audit.patch   # or: git am < ...  /  merge the branch
# backend:  pytest   &&  python manage.py check
# frontend: node --test frontend/tests/
```
