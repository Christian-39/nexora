# nexora
Admin managed platform similar to whatsapp.
# NEXORA backend

Security-first, independently deployed Django/DRF/Channels backend. Each installation has one database, one storage bucket and one secret set. There is deliberately no tenant model or cross-instance registry.

## Implemented foundation

- UUID custom users; ADMIN/MEMBER roles; centralized E.164 phone normalization; hashed initial six-digit PIN.
- HttpOnly-cookie JWT access/refresh tokens, refresh replay detection, server-side device-session records, per-session revocation, blacklist-backed logout, throttling, temporary PIN lockout, and backend-enforced first-login PIN change.
- Admin-only member creation (no registration route), activation/deactivation, session-revoking credential reset, and auditable lifecycle actions.
- Integrity-constrained admin/member private conversations, participant authorization, cursor-paginated messages, authorized text/member/group/date/media-type search, sender/client UUID idempotency, edit/delete windows, controlled reactions, aggregate delivery/read states, and WebSocket message/receipt/change/typing events.
- Admin-created groups with validated active membership, transactional add/remove membership, matching conversation authorization, configurable send permissions, archive support, and audit records.
- Persistent notifications, user-scoped push-subscription APIs, durable push-delivery queue, retry/backoff, automatic invalid-subscription cleanup, and a locking-safe `push_worker` management command.
- Authorized image/video/voice upload and download endpoints, signature/extension/size validation, disk-backed large uploads, random private storage keys, image verification, ffprobe duration/codec/dimension checks, media receipts/notifications/WebSocket events, and private S3-compatible storage.
- Resumable S3-compatible multipart upload sessions with signed part URLs, size verification, cancellation, idempotent client IDs, private keys, and a durable finalizer that validates content before publishing a message.
- Durable locking-safe media worker that streams originals to temporary disk, creates bounded WebP image derivatives and video posters with Pillow/FFmpeg, records processing state, and serves every variant through the same conversation authorization check.
- Database-driven message/media/edit/delete/profile policies with bounded serializer validation, short-lived cache invalidation, and audit records for settings and branding changes.
- Validated logo/favicon uploads with random private storage keys and controlled public proxy endpoints.
- Safe public configuration allowlist, cookie-authentication CSRF enforcement and bootstrap endpoint, restricted CORS, CSP, Permissions-Policy, secure production cookie/header defaults, and normalized API errors.
- Fail-closed production startup when a strong secret, MySQL-compatible database, Redis, or private object storage is missing.
- Redis-backed multi-process presence with authorized contact scopes, connection counters, offline last-seen updates, and user privacy controls; local-memory presence is development-only.
- Member profile preferences for theme, phone visibility, last-seen privacy, and push enablement.
- Initial database migrations and automated authorization, group-permission, cross-conversation reply, idempotency, credential, media-IDOR, and upload tests.
- Container deployment assets for ASGI API, MySQL, authenticated Redis, durable push worker and media worker, plus liveness/readiness probes.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # export values with your preferred env loader
export DEBUG=True SECRET_KEY='dev-only-change-me'
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
uvicorn config.asgi:application --host 0.0.0.0 --port 8000
python manage.py push_worker              # separate durable push worker
python manage.py finalize_uploads          # validates completed direct uploads
python manage.py media_worker             # image/video derivative worker
pytest
```

Use an international phone number when `createsuperuser` asks for the username. Never ship a predefined administrator.

## Production topology

Static frontend → HTTPS reverse proxy → ASGI workers → independent MySQL 8 database + Redis + private S3-compatible bucket. Run migrations before switching traffic. Use `uvicorn` workers behind Gunicorn or Daphne. Redis is mandatory with multiple ASGI processes; the in-memory layer is development-only.

Set a unique `SECRET_KEY`, database credentials, exact `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, Redis URL, private storage credentials and VAPID keys per deployment. Install FFmpeg/ffprobe on API and media-worker hosts. Never share a database, bucket or credential set between organizations.

## API

- `GET /api/auth/csrf/`, `/api/me/`, `/api/auth/sessions/`; `POST /api/auth/login/`, `/logout/`, `/refresh/`, `/change-pin/`; `DELETE /api/auth/sessions/{uuid}/`
- `/api/members/` plus `/{uuid}/activate/`, `/deactivate/`, and `/reset-pin/` (administrator only)
- `/api/conversations/`; `/api/conversations/{uuid}/messages/`; `/read/`
- `/api/messages/search/`; `/api/messages/{uuid}/`; `/api/messages/{uuid}/reaction/`
- `/api/groups/`; `/api/groups/{uuid}/members/`; `/api/groups/{uuid}/leave/`; `/api/groups/{uuid}/archive/`
- `GET /api/security/` and `GET /api/audit/` with administrator-only filtering
- `GET /api/unread/` for authoritative global and per-conversation unread counts
- `POST /api/media/`; `GET /api/media/{attachment_uuid}/`
- `/api/uploads/`; `/api/uploads/{uuid}/part/`; `/complete/`; abort/status via `/api/uploads/{uuid}/`
- `/api/notifications/`; `/api/notifications/read/`; `/api/push/`
- `GET/PATCH /api/settings/`; `POST /api/settings/branding/`; `GET /api/audit/` (administrator only)
- `GET /api/public/config/`; `GET /api/public/branding/logo/`; `/favicon/`
- WebSockets: `/ws/conversations/{uuid}/` and `/ws/presence/`, authenticated by an active access-cookie device session.

Responses use `{success,message,data}` or `{success,message,code,errors}`. Access cookies are short lived. A frontend should call refresh with credentials included and keep tokens out of JavaScript storage.

## Media and push

The models reserve private storage keys rather than public URLs. Production media endpoints must issue short-lived signed URLs only after conversation authorization. Upload completion should validate signatures with libmagic/Pillow/ffprobe and verify object metadata. Push subscriptions are persisted; delivery workers should use VAPID environment secrets and deactivate HTTP 404/410 subscriptions.

## Security notes

PINs are only passed to Django's password hasher. They must never be logged. UUIDs do not replace object authorization. Conversation querysets and WebSockets both verify membership. Use TLS, Redis authentication, private object ACLs, request-size limits at the proxy, malware scanning, CSP at the frontend, database backups, key rotation and centralized redacted logs.

## Current scope boundary

This repository is a production-oriented core, not a claim that every item in the supplied 80-section specification is finished. Before launch, complete and independently review: resumable/direct-to-object-storage uploads, generated video posters and image derivatives, presence/last-seen privacy controls, richer profile/settings controls, notification aggregation, deployment manifests, operational monitoring, backup/restore drills, malware scanning, migrations review, and the complete acceptance/security/load-test matrix.


# NEXORA — Frontend

Pure HTML + CSS + vanilla JavaScript (ES modules). No frameworks, no build step,
no bundler, no runtime dependencies. It is a static bundle that talks to the
Django / DRF / Channels backend over REST and WebSockets.

---

## 1. Layout

```
frontend/
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

Configuration is read at load time from `<meta>` tags (or `window.NEXORA_RUNTIME`
if you prefer to inject it):

| Meta name | Default | Meaning |
|---|---|---|
| `nexora-api-base` | `""` (same origin) | Backend origin, e.g. `https://api.example.org` |
| `nexora-api-prefix` | `/api` | API path prefix |
| `nexora-auth-mode` | `cookie` | `cookie` (HttpOnly session — recommended) or `bearer` |

**Recommended deployment:** serve this directory from the same origin as Django
(reverse proxy `/api/` and `/ws/` to the backend). That keeps the session cookie
`SameSite=Lax`, avoids CORS entirely, and lets the service worker cache the shell.

If you serve it cross-origin, the backend must send
`Access-Control-Allow-Credentials: true` and an explicit
`Access-Control-Allow-Origin`, and the session cookie must be
`SameSite=None; Secure`.

### Authentication transport

* **cookie mode (default).** The browser holds an HttpOnly session cookie.
  Nothing is duplicated into `localStorage`. CSRF is read from the `csrftoken`
  cookie and sent as `X-CSRFToken` on unsafe methods.
* **bearer mode.** The access token is kept **in memory only** (`api.js`
  `tokenStore`) and is never persisted. A page reload re-establishes the session
  via `POST /api/auth/refresh/` using the refresh cookie.

Concurrent 401s trigger exactly one de-duplicated refresh attempt; if it fails,
an `unauthorized` event is emitted and the user is routed to sign-in.

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

GET  /media/{id}/url/        → { url, thumbnail_url, expires_at }   (signed URLs)

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
Endpoint: `/ws/app/` on the API origin (`ws://`/`wss://` derived automatically).
In bearer mode a short-lived `?token=` query parameter is appended.

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
Voice notes fetch audio only on first play. Expiring signed URLs are
re-resolved automatically on load failure.

---

## 5. Security posture

* Role checks in the UI are **UX only**. Hidden buttons are not a control —
  every action is authorized server-side, and 403 responses are surfaced.
* PINs are masked, never logged, never stored, and the input is wiped after
  submission. Sign-in failures return a deliberately generic message so account
  existence is not disclosed.
* The service worker **never** caches `/api/` responses, private media, or
  anything authenticated. Only the public application shell and static assets
  are cached (`network-only` for API, `stale-while-revalidate` for assets,
  `network-first + offline.html` for navigations).
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

* `manifest.webmanifest` with maskable icon and shortcuts. For fully dynamic
  PWA branding, serve this file from Django and inject `name` / `short_name` /
  `theme_color` / `icons` from the same configuration as `/public/config/`; the
  static file here is the fallback. Document metadata (`theme-color`,
  `application-name`, `apple-mobile-web-app-title`, favicon) is always updated
  dynamically at runtime.
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

Point `nexora-api-base` at your Django dev server, or (better) run both behind
one reverse proxy so cookies and the service worker share an origin.

Service workers and `getUserMedia` (voice notes) require a secure context:
`https://` or `http://localhost`.
