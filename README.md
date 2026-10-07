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

Production start-up refuses to boot without `SECRET_KEY`, an authenticated
`SECURITY_RELAY_TOKEN`, `ALLOWED_HOSTS`, `REDIS_URL`, a non-SQLite database,
`STORAGE_BUCKET` and at least one CORS/CSRF origin. The production relay cannot
be disabled. That is deliberate: misconfiguration should fail loudly rather
than silently expose the backend.

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

For the common local split (static frontend on `127.0.0.1:5500`, Django on
`127.0.0.1:8000`), copy `frontend/.env.example` to `frontend/.env`, build, and
serve `dist/`:

```bash
cp frontend/.env.example frontend/.env
cd frontend && node build.mjs
python3 -m http.server 5500 --directory dist
```

The example points to the local Django origin so `/api/auth/login/` cannot be
mistakenly posted to the static server. For a same-origin reverse proxy, use
`API_BASE_URL=same-origin`. The browser-side `config.js` reads only the
build-injected `API_BASE_URL`; it never guesses a backend hostname or falls
back to a committed production origin. On Vercel, configure the public HTTPS
Security Relay origin in the Vercel project environment; its production build
rejects a missing, loopback, or plain-HTTP API origin unless same-origin proxy
mode is explicitly selected. Never point browser code at the private Django
service or object-store internals.

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

### 4.1 The deployed topology (native Python; no Docker)

```
Vercel static frontend
        │ HTTPS / WSS; API_BASE_URL points to the public relay
        ▼
Render public Python Security Relay
  fixed-host/path ASGI reverse proxy; auth, cookies, media + WebSocket upgrades
        │ private Render network; shared relay-hop token
        ▼
Render private Django/Channels ASGI service
        ├── external managed MySQL 8
        ├── external managed Redis (REDIS_URL — channels, cache, presence)
        ├── private S3-compatible object storage
        └── separate Render background workers (push / media / uploads)
```

Only the relay is public. The backend is a private Render service and refuses
HTTP and WebSocket requests without the shared relay token. The relay has one
configured private upstream and exact public-host/path allowlists; it is not an
open proxy. The browser continues to use the centralized `API_BASE_URL` from
`config.js`, but that value must point to the relay—not the private backend.

There is **no Docker anywhere in this project**: no Dockerfile, Compose file,
container Redis or container MySQL. MySQL, Redis and object storage remain the
existing external managed services. `render.yaml` describes the native Python
relay, private ASGI backend and separate workers.

The backend start command remains ASGI, never WSGI:

```bash
gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker -c gunicorn.conf.py
```

`gunicorn.conf.py` binds `0.0.0.0:$PORT` and disables Uvicorn's generic
proxy-header parser. The ASGI boundary trusts exactly one forwarded scheme only
after the relay token is authenticated. Do not expose the private backend or
replace this with a proxy that forwards arbitrary client-supplied headers.

**Relay configuration** (the Render Blueprint wires the private host/port and
shared token):

```
SECURITY_RELAY_TOKEN=<same generated secret on relay and backend>
UPSTREAM_HOST / UPSTREAM_PORT=<private backend host/port from Render>
UPSTREAM_SCHEME=http
PUBLIC_SCHEME=https
PUBLIC_HOSTS=<optional exact aliases; Render hostname is auto-detected>
```

**Backend environment (Render → Environment)**

```
DJANGO_ENV=production
DEBUG=False
SECRET_KEY=<unique random value>
SECURITY_RELAY_REQUIRED=True
SECURITY_RELAY_TOKEN=<same generated secret as relay>
ALLOWED_HOSTS=<exact public relay hostname and configured API aliases>
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

Never commit secrets. The backend must list the relay's actual public hostname
in `ALLOWED_HOSTS`; custom aliases must also be listed exactly in the relay's
`PUBLIC_HOSTS` setting and backend `ALLOWED_HOSTS`.

**Privacy boundary:** the relay necessarily sees the connecting peer at its
network edge. The app does not claim anonymity. It strips user-supplied
forwarded-IP metadata, never forwards a browser IP to Django, and does not
persist the relay or peer address in security-event rows in relay-required
mode. The browser sees the relay origin, not the backend's private host. The
shared token authenticates the service-to-service hop; it is not a replacement
for user authentication, authorization, CSRF or WebSocket-origin checks.

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
* The public relay health check forwards `/health/live/` to the private ASGI
  backend. `/health/ready/` is also routed through the relay and independently
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

### 4.5 Alternative: another trusted proxy topology

A same-origin proxy can serve the frontend and forward `/api/`, `/ws/` and
health requests, but in production it must provide the same network boundary:
fixed private upstream, exact host/path allowlists, trusted forwarded headers,
and the shared relay token expected by the backend ASGI gate. Do not expose the
Django service directly or simply pass browser-supplied `X-Forwarded-*` headers.
Build with `API_BASE_URL=same-origin` only when the proxy actually forwards the
API and WebSocket routes. The browser then uses relative URLs.

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

Validation order: server and organization size ceilings → **content signature
(magic bytes)** → extension must match the detected type → structural decode
and pixel limits for images → probed stream kind and duration for audio/video
when `ffprobe` is available. Browser MIME is only a hint: ambiguous voice MP4
is accepted only when the extension/MIME indicate audio and `ffprobe` confirms
an audio-only stream. A file is stored under a generated, unguessable key; the
client's filename never touches the storage path. Executables, archives and
PDFs are rejected outright.

Files are private. `GET /api/media/{uuid}/` authorizes the caller and then
streams the object with HTTP range support; `GET /api/media/{uuid}/url/`
authorizes first and returns the configured media URL. In relay-required
production this stays on the protected API stream and never exposes an
object-store address; non-relay deployments may use short-lived signed URLs.
Derivatives — thumbnail, optimized WebP, video poster — are
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
