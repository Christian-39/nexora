# NEXORA

A private, administrator-controlled messaging platform. Each deployment is an
independent installation for one organization: its own database, its own
storage bucket, its own secrets, its own users and its own branding. There is
no tenant registry, no shared database and no shared bucket.

* **Backend** — Django 5 + Django REST Framework + Channels (ASGI), MySQL 8,
  Redis, S3-compatible private object storage.
* **Frontend** — vanilla JavaScript ES modules, no build step, no framework,
  installable as a PWA.
* **Authentication** — phone number + six-digit PIN, delivered as HttpOnly
  cookies carrying rotating JWTs bound to revocable device sessions.
* **No email architecture.** NEXORA never sends email. There is no SMTP
  configuration, no verification flow and no email-based password reset. An
  email address exists only as an optional contact field.

---

## 1. Two kinds of configuration

This distinction runs through the whole codebase and is the single most
important thing to understand before changing anything.

| | **Environment configuration** | **Database configuration** |
|---|---|---|
| Lives in | `backend/.env` (and the real process environment) | the `PlatformConfiguration` row |
| Read by | `backend/config/env.py` → `backend/config/settings.py`, **once** | `apps.platform_settings.services.messaging_policy()` |
| Contains | `SECRET_KEY`, database, Redis, storage credentials, VAPID keys, allowed hosts/origins, hard ceilings | organization name, branding, colours, policy text, messaging limits, feature switches, group defaults, security policy |
| Changed by | a deployment engineer, with a restart | the administrator, in **Settings**, effective immediately |
| Exposed to the frontend | **never** | via `GET /api/public/config/`, through an explicit allow-list |

Application code never calls `os.getenv` or `os.environ.get`. Every value is
read through `from decouple import config` in `config/env.py`, and everything
else reads `django.conf.settings`. `config/env.py` is the only module permitted
to touch the environment.

Values set in the admin Settings screen genuinely change backend behaviour —
lowering `max_message_length` causes the API to reject longer messages;
turning off reactions makes `POST /api/messages/{id}/reactions/` return 403;
disabling previews changes the text stored in notifications and pushed to
devices. They are not cosmetic.

---

## 2. Requirements

| | Development | Production |
|---|---|---|
| Python | 3.11+ | 3.11+ |
| Database | SQLite (automatic fallback) | **MySQL 8+ (required)** |
| Redis | optional (in-memory fallbacks) | **required** |
| Object storage | local filesystem | **S3-compatible bucket (required)** |
| `ffmpeg` / `ffprobe` | optional | **required for video posters and duration probing** |

Production start-up refuses to boot without `SECRET_KEY`, `ALLOWED_HOSTS`,
`REDIS_URL`, a non-SQLite database, `STORAGE_BUCKET` and at least one
CORS/CSRF origin. That is deliberate: a misconfigured deployment should fail
loudly, not quietly serve an insecure app.

---

## 3. Local development

### 3.1 Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit SECRET_KEY at minimum
python manage.py migrate
python manage.py createsuperuser --phone +2348030000000   # the first administrator

python -m uvicorn config.asgi:application --reload --host 0.0.0.0 --port 8000
```

With `DATABASE_URL` and `DATABASE_NAME` empty and `DEBUG=True`, SQLite is used
automatically. With `REDIS_URL` empty, Channels uses the in-memory layer and
the cache uses local memory — fine for one process, **not** for production.

Background workers (optional locally, required in production):

```bash
python manage.py media_worker     # image/video/voice derivatives
python manage.py push_worker      # durable web-push delivery
```

Both accept `--once` to drain the queue and exit, and can be run several times
in parallel: they claim work with `SELECT … FOR UPDATE SKIP LOCKED`.

### 3.2 Frontend

The frontend is static. Serve `frontend/` with anything:

```bash
cd frontend
python -m http.server 5500        # or: npx serve -l 5500, or VS Code Live Server
```

Then open <http://127.0.0.1:5500/login.html>.

**No file needs editing to point the frontend at the backend.**
`frontend/assets/js/config.js` resolves the API origin in this order:

1. `window.NEXORA_RUNTIME = { apiBase: '…' }` (or a `<script id="nexora-config" type="application/json">` blob) injected by the deployment;
2. `<meta name="nexora-api-base" content="https://api.example.org">`;
3. **local-dev detection** — a page served from `localhost`/`127.0.0.1` on a
   known static-server port (5500, 5501, 8080, 3000, 5173 …) assumes Django is
   on **the same hostname**, port 8000;
4. **same origin** — the recommended production layout.

Step 3 preserves the hostname on purpose. Browsers scope cookies by host and
ignore the port, so a page on `http://127.0.0.1:5500` must call
`http://127.0.0.1:8000` — not `localhost:8000` — or the session and CSRF
cookies are never sent. Use `127.0.0.1` on both sides, or `localhost` on both
sides. Verified working on `127.0.0.1:5500`, `localhost:5500` and
`localhost:8080`.

The WebSocket origin is always derived from the resolved API origin by swapping
the scheme (`http→ws`, `https→wss`), so TLS can never be mismatched.

### 3.3 Everything on one origin (optional)

`python devserver.py` serves the static frontend and the Django ASGI app from
port 5500, mirroring the production reverse-proxy layout. It is a convenience
for demos and same-origin testing; nothing in the application depends on it.

### 3.4 First sign-in

The administrator creates members; members never self-register. A new member's
initial PIN is **the first six digits of their normalized phone number**
(`+2348012345678` → `234801`). It is never returned by the API and never
logged. The member must change it before any other endpoint will respond — the
API returns `403 PERMISSION_DENIED` until they do.

---

## 4. Production deployment

### 4.1 Same origin (recommended)

One reverse proxy (nginx/Caddy) serves the static `frontend/` directory and
forwards `/api/`, `/ws/`, `/static/` and `/health/` to the ASGI server. No CORS
is involved, cookies stay `SameSite=Lax`, and `config.js` resolves to the same
origin with no configuration at all.

```
COOKIE_SAMESITE=Lax
COOKIE_SECURE=True
CSRF_TRUSTED_ORIGINS=https://app.example.org
```

### 4.2 Separate origins

The frontend is on `https://app.example.org`, the API on
`https://api.example.org`:

```
# backend/.env
ALLOWED_HOSTS=api.example.org
CORS_ALLOWED_ORIGINS=https://app.example.org
CSRF_TRUSTED_ORIGINS=https://app.example.org,https://api.example.org
COOKIE_SAMESITE=None          # required for cross-site cookies
COOKIE_SECURE=True            # enforced: SameSite=None without Secure is refused
```

```html
<!-- every page in frontend/ -->
<meta name="nexora-api-base" content="https://api.example.org">
```

`CORS_ALLOW_ALL_ORIGINS` is never enabled, in any mode.

### 4.3 Running

```bash
python -m uvicorn config.asgi:application --host 0.0.0.0 --port 8000 \
  --workers 4 --proxy-headers --forwarded-allow-ips='*'
python manage.py media_worker
python manage.py push_worker
```

Both HTTP and WebSocket traffic go through the ASGI app; do not run the WSGI
entry point if you want realtime.

### 4.4 Web push keys

```bash
python -c "from py_vapid import Vapid01; v=Vapid01(); v.generate_keys(); print(v.public_key, v.private_key)"
```

Set `PUSH_PUBLIC_KEY`, `PUSH_PRIVATE_KEY` and `PUSH_CONTACT`. The public key is
served to browsers through `/api/public/config/`; the private key never leaves
the backend and never appears in any response or log.

---

## 5. Architecture

```
frontend/
  assets/js/config.js   runtime configuration — the ONLY place origins are resolved
  assets/js/api.js      the ONLY HTTP layer: CSRF, credentials, timeouts,
                        retries, refresh-on-401, normalized errors, uploads
  assets/js/websocket.js single multiplexed socket with bounded backoff
  sw.js                 PWA shell cache; /api/ is always network-only
backend/
  config/env.py         the ONLY module that reads the environment
  config/settings.py    all configuration, read once, through decouple
  apps/accounts/        users, PINs, device sessions, profile, member admin
  apps/conversations/   conversations, messages, receipts, reactions,
                        realtime.py (every server event) and consumers.py
  apps/media/           validation, private storage, signed URLs, derivatives
  apps/groups/          groups, membership, per-group permission flags
  apps/notifications/   persistent notifications + durable web push
  apps/platform_settings/ the organization's database configuration
  apps/audit/ apps/security/  audit trail and security event log
```

### 5.1 Response envelope

Every JSON response has one shape, enforced by
`apps.core.renderers.EnvelopeJSONRenderer`:

```jsonc
{ "success": true,  "message": "Message sent", "data": { … } }
{ "success": false, "message": "…", "code": "PERMISSION_DENIED", "errors": {} }
```

Lists are cursor-paginated: `data: { next, previous, results }`.

### 5.2 Authorization model

* An administrator may message any member privately.
* A member has exactly one private conversation: with an administrator.
* **Member ↔ member private conversations cannot be created**, by any route.
* Members interact with each other only inside groups an administrator put
  them in, and only as far as that group's flags allow (`members_can_send`,
  `members_can_send_media`, `members_can_send_voice`, `members_can_reply`,
  `members_can_react`, `members_can_view_members`, `members_can_leave`).
* Every conversation, message and attachment lookup is scoped to the caller's
  participation. An unauthorized UUID returns **404**, not 403, so identifiers
  cannot be probed.

### 5.3 Realtime

One multiplexed socket per tab: `/ws/app/`.
(`/ws/conversations/{uuid}/` remains for single-thread clients.)

Client → server: `conversation.join`, `conversation.leave`, `typing`,
`message.read`, `message.delivered`, `presence.ping`, `ping`.

Server → client: `connection.ready`, `conversation.joined`,
`conversation.denied`, `message.new`, `message.updated`, `message.edited`,
`message.deleted`, `message.delivered`, `message.read`, `message.reaction`,
`typing.start`, `typing.stop`, `presence.online`, `presence.offline`,
`presence.update`, `unread.update`, `conversation.unread`,
`conversation.created`, `conversation.updated`, `group.membership`,
`group.removed`, `notification.new`, `notification.read`, `media.ready`,
`auth.error`, `pong`.

Every one of them is emitted from `apps/conversations/realtime.py` and nowhere
else. A socket is closed when the access token that opened it expires
(code 4401); the client refreshes over HTTP and reconnects with bounded
exponential backoff and jitter. Authentication failures close with 4003 and
are not retried.

### 5.4 Media

Upload is a single multipart `POST /api/conversations/{id}/messages/` carrying
`kind`, `client_id`, `file` and optionally `caption`, `reply_to`, `duration`,
`poster`. Message, attachment, receipts and the realtime event are created in
one transaction. A resumable chunked API (`/api/uploads/…`) exists for very
large files.

Validation order: declared size → **content signature (magic bytes)** →
declared MIME must agree → extension must match the signature → structural
decode and pixel limits for images → probed duration for audio/video. A file
is stored under a generated, unguessable key; the client's filename never
touches the filesystem. Executables, archives and PDFs are rejected outright.

Files are private. `GET /api/media/{uuid}/` authorizes the caller and then
streams the object with HTTP range support; `GET /api/media/{uuid}/url/`
authorizes first and then issues a short-lived signed URL (S3-compatible
storage only). Derivatives — thumbnail, optimized WebP, video poster — are
produced by `media_worker` out of band; a derivative failure never destroys
the original, and `media.ready` tells connected clients when they exist.

---

## 6. Tests

```bash
cd backend
DJANGO_ENV=test python -m pytest        # in-memory SQLite, no services needed
```

96 tests covering authentication and lockout, CSRF, authorization and IDOR,
messaging idempotency/ordering/receipts, media validation and streaming,
WebSocket authorization and every emitted event, push subscription and
aggregation, database-driven settings, and — in
`tests/test_frontend_contract.py` — the frontend↔backend contract itself:
every endpoint `api.js` calls is asserted against the real URL map, every
socket event the UI listens for is asserted against the backend source, and
the frontend is scanned for raw `fetch`, hardcoded origins, `innerHTML`/`eval`
sinks and sensitive values in web storage.

`DJANGO_ENV=test` selects in-memory SQLite and disables global throttling;
`backend/conftest.py` sets it automatically.

---

## 7. Security posture

* Six-digit PIN, hashed by Django's password hasher; never logged, never
  returned, never stored in the browser.
* Forced PIN change on first sign-in and after an administrative reset;
  changing a PIN revokes every session.
* Failed-attempt lockout and throttling, both administrator-configurable.
  Sign-in responses are identical for unknown phone, wrong PIN, locked and
  deactivated accounts — no account enumeration.
* Tokens live in HttpOnly cookies; the refresh cookie is scoped to
  `/api/auth/` and rotates on every use, with the old token blacklisted.
* CSRF is enforced on every unsafe request **including sign-in** (DRF exempts
  views from the CSRF middleware, so the check is explicit). The frontend
  bootstraps the cookie from `/api/auth/csrf/` before its first unsafe call.
* Security headers: HSTS, CSP, `nosniff`, `Referrer-Policy`,
  `Permissions-Policy`, `X-Frame-Options: DENY`, Secure + SameSite cookies.
* No `innerHTML`, no `eval`, no `new Function` in frontend code; all
  user-generated text is inserted as `textContent`.
* The service worker never caches anything under `/api/` — neither
  authenticated JSON nor private media — so nothing survives sign-out in a
  shared cache.
* Audit and security logs strip any key resembling a PIN, password, token or
  secret before writing.

---

## 8. Accessibility and responsiveness

Layouts are fluid from 320 px upward with breakpoints at 360, 430, 768, 1024,
1280 and 1440 px, plus coarse-pointer and short-landscape handling. There is no
global `overflow-x: hidden`; the only horizontal-overflow rules are on named
scroll containers that also set `overflow-y: auto`. Interactive elements are
real semantic controls with visible focus, keyboard operation, ARIA labelling
and live regions, and `prefers-reduced-motion` is respected.
