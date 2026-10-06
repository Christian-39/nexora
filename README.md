# NEXORA

A private, administrator-controlled messaging platform. Each deployment is an
independent installation for one organization: its own database, its own
storage bucket, its own secrets, its own users and its own branding. There is
no tenant registry, no shared database and no shared bucket.

* **Backend** — Django 5 + Django REST Framework + Channels (ASGI), MySQL 8,
  Redis, S3-compatible private object storage.
* **Frontend** — vanilla JavaScript ES modules, no framework or bundler;
  a small Node build step injects public API configuration and versions the
  service worker. Installable as a PWA.
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
| Database | MySQL 8+ or MariaDB (explicitly configured) | **MySQL 8+ or MariaDB (required)** |
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

There is no implicit SQLite fallback: configure a local MySQL/MariaDB database
before running migrations. Long external keys remain unchanged; portable
uniqueness is enforced through fixed-width SHA-256 digest columns. The integrity
migrations preflight duplicate/malformed rows and are retry-aware if MySQL has
already committed a staging-column or index DDL step. Back up and test these
migrations on a production-like database before deployment; do not mark a
failed migration as applied. With `REDIS_URL` empty in development, Channels may
use the in-memory layer and the cache local memory for a single process;
production requires the managed Redis instance.

Background workers (optional locally, required in production):

```bash
python manage.py media_worker     # image/video/voice derivatives
python manage.py push_worker      # durable web-push delivery
```

Both accept `--once` to drain the queue and exit, and can be run several times
in parallel: they claim work with `SELECT … FOR UPDATE SKIP LOCKED`.

### 3.2 Frontend

The frontend remains plain static HTML/CSS/JavaScript, with a small Node build
step for deployment configuration (no bundler or framework). For local
same-origin proxying, serve the source directly:

```bash
cd frontend
python -m http.server 5500
```

For a split frontend/API origin, set `API_BASE_URL` in an untracked
`frontend/.env` or the shell and run `node build.mjs` before serving `dist/`.
The browser-side `config.js` reads only the build-injected `API_BASE_URL`; it
never infers a backend hostname, assumes port 8000, or falls back to a committed
production origin. Empty means root-relative same-origin proxy; an explicit
`same-origin` value is also accepted. Vercel builds require either a valid
public HTTPS origin or explicit same-origin proxy mode; they reject loopback
and insecure HTTP API origins.

The WebSocket scheme is derived from that same value (`http→ws`, `https→wss`).
The handshake uses the HttpOnly access cookie; credentials are not placed in a
query string.

### 3.3 Everything on one origin (optional)

`python devserver.py` serves the static frontend and the Django ASGI app from
port 5500, mirroring the production reverse-proxy layout. It is a convenience
for demos and same-origin testing; nothing in the application depends on it.

### 3.4 First sign-in and administrator PIN reset

The administrator creates members; members never self-register. A new member's
initial PIN is **the first six digits of their normalized phone number**
(`+2348012345678` → `234801`). It is never returned by the API and never
logged.

* **First-login / forced PIN change (`must_change_pin = true`)** — when a member
  signs in with their initial PIN (or after an administrator PIN reset), they
  are routed to the forced PIN setup screen (`POST /api/auth/change-pin/` with
  `{ new_pin, confirm_pin }`). `current_pin` is not required when
  `must_change_pin` is `true`, and `confirm_pin` must match `new_pin`. Weak
  PINs (repeated digits such as `000000`/`111111` or sequential digits such as
  `123456`/`654321`), reusing the current PIN, or reusing the phone-derived
  initial PIN are rejected both client-side and server-side. Upon updating the
  PIN, `credential_state` becomes `CHANGED`, all prior sessions are revoked, and
  a fresh rotated device session + HttpOnly cookies + profile payload are
  returned immediately so the member proceeds straight to the chat workspace.
* **Voluntary PIN change (`must_change_pin = false`)** — requires `current_pin`,
  `new_pin`, and `confirm_pin`.
* **Administrator PIN reset (`POST /api/members/<id>/reset-pin/`)** — only an
  active administrator can reset a member's PIN. The reset sets the member's
  PIN back to the first six digits of their normalized phone number, sets
  `credential_state = RESET_REQUIRED` (`must_change_pin = true`), clears failed
  login counters and temporary lockouts, revokes all active device sessions for
  that member, and records `CREDENTIAL_RESET` audit and security events.

---

## 4. Production deployment

### 4.1 The deployed topology (no Docker)

```
Vercel static frontend            https://nexora-eight-lilac.vercel.app
        │ HTTPS + WSS
        ▼
Render Python web service         https://nexora-f397.onrender.com
  Gunicorn + UvicornWorker + Django Channels
        ├── external managed MySQL 8
        ├── external managed Redis        (REDIS_URL — channels, cache, presence)
        └── external private S3-compatible bucket
        +  separate Render background workers (push / media / uploads)
```

There is **no Docker anywhere in this project**: no Dockerfile, no Compose, no
container Redis and no container MySQL. Redis, MySQL and object storage are
external managed services reached over the network.

`render.yaml` in the repository root is the blueprint for exactly this layout.

**Build command** (`rootDir: backend`)

```bash
./bin/render-build.sh
```

**Start command** — ASGI, never WSGI:

```bash
gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker -c gunicorn.conf.py
```

`gunicorn.conf.py` binds `0.0.0.0:$PORT`, which is what the platform health
checks; a hard-coded port makes the deploy time out. `config.wsgi` cannot serve
WebSockets — deploying it silently removes every realtime feature.

**Backend environment (Render → Environment)**

```
DJANGO_ENV=production
DEBUG=False
SECRET_KEY=<64+ random characters>
ALLOWED_HOSTS=nexora-f397.onrender.com
CORS_ALLOWED_ORIGINS=https://nexora-eight-lilac.vercel.app
CSRF_TRUSTED_ORIGINS=https://nexora-eight-lilac.vercel.app
COOKIE_SAMESITE=None            # cross-site frontend
COOKIE_SECURE=True              # enforced: SameSite=None without Secure is refused
SECURE_SSL_REDIRECT=True
REDIS_URL=<external managed redis url>
DATABASE_URL=mysql://user:password@host:3306/nexora
STORAGE_BUCKET / STORAGE_ENDPOINT / STORAGE_REGION /
STORAGE_ACCESS_KEY / STORAGE_SECRET_KEY
PUSH_PUBLIC_KEY / PUSH_PRIVATE_KEY / PUSH_CONTACT
MEDIA_PROCESS_INLINE=False
FFMPEG_BINARY / FFPROBE_BINARY  (see 4.3)
```

Secrets are set in the dashboard, never committed.

### 4.2 Redis is a hard requirement in production

`REDIS_URL` must point at an **external managed Redis** (Render Key Value,
Upstash, Redis Cloud, ElastiCache …). Production refuses to boot without it and
never silently degrades to the in-memory channel layer, because that layer
cannot deliver an event from one ASGI process to another: messages would appear
only for users who happened to land on the same worker.

* `redis://` or `rediss://` (TLS). A value without a scheme is rejected at boot.
* If a managed provider's TLS certificate cannot be verified by the host trust
  store, set `REDIS_SSL_CERT_REQS=none` — the transport stays encrypted.
* Connection and blocking-pop resilience are configurable via environment
  variables (`REDIS_SOCKET_CONNECT_TIMEOUT=5`, `REDIS_SOCKET_TIMEOUT=5`,
  `REDIS_CHANNEL_SOCKET_TIMEOUT=15`, `REDIS_HEALTH_CHECK_INTERVAL=30`,
  `REDIS_RETRY_ON_TIMEOUT=True`, `REDIS_CHANNEL_CAPACITY=1500`,
  `REDIS_CHANNEL_EXPIRY=60`). The channel socket timeout is automatically kept
  above `channels_redis`'s 5-second `BZPOPMIN` blocking window so idle WebSocket
  consumers never crash with `redis.exceptions.TimeoutError`.
* `/health/live/` reports process liveness; `/health/ready/` independently
  verifies MySQL (`database`), Redis cache (`cache`), Redis Channels
  (`channels`), and private object storage (`storage`) without exposing
  connection URLs or secrets.
* The in-memory layer remains available for local development only.

### 4.3 Background workers and FFmpeg

Push delivery, media derivatives and upload finalization run as **separate
Render background workers**, not inside the web service (a web service is
recycled on deploy and scaled per request):

```
python manage.py push_worker
python manage.py media_worker
python manage.py finalize_uploads
```

`bin/render-build.sh` downloads a static FFmpeg/ffprobe into `backend/bin/`
because a managed Python runtime has no package manager; point `FFMPEG_BINARY`
and `FFPROBE_BINARY` at them. If the download is unavailable, image derivatives
still work and video/voice items are recorded as `FFMPEG_MISSING` rather than
failing the deploy.

### 4.4 Frontend (Vercel)

Set the Vercel project **Root Directory** to `frontend` and define the public
`API_BASE_URL` environment variable for each deployment environment. The build
command in `frontend/vercel.json` runs `node build.mjs`; it validates the origin,
injects it into all HTML pages, and versions the service-worker cache. Example:

```text
API_BASE_URL=https://api.example.org
```

Do not include credentials, a URL path, query or fragment. Vercel builds reject
missing configuration, loopback hosts, and non-HTTPS API origins. Use
`API_BASE_URL=same-origin` only when Vercel or another proxy actually forwards
`/api/` and `/ws/` on the page origin. `API_BASE_URL` is public client configuration, not a secret. Do not use
meta tags or manually edit page HTML for deployment values. `CORS_ALLOW_ALL_ORIGINS`
is never enabled.

`frontend/vercel.json` serves `dist/` and revalidates HTML, JavaScript, CSS and
`sw.js` so a deploy cannot be masked by a stale cached bundle.

### 4.5 Alternative: one origin behind a reverse proxy

One nginx/Caddy serving the frontend build and forwarding `/api/`, `/ws/`,
`/static/` and `/health/` to the ASGI server works unchanged. Build with
`API_BASE_URL=same-origin`; configure the backend's cookie policy appropriately
for that topology. The browser uses relative API URLs.

### 4.6 Web push keys

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
  build.mjs             injects public API_BASE_URL; no client-side host fallback
  sw.js                 public shell/assets cache; only public config/branding API allowlisted
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
# Frontend (Node's built-in test runner; no npm install)
node --test frontend/tests/

# Backend (configure an isolated MySQL 8 or MariaDB test database first)
cd backend
.venv/bin/pytest
.venv/bin/python manage.py makemigrations --check --dry-run
```

The backend settings deliberately fail closed if a MySQL/MariaDB database is
not configured; SQLite is not an implicit test or development fallback. Backend
coverage includes authentication, PIN lifecycle, lockout, CSRF, authorization
and IDOR, message idempotency/receipts, media validation and storage, WebSocket
authorization, Redis/cache resilience, health checks, redacted logging, push,
settings, query-count guards and frontend/backend route contracts. The frontend
suite covers injected API origin/build output, API refresh deduplication and
failure classes, public config caching, WebSocket lifecycle, optimistic media
indexing, PIN contracts, error redaction and mobile layout invariants.

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
  views from the CSRF middleware, so the check is explicit). Before its first
  unsafe call, the frontend obtains the token from `/api/auth/csrf/` JSON and
  keeps it in memory; the backend continues to set Django's CSRF cookie.
* The Django API applies HSTS, a deny-all CSP, `nosniff`, `Referrer-Policy`,
  `Permissions-Policy`, `X-Frame-Options: DENY`, and Secure/SameSite cookies.
  Vercel static pages set frame, MIME-sniffing, referrer, and permissions
  headers, but do not yet have a CSP: their HTML still contains inline startup
  scripts and style attributes, so a strict policy needs a separate hashed-CSP
  change rather than an unsafe-inline claim.
* No `innerHTML`, no `eval`, no `new Function` in frontend code; all
  user-generated text is inserted as `textContent`.
* The service worker caches only explicitly allowlisted public branding/config
  endpoints when they return public cache headers and do not vary on cookies or
  authorization. Every other `/api/` response—including private media and
  authenticated data—remains network-only and is never retained across sign-out.
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
