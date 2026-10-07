# NEXORA — Frontend

Pure HTML + CSS + vanilla JavaScript (ES modules). No frameworks, bundler or
runtime dependencies. The optional Node build step only injects public
deployment configuration and versions the service worker; it does not compile
or transform the application. The static bundle talks to Django/DRF/Channels
over REST and WebSockets through the configured public Render API origin in
production.

---

## 1. Layout

```
frontend/
├── build.mjs           injects public API_BASE_URL and versions sw.js
├── vercel.json         Vercel build/output and security/cache headers
├── .env.example        local API_BASE_URL example and Vercel guidance
├── index.html          session probe → admin.html or chat.html
├── login.html          sign-in + enforced first-login PIN change
├── chat.html           conversation list + thread + composer (both roles)
├── groups.html         group directory, creation, group information
├── members.html        admin member management
├── admin.html          admin dashboard
├── settings.html       full settings surface (role-aware)
├── profile.html        personal subset of settings
├── 403 / 404 / 500 / offline.html
├── manifest.webmanifest
├── sw.js               service worker (shell cache, push, notification click)
├── robots.txt
└── assets/
    ├── css/  variables · themes · typography · main · components · chat · admin · responsive
    ├── js/   api · auth · websocket · messages · chat · media · voice · presence
    │         notifications · push · members · groups · settings · navigation
    │         theme · pin · ui · utils
    └── images/ icon-192 · icon-512 · icon-maskable-512
```

Every module has one responsibility. No file is a catch-all, and there are no
circular imports (`ui` and `utils` are leaves; `api` depends only on `utils`).

---

## 2. Deployment configuration

The browser never guesses a backend from the page hostname and contains no
production API or loopback fallback. `assets/js/config.js` is the only browser
module that reads the public `API_BASE_URL` value.

### Vercel build

Set the Vercel project **Root Directory** to `frontend`, configure the public
`API_BASE_URL` environment variable, and let `frontend/vercel.json` run:

```sh
node build.mjs
```

The build copies the static app to `dist/`, injects the JSON-encoded public
origin into every HTML page, and versions the service-worker cache from the
static source fingerprint, commit identity and API origin. Example value:

```text
API_BASE_URL=https://<service>.onrender.com
```

It must be the public Render API origin in production (e.g.
`https://nexora-f397.onrender.com`)—never any private endpoint or the storage
host. It must be an HTTP(S) origin only (no credentials, path, query or
fragment);
Vercel requires HTTPS and rejects a missing value, loopback hosts, and plain
HTTP API origins. Use the explicit value `same-origin` only when a reverse proxy
serves `/api/` and `/ws/` from the same origin as the pages. `API_BASE_URL` is public configuration, never a
secret; do not put credentials or secret keys in it.

### Local static hosting

For a separate Django API on port 8000 and this static frontend on port 5500:

```sh
cp .env.example .env       # local example: http://127.0.0.1:8000
node build.mjs
python3 -m http.server 5500 --directory dist
```

This explicit API origin prevents `/api/` requests from being sent to the
standalone static server (which otherwise returns 405). `frontend/.env` is
untracked and never shipped. For a same-origin reverse proxy, set
`API_BASE_URL=same-origin` before building; serving the unbuilt source without
a runtime override also intentionally keeps root-relative URLs for that proxy.

On Vercel, set `API_BASE_URL` in the project environment to the public HTTPS
Render API origin (e.g. `https://nexora-f397.onrender.com`). Never use the
local example value or any private backend hostname there. The Vercel build
fails if this production value is missing, loopback, or plain HTTP unless an
explicit same-origin trusted proxy is configured. Never commit a real frontend
`.env`.

The `API_PREFIX` remains `/api`. REST requests live in `assets/js/api.js`; the
WebSocket scheme/origin is derived centrally (`http→ws`, `https→wss`) from the
configured API origin. No other module maintains a backend host.

For a cross-origin frontend/API deployment, the backend must return an explicit
`Access-Control-Allow-Origin` plus `Access-Control-Allow-Credentials: true`,
trust the frontend origin for CSRF, and set auth cookies `SameSite=None; Secure`.
In production, HTTP requests, WebSocket upgrades, authentication cookies,
Authorization, and media API traffic all flow directly to the public Render
Django/Channels ASGI service. Do not point this setting at any private
infrastructure or storage endpoint. For a different same-origin proxy,
configure the equivalent authenticated network boundary and trusted-header
policy before using relative API URLs.

HTML and code responses are revalidated by Vercel. The service worker versions
its shell cache when `API_BASE_URL` changes, so a previous deployment cannot
silently keep the old API origin.

**Tests:** `node --test frontend/tests/` (Node's built-in runner; no npm
dependencies).

### Authentication transport

The backend contract is cookie-based: access and rotating refresh tokens are
HttpOnly, the refresh cookie is path-scoped, and CSRF is required on unsafe
requests including login and refresh. The central API layer obtains the CSRF
token from `/api/auth/csrf/` JSON and keeps it in memory; JavaScript never
copies a PIN, refresh token or session cookie into web storage.

Concurrent authenticated 401s share one refresh request. A definitive refresh
rejection is latched for the tab until a verified session or successful login;
a timeout, network failure or server outage does not erase the cached display
snapshot or force a false logout. WebSockets authenticate only with the
HttpOnly access cookie—credentials are never appended to a URL.

---

## 3. Backend contract

All paths are relative to the API prefix. Responses may use the normalized
envelope `{ success, message, data }` **or** plain DRF payloads — `api.js`
unwraps both. Errors are normalized into `ApiError { status, code, message,
errors, retryAfter }`.

### Auth & identity
```
POST   /auth/login/            { identifier, phone, pin } → { user, access? }
POST   /auth/logout/
POST   /auth/refresh/
POST   /auth/change-pin/       { current_pin?, new_pin, confirm_pin }
GET    /auth/sessions/         → [{ id, platform, browser, last_active_at, is_current }]
DELETE /auth/sessions/{id}/
GET    /me/                    → user  (see flags below)
PATCH  /me/                    { display_name, phone_visible, presence_visible }
POST   /me/avatar/             multipart
GET    /me/preferences/  ·  PATCH /me/preferences/
```

The `/me/` payload drives role-based UX:

```jsonc
{
  "id": "…", "display_name": "…", "phone": "…", "login_identifier": "…",
  "is_admin": true,                 // or "role": "admin" | "member"
  "must_change_pin": false,         // or requires_pin_change / is_default_pin
  "avatar_url": "…", "phone_visible": true, "presence_visible": true,
  "editable_fields": ["display_name", "avatar", "phone_visible"]
}
```

### Public configuration (drives all branding)
```
GET /public/config/
```
```jsonc
{
  "app_name": "…", "app_short_name": "…", "organization_name": "…",
  "logo_url": "…", "favicon_url": "…",
  "primary_color": "#33526E", "secondary_color": "#1E7A4E",
  "contact_phone": "…", "contact_email": "…", "address": "…", "website": "…",
  "about": "…", "support": "…",
  "policies": [{ "key": "privacy", "title": "Privacy Policy", "body": "…", "url": null }],
  "limits":   { "max_message_length": 4000, "max_image_size": 10485760,
                "max_video_size": 104857600, "max_voice_duration": 300,
                "allowed_image_types": [...], "allowed_video_types": [...] },
  "features": { "replies": true, "reactions": false, "message_editing": false,
                "delete_for_everyone": true, "voice_notes": true,
                "video_messages": true, "presence": true, "typing": true,
                "push": true, "member_leave_group": false }
}
```
Nothing about the organization is hard-coded. `features` gates which controls
are offered; `limits` gates client-side validation. Both are advisory — the
backend still enforces them.

### Conversations, messages, groups, media
```
GET  /conversations/                      ?search= &type= &unread= &cursor= &limit=
GET  /conversations/unread-summary/
GET  /conversations/{id}/
GET  /conversations/{id}/messages/        ?limit= &cursor=   (newest page first)
POST /conversations/{id}/messages/        JSON text, or multipart for media
POST /conversations/{id}/read/            { last_message_id }
POST /conversations/{id}/typing/          { typing }          (WS preferred)

GET  /messages/{id}/   ·  PATCH /messages/{id}/   ·  DELETE /messages/{id}/?scope=self|everyone
POST /messages/{id}/reactions/  ·  DELETE /messages/{id}/reactions/?reaction=

GET/POST /groups/            ·  GET/PATCH/DELETE /groups/{id}/
POST /groups/{id}/archive/   ·  /unarchive/   ·  /leave/
GET/POST /groups/{id}/members/  ·  DELETE /groups/{id}/members/{memberId}/
POST /groups/{id}/image/     multipart

GET  /media/{id}/url/        → { url, thumbnail_url, expires_at }   (short-lived signed object-storage URL; default TTL = SIGNED_URL_TTL_SECONDS = 300 s)

GET  /members/ ?search= &is_active= &selectable=   ·  POST /members/
GET/PATCH /members/{id}/  ·  POST /members/{id}/activate|deactivate|reset-pin/
GET  /members/{id}/activity/  ·  GET /members/{id}/conversation/

GET  /notifications/  ·  POST /notifications/read/  { ids | all }
GET  /push/  → { vapid_public_key }  ·  POST /push/subscribe/  ·  /unsubscribe/
GET/PATCH /settings/  ·  POST /settings/assets/{logo|favicon}/  ·  /settings/policies/
GET  /dashboard/  ·  GET /audit/  ·  GET /security/events/  ·  GET/PATCH /security/
GET  /search/
```

**Message payload** — the frontend accepts flexible field names and normalizes:

```jsonc
{
  "id": "…", "client_id": "…",           // client_id enables idempotent retries
  "conversation_id": "…",
  "kind": "text|image|video|voice|file|system",
  "text": "…", "caption": "…",
  "created_at": "…", "edited_at": null, "is_deleted": false,
  "status": "sent|delivered|read",        // backend-authoritative
  "sender": { "id": "…", "display_name": "…", "avatar_url": "…" },
  "media": { "id": "…", "url": "…", "thumbnail_url": "…", "mime_type": "…",
             "size": 0, "width": 0, "height": 0, "duration": 0,
             "status": "ready|processing", "expires_at": null },
  "reply_to": { "id": "…", "preview": "…", "sender": {...} },
  "reactions": [{ "reaction": "👍", "count": 2, "is_mine": true }],
  "can_edit": false, "can_delete_for_self": true, "can_delete_for_everyone": false
}
```

> **`client_id` is required for duplicate protection.** The client sends the
> same `client_id` for the original attempt and every retry. The backend must
> treat a repeat as idempotent and echo `client_id` back in the created message.

### WebSocket
Endpoint: `/ws/app/` on the configured API origin (`ws://`/`wss://` derived
automatically). The handshake uses the HttpOnly access cookie only; no token or
other credential is placed in the URL.

Frames are `{ "type": "...", ...payload }`. Client → server:
`conversation.join`, `conversation.leave`, `typing`, `message.read`,
`message.delivered`, `presence.ping`, `ping`.

Server → client: `message.new`, `message.updated`, `message.edited`,
`message.deleted`, `message.delivered`, `message.read`, `message.reaction`,
`media.ready`, `typing` / `typing.start` / `typing.stop`, `presence.update` /
`presence.online` / `presence.offline`, `conversation.created` /
`conversation.updated`, `conversation.unread`, `unread.update`,
`group.membership`, `group.removed`, `notification.new`, `notification.read`,
`dashboard.update`, `pong`, and `auth.error` (or close code `4003`) for
rejected authentication.

---

## 4. Correctness guarantees

**Delivery status is never faked.** Local states are only `sending`,
`unconfirmed` and `failed`. `sent` / `delivered` / `read` come exclusively from
the backend. If a send times out or the connection drops mid-flight, the message
shows *"Message status is being confirmed"* and a reconciliation pass re-fetches
the newest page; `client_id` matching either promotes the message to its real
server state or marks it genuinely failed with a retry action.

**Realtime events are reconciled, not trusted blindly.** Every handler is
idempotent, status only ever moves forward (`sending < unconfirmed < sent <
delivered < read`), and out-of-order inserts are placed by timestamp. On
reconnect, every open conversation is reconciled against REST.

**Rendering is XSS-safe.** No user or backend string is ever assigned to
`innerHTML`; `utils.el()` actively throws if you pass an `html` property.
Message bodies go through `renderTextWithLinks()`, which emits text nodes and
only linkifies scheme-qualified `http(s)` URLs.

**Branding can't inject CSS.** Colours are applied only after strict
`#RRGGBB` validation, into CSS custom properties. Logo/favicon URLs must resolve
to `http(s)` and have graceful fallbacks.

**Memory and DOM are bounded.** Each conversation retains at most 300 messages;
conversation stores are held in a 12-entry LRU. Nothing renders thousands of
nodes. Scroll position is preserved exactly when older pages load.

**No polling.** Unread counts come from `unread-summary` plus WebSocket deltas,
refreshed on reconnect and on tab focus only.

**Media is lazy.** Images and video posters load via `IntersectionObserver`;
video files are never fetched to render a list — only on explicit playback.
Voice notes fetch audio only on first play. Media URLs are re-authorized and
re-resolved automatically on load failure; the backend always issues a
short-lived signed object-storage URL (default five minutes) instead of
exposing a long-lived public storage address.

---

## 5. Security posture

* Role checks in the UI are **UX only**. Hidden buttons are not a control —
  every action is authorized server-side, and 403 responses are surfaced.
* PINs are masked, never logged, never stored, and the input is wiped after
  submission. Sign-in failures return a deliberately generic message so account
  existence is not disclosed.
* The service worker caches the public application shell/assets and only an
  explicit allowlist of anonymous branding/config endpoints, when the response
  is marked `Cache-Control: public` and does not vary on cookies or
  authorization. Every other `/api/` response, private media and authenticated
  data stay network-only; no private API data enters Cache Storage.
* Notification click routing accepts only same-scope relative paths, focuses an
  existing window when possible, and re-authenticates on cold start — a
  notification URL can never expose conversation data to an unauthenticated
  session.
* Push content is composed server-side. When previews are disabled the backend
  must send a generic body; the service worker never invents message content.
* `localStorage` holds only non-sensitive preferences (theme, collapsed nav,
  last-opened conversation id, cached **public** branding).

---

## 6. Accessibility

Semantic landmarks and skip links on every page · visible `:focus-visible`
outlines that are never removed · `aria-label` on every icon-only control ·
`role="log"` + `aria-live` on the message thread · polite/assertive live regions
for status announcements (`ui.announce`) · focus trapping and restoration in
modals, sheets and the lightbox · keyboard-operable voice scrubber
(arrows/Home/End/Space) · six-digit PIN entry with per-cell labels, arrow and
backspace navigation, and paste distribution · status conveyed by icon + text,
never colour alone · `prefers-reduced-motion` and `prefers-contrast` honoured.

---

## 7. Responsive behaviour

Verified breakpoints: 320 · 360 · 375 · 390 · 393 · 414 · 430 · 480 · 600 · 768
· 1024 · 1280 · 1440+.

* ≤767px — single pane: list ⇄ full-screen thread with back navigation, primary
  nav becomes a bottom bar (hidden while a thread is open), modals become bottom
  sheets, tables reflow into stacked labelled rows.
* 768–1023px — icon-only nav rail, two-pane chat.
* ≥1024px — full sidebar, optional details panel (overlay drawer below 1280px).
* Safe-area insets applied to headers, composer, nav and sheets; the composer
  uses a ≥16px font so iOS does not zoom on focus, and
  `interactive-widget=resizes-content` keeps it above the keyboard.

There is **no** `overflow-x: hidden` anywhere. Overflow is prevented at the
source with `min-width: 0` on flex/grid children, `overflow-wrap: anywhere` on
user content, and constrained media widths.

---

## 8. PWA

* `manifest.webmanifest` with maskable icon and shortcuts is the static
  fallback. After public configuration loads, the page refreshes document
  metadata and may create a blob-backed manifest with the same public branding;
  no authenticated data is included.
* Updates are non-destructive: a new worker installs but does not activate until
  the user accepts a "Reload" toast, which sends `NEXORA_SKIP_WAITING`.
* `navigator.setAppBadge()` reflects unread state where supported, with a silent
  fallback elsewhere; the backend's `unread_total` on a push payload keeps the
  badge accurate while the app is closed.

---

## 9. Running locally

```bash
python3 -m http.server 8080 --directory frontend
```

The default is same-origin. If the Django API is on a separate local origin,
set `API_BASE_URL` in an untracked `frontend/.env` or build-time environment,
then run `node build.mjs`; for same-origin proxying, set `API_BASE_URL=same-origin`.

Service workers and `getUserMedia` (voice notes) require a secure context:
`https://` or `http://localhost`.
