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
- Resumable chunked upload sessions over authorized API routes: ordered 5 MiB parts stream into private staging storage, session size is enforced, retries use stable client IDs, and `/complete/` validates before creating the message. `finalize_uploads` expires abandoned OPEN sessions and retries cleanup for completed/aborted staging objects; it does not publish messages or process direct multipart uploads.
- Durable locking-safe media worker that streams originals to temporary disk, creates bounded WebP image derivatives and video posters with Pillow/FFmpeg, records processing state, and serves every variant through the same conversation authorization check.
- Database-driven message/media/edit/delete/profile policies with bounded serializer validation, short-lived cache invalidation, and audit records for settings and branding changes.
- Validated logo/favicon uploads with random private storage keys and controlled public proxy endpoints.
- Safe public configuration allowlist, cookie-authentication CSRF enforcement and bootstrap endpoint, restricted CORS, CSP, Permissions-Policy, secure production cookie/header defaults, and normalized API errors.
- Fail-closed production startup when the relay hop token, a strong secret, MySQL-compatible database, Redis, or private object storage is missing; production cannot disable the relay boundary.
- Redis-backed multi-process presence with authorized contact scopes, connection counters, offline last-seen updates, and user privacy controls; local-memory presence is development-only.
- Member profile preferences for theme, phone visibility, last-seen privacy, and push enablement.
- Initial database migrations and automated authorization, group-permission, cross-conversation reply, idempotency, credential, media-IDOR, and upload tests.
- Native Python deployment assets (`render.yaml`, `bin/render-build.sh`, `gunicorn.conf.py`, and `../relay/`) for a public constrained Security Relay, private Django/Channels ASGI service, and separate push/media/upload workers, against the existing external managed MySQL, Redis and object storage. This project does not use Docker.

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
python manage.py finalize_uploads          # expires abandoned resumable sessions and retries staging cleanup
python manage.py media_worker             # image/video derivative worker
pytest
```

Use an international phone number when `createsuperuser` asks for the username. Never ship a predefined administrator.

## Production topology

Static frontend (Vercel) → HTTPS/WSS → **public native-Python Security Relay**
→ private Render network + shared relay-hop token → **private Django/Channels
ASGI service** → the existing external managed MySQL 8, Redis and private
S3-compatible bucket. Separate background workers continue to share the same
configuration. No Docker is used.

The relay (`../relay/app.py`) has one configured private upstream, exact public
host and API/WebSocket/health path allowlists, preserves legitimate cookies,
Authorization and media bodies, strips client-supplied forwarded-IP/scheme
headers, inserts a shared hop token, and streams HTTP and WebSocket traffic.
The backend `SecurityRelayBoundary` authenticates both protocols before Django
or Channels handles them. In production, `SECURITY_RELAY_REQUIRED=True` and a
32+ character `SECURITY_RELAY_TOKEN` are mandatory; settings fail closed if the
token is missing or the boundary is disabled. Gunicorn/Uvicorn's generic
proxy-header parsing is disabled. Only the authenticated relay hop may supply
the forwarded scheme.

The public browser `API_BASE_URL` must point at the relay. The backend must be
a private Render service, and its `ALLOWED_HOSTS` must contain only the public
relay hostname(s). Render Blueprints generate one shared token in an environment
group and wire the private upstream host/port. For custom public aliases, set
all exact names in the relay's `PUBLIC_HOSTS` and backend `ALLOWED_HOSTS`.

Start command — ASGI only; `config.wsgi` cannot serve WebSockets:

```bash
gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker -c gunicorn.conf.py
```

`gunicorn.conf.py` binds `0.0.0.0:$PORT` and does not trust arbitrary
`X-Forwarded-*` metadata. Relay scheme trust is authenticated at the ASGI
boundary. The Render public relay health check forwards `/health/live/` to the
private backend; `/health/ready/` checks MySQL, Redis cache/Channels and
private storage.

Redis is mandatory in production (channel layer, cache, presence) and boot
fails clearly without it; the in-memory layer is development-only. Use
`rediss://` for TLS, and `REDIS_SSL_CERT_REQS=none` only when a managed
provider's certificate chain is not verifiable.

Run migrations before switching traffic (`bin/render-build.sh` does this for
the backend service only, so parallel worker deploys cannot race).

Set a unique `SECRET_KEY`, shared relay token, database credentials, exact
`ALLOWED_HOSTS`, CORS/CSRF origins, `REDIS_URL`, private storage credentials
and VAPID keys per deployment. For the cross-site frontend, set
`COOKIE_SAMESITE=None` and `COOKIE_SECURE=True`. Provide FFmpeg/ffprobe to the
backend and media-worker hosts (`bin/render-build.sh` installs a static build
into `backend/bin/`). The upload finalizer also needs permission to list objects
under `media/staging/` so it can retry cleanup of superseded chunk objects;
scope that bucket-list permission to the staging prefix where supported.
Never share a database, bucket or credential set between organizations.

Privacy boundary: the relay necessarily sees its incoming network peer. It is
not an anonymity service. The relay does not forward peer IP headers to Django,
and security-event rows in relay-required mode store no browser or shared relay
IP. The relay token authenticates only the proxy hop; normal user authentication,
authorization, CSRF and WebSocket origin checks remain active.

Background processing runs as its own services, never inside the web service:

```bash
python manage.py push_worker
python manage.py media_worker
python manage.py finalize_uploads
```

## API

- `GET /api/auth/csrf/`, `/api/me/`, `/api/auth/sessions/`; `POST /api/auth/login/`, `/logout/`, `/refresh/`, `/change-pin/`; `DELETE /api/auth/sessions/{uuid}/`
- `/api/members/` plus `/{uuid}/activate/`, `/deactivate/`, and `/reset-pin/` (administrator only)
- `/api/conversations/`; `/api/conversations/{uuid}/messages/`; `/read/`
- `/api/messages/search/`; `/api/messages/{uuid}/`; `/api/messages/{uuid}/reaction/`
- `/api/groups/`; `/api/groups/{uuid}/members/`; `/api/groups/{uuid}/leave/`; `/api/groups/{uuid}/archive/`
- `GET /api/security/` and `GET /api/audit/` with administrator-only filtering
- `GET /api/unread/` for authoritative global and per-conversation unread counts
- `POST /api/media/`; `GET /api/media/{attachment_uuid}/`
- `POST /api/uploads/`; `/api/uploads/{uuid}/` for owner-only progress; `/api/uploads/{uuid}/part/`; `/api/uploads/{uuid}/complete/`
- `/api/notifications/`; `/api/notifications/read/`; `/api/push/`
- `GET/PATCH /api/settings/`; `POST /api/settings/branding/`; `GET /api/audit/` (administrator only)
- `GET /api/public/config/`; `GET /api/public/branding/logo/`; `/favicon/`
- `POST /api/client-errors/` for rate-limited, secret-redacted frontend error telemetry
- `GET /health/live/` and `GET /health/ready/` checking `database`, `cache`, `channels`, and `storage`
- WebSockets: `/ws/app/` (multiplexed), `/ws/conversations/{uuid}/`, and `/ws/presence/`, authenticated by an active access-cookie device session.

Responses use `{success,message,data}` or `{success,message,code,errors}`. Access cookies are short lived. A frontend should call refresh with credentials included and keep tokens out of JavaScript storage.

## Media and push

The models reserve private storage keys rather than public URLs. Relay-required production authorizes each media request and streams it through the protected `/api/media/` route; it never returns an object-store address. Non-relay deployments may issue short-lived signed URLs after conversation authorization. Direct and resumable upload completion validates server-detected signatures, extensions, image structure, and (when available) ffprobe stream kind/duration before message commit. Push subscriptions are persisted; delivery workers use VAPID environment secrets and deactivate HTTP 404/410 subscriptions.

## Security notes

PINs are only passed to Django's password hasher. They must never be logged. UUIDs do not replace object authorization. Conversation querysets and WebSockets both verify membership. Use TLS, Redis authentication, private object ACLs, request-size limits at the proxy, malware scanning, CSP at the frontend, database backups, key rotation and centralized redacted logs.

## Current scope boundary

This repository is a production-oriented core, not a claim that every item in the supplied 80-section specification is finished. Before launch, independently review direct-to-object-storage upload integration, operational monitoring, backup/restore drills, malware scanning, migration plans, and the complete acceptance/security/load-test matrix.
