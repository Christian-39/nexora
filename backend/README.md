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
- Non-containerised deployment assets (`render.yaml`, `bin/render-build.sh`, `gunicorn.conf.py`) for an ASGI web service plus separate push/media/upload background workers, against external managed MySQL, Redis and object storage, with liveness/readiness probes. This project does not use Docker.

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

Static frontend (Vercel, or any static host / reverse proxy) → HTTPS → **ASGI** web service → independent MySQL 8 database + **external managed Redis** + private S3-compatible bucket. No Docker is involved at any layer.

Start command — ASGI only; `config.wsgi` cannot serve WebSockets:

```bash
gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker -c gunicorn.conf.py
```

`gunicorn.conf.py` binds `0.0.0.0:$PORT` so a platform-assigned port is honoured, and trusts `X-Forwarded-Proto` so TLS termination upstream is detected.

Redis is mandatory in production (channel layer, cache, presence) and the boot fails clearly without it; the in-memory layer is development-only and can never be substituted in production. Use `rediss://` for TLS, and `REDIS_SSL_CERT_REQS=none` only when a managed provider's certificate chain is not verifiable.

Run migrations before switching traffic (`bin/render-build.sh` does this for the web service only, so parallel worker deploys cannot race).

Set a unique `SECRET_KEY`, database credentials, exact `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, `REDIS_URL`, private storage credentials and VAPID keys per deployment. When the frontend is on another site, also set `COOKIE_SAMESITE=None` and `COOKIE_SECURE=True`, or the browser will not send the session cookies. Provide FFmpeg/ffprobe to the API and media-worker hosts (`bin/render-build.sh` installs a static build into `backend/bin/`). Never share a database, bucket or credential set between organizations.

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
