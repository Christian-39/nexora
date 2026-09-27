# NEXORA — production fix report

Surgical changes only. No rebuild, no framework, no business-logic redesign.
30 paths touched (25 modified, 5 added, 2 deleted). Everything below is verified
by an automated test or a live run recorded at the end of this document.

---

## 1. Root causes

| # | Symptom | Real cause | Fix |
|---|---|---|---|
| 1 | Render boot: `ImproperlyConfigured: REDIS_URL is mandatory in production` | No Redis was attached to the service. The guard was correct. | External managed Redis wired through `REDIS_URL` (`render.yaml`, `.env.example`, settings validation). Guard kept and now covered by tests. |
| 2 | Endless "Reconnecting…" on Vercel | `resolveApiOrigin()` returned `''` for any non-local hostname, so the hosted page asked **Vercel** for `/api/*` and `wss://nexora-eight-lilac.vercel.app/ws/app/`. Every socket died instantly. | `PRODUCTION_API_ORIGIN` fallback in `config.js`; the WS origin is derived from it. |
| 3 | Reconnect loop even against the right host | The consumer called `close(4003)` **before** `accept()`. A rejected handshake reaches the browser as code `1006` — indistinguishable from a network blip — so the client retried forever with a dead session. | Backend accepts, sends a typed `auth.error`, then closes 4401/4403. Client treats those as terminal. |
| 4 | Retry storm when the backend was simply down | A failed `refreshSession()` could not be told apart from an unreachable backend, so a transient outage looked like a sign-out (and vice-versa). | `refreshSession()` now returns `{ok, reason: 'ok' \| 'rejected' \| 'unreachable'}`; `rejected` → sign out once, `unreachable` → backoff. |

---

## 2. Files changed and why

### Backend

| File | Change |
|---|---|
| `backend/config/settings.py` | Render host in `ALLOWED_HOSTS`; `_clean_origins()` normalises CORS/CSRF values (strips paths/trailing slashes, seeds CSRF from CORS); `REDIS_URL` scheme validation + `REDIS_SSL_CERT_REQS` for managed TLS; expanded, actionable Redis error; refuses `SameSite=None` without `Secure`; `WEBSOCKET_ALLOWED_ORIGINS` derived from CORS + local dev origins. The production Redis guard is untouched. |
| `backend/config/asgi.py` | Rewritten: `ProtocolTypeRouter` with an explicit `OriginValidator` seeded from `WEBSOCKET_ALLOWED_ORIGINS`. **Not** `AllowedHostsOriginValidator` — `ALLOWED_HOSTS` is the Render host, so it would reject the Vercel origin and recreate the loop. |
| `backend/config/__init__.py` | `pymysql.install_as_MySQLdb()` so MySQL works on a managed host with no build toolchain for `mysqlclient`. |
| `backend/config/gunicorn.conf.py` | Binds `0.0.0.0:$PORT` (a fixed port fails Render's health check), `UvicornWorker`, `WEB_CONCURRENCY`, forwarded-proto trust, long WS-friendly timeout. |
| `backend/apps/accounts/ws_auth.py`, `apps/conversations/consumers.py` | Accept-then-close protocol with typed reasons `NO_CREDENTIAL`, `SESSION_REVOKED`, `TOKEN_EXPIRED`, `FORBIDDEN`; close 4401 (auth) / 4403 (origin or membership). `disconnect()` hardened — a rejected socket reaches it before `typing_tasks` exists and used to raise `AttributeError`. |
| `backend/requirements.txt` | `PyMySQL`, `uvicorn[standard]`, `gunicorn` pinned for the non-Docker host. |
| `backend/bin/render-build.sh` *(new)* | Non-Docker build: deps, static FFmpeg/ffprobe into `backend/bin/`, `collectstatic`, `migrate` (web only — `--no-migrate` for workers so parallel deploys cannot race). |
| `render.yaml` *(new)* | Blueprint: ASGI web service + `push_worker`, `media_worker`, `finalize_uploads` background workers; all secrets `sync: false`. |
| `backend/Dockerfile`, `backend/docker-compose.yml` | **Deleted.** No Docker remains; Redis/MySQL/S3 stay as external managed services. |
| `backend/.env.example`, `backend/README.md`, `README.md` | Documented the Render + Vercel topology, mandatory external Redis, TLS options, workers, and the ASGI start command; all compose/container prose removed. |

### Frontend

| File | Change |
|---|---|
| `assets/js/config.js` | Single source of truth. Resolution order: runtime override → `nexora-api-base` meta (`same-origin` ⇒ `''`) → local dev (same hostname:8000) → `''` if the page is already on the API origin → `PRODUCTION_API_ORIGIN`. Exports `apiConfig.wsOrigin` via `toWebSocketOrigin()` (`http→ws`, `https→wss`). |
| `assets/js/api.js` | Consumes `apiConfig`; `refreshSession()` returns a reason instead of a bare boolean. |
| `assets/js/websocket.js` | Explicit state machine `idle → connecting → open → reconnecting → offline → closed`; single socket, single timer, listeners bound once (epoch guard kills late callbacks from superseded sockets); bounded backoff with jitter, `MAX_RECONNECT_ATTEMPTS=10` → `closed{RETRY_LIMIT}`; heartbeat 25 s / 12 s timeout; one-shot refresh (`#refreshUsed`) on auth failure, then stop and go to login; `stop()` is terminal and clears everything; `offline`/`online` handling with exactly one controlled attempt on recovery. |
| `assets/js/auth.js` | `realtime.stop()` on logout and on terminal auth failure — no socket outlives the session. |
| `assets/js/navigation.js` | Injects `<header class="app-header">` with the hamburger (≤767 px) and the profile + theme controls top-right; accessible drawer (`role="dialog"`, `aria-modal`, backdrop, focus trap, Esc / backdrop / item close, focus restore, safe-area insets); role-aware items; desktop sidebar unchanged. Profile/theme removed from the nav footer; the theme control reuses the existing `theme.js` module. |
| `assets/js/ui.js` | Toast region moved to the top, offset by `--app-header-h`, clean stacking. One notification system — the existing notification centre, unread counts, WS events and PWA badge are untouched. |
| `assets/css/main.css`, `components.css`, `responsive.css` | Header/drawer/toast styles; ≤767 px hides `.app-nav` and shows the menu button; 768–1023 px icon rail; ≥1024 px unchanged. No global `overflow-x: hidden` — the overflowing rules were fixed at source. |
| `sw.js` | `VERSION='v1.2.0'`; `/api/*` network-only (never cached); code assets network-first so a deploy is live immediately; images stale-while-revalidate; no private media or auth responses cached. |
| `vercel.json` *(new)* | `must-revalidate` for HTML/JS/CSS/`sw.js` (+ `Service-Worker-Allowed: /`), 7-day images, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, and a `Permissions-Policy` that deliberately does **not** block `microphone` (voice notes). |

### Tests (added, none deleted)

| File | Count |
|---|---|
| `backend/tests/test_deployment_config.py` *(new)* | 19 — Redis channel layer/cache selected, missing/malformed `REDIS_URL` fails, `rediss://` + `REDIS_SSL_CERT_REQS`, dev-only in-memory layer, CORS/CSRF normalisation, `SameSite=None; Secure` (and refusal without Secure), Render host, MySQL + private storage required, ASGI entry point, no Docker artefacts, `render.yaml`/`gunicorn.conf.py` assertions. |
| `backend/tests/test_websocket.py` | +4 and 2 rewritten — accept-then-`auth.error` protocol, revoked session, foreign origin 4403, configured Vercel origin accepted, `/ws/app/` routing. |
| `backend/tests/test_frontend_contract.py` | +13 — config.js sole owner of the origin, WS derived from it, reconnect markers, drawer a11y markers, header owns profile/theme, role-aware items, top-anchored toasts, single toast system, no global `overflow-x`, mobile breakpoint. |
| `frontend/tests/config.test.mjs`, `websocket.test.mjs` *(new)* | 21 — URL resolution local/prod/override, and the full reconnect lifecycle including refresh-once, rejected refresh, unreachable backend, `stop()`/`start()`, offline/online, idempotency. |

---

## 3. Render environment variables

```
DJANGO_ENV=production
DEBUG=False
SECRET_KEY=<64+ random chars>                       # secret
ALLOWED_HOSTS=nexora-f397.onrender.com
CORS_ALLOWED_ORIGINS=https://nexora-eight-lilac.vercel.app
CSRF_TRUSTED_ORIGINS=https://nexora-eight-lilac.vercel.app
COOKIE_SAMESITE=None
COOKIE_SECURE=True
SECURE_SSL_REDIRECT=True
REDIS_URL=rediss://:<password>@<host>:<port>        # secret, external managed Redis
REDIS_SSL_CERT_REQS=                                # "none" only if the chain is unverifiable
DATABASE_URL=mysql://user:pass@host:3306/nexora     # secret
STORAGE_BUCKET / STORAGE_ENDPOINT / STORAGE_REGION
STORAGE_ACCESS_KEY / STORAGE_SECRET_KEY             # secret
PUSH_PUBLIC_KEY / PUSH_PRIVATE_KEY / PUSH_CONTACT   # secret
MEDIA_PROCESS_INLINE=False
WEB_CONCURRENCY=2
PYTHON_VERSION=3.12.6
FFMPEG_BINARY=/opt/render/project/src/backend/bin/ffmpeg
FFPROBE_BINARY=/opt/render/project/src/backend/bin/ffprobe
```

The three worker services take the **same** variables (share an env group).
No secret is committed anywhere; `render.yaml` marks them `sync: false`.

## 4. Build and start commands

| Service | Build | Start |
|---|---|---|
| Web (ASGI) | `./bin/render-build.sh` | `gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker -c gunicorn.conf.py` |
| Push worker | `./bin/render-build.sh --no-migrate` | `python manage.py push_worker` |
| Media worker | `./bin/render-build.sh --no-migrate` | `python manage.py media_worker` |
| Upload finalizer | `./bin/render-build.sh --no-migrate` | `python manage.py finalize_uploads` |

`rootDir: backend` for all four. Health check path: `/health/`.
Do **not** use `config.wsgi` — it cannot serve WebSockets.

## 5. Redis configuration

* External managed instance (Render Key Value, Upstash, Redis Cloud, ElastiCache…).
  It backs the channel layer, the cache and presence.
* `redis://` or `rediss://`. A value with no scheme is rejected at boot.
* With TLS on a provider whose chain the host cannot verify: `REDIS_SSL_CERT_REQS=none`
  (still encrypted).
* Same instance for the web service and all workers, otherwise events do not
  cross process boundaries.
* Production never falls back to the in-memory layer; that path is
  development-only and asserted as such by `test_deployment_config.py`.
* Sizing: `maxmemory-policy noeviction` (or `volatile-ttl`) — channel groups and
  presence keys already carry TTLs.

## 6. Manual deployment steps

1. **Provision** external MySQL 8, external Redis and a private S3-compatible
   bucket. Copy their URLs/credentials.
2. **Generate VAPID keys:**
   `python -c "from py_vapid import Vapid01; v=Vapid01(); v.generate_keys(); print(v.public_key, v.private_key)"`
3. **Backend on Render** — Blueprint (`render.yaml`) or manually: new *Web
   Service* → repo → `rootDir: backend` → build/start from §4 → paste §3
   variables → deploy. First boot runs migrations. Verify:
   `curl -i https://nexora-f397.onrender.com/health/` → `200`.
4. **Create the first administrator** (Render Shell):
   `python manage.py createsuperuser --phone +234…`
5. **Workers** — three *Background Workers*, same repo/rootDir, commands from
   §4, same environment group.
6. **Frontend on Vercel** — import the repo, root directory `frontend`,
   framework *Other*, no build command, output `frontend`. `vercel.json` is
   picked up automatically. No file edits are needed: `config.js` resolves to
   the Render backend.
7. **Verify in a browser** on `https://nexora-eight-lilac.vercel.app`:
   sign in, DevTools → Network → WS shows `wss://nexora-f397.onrender.com/ws/app/`
   status 101; send a message in two tabs; toggle airplane mode and back —
   the banner goes `offline → reconnecting → connected` and settles.
8. **If the frontend moves to another domain**, update `CORS_ALLOWED_ORIGINS`
   and `CSRF_TRUSTED_ORIGINS` and redeploy the backend — nothing else changes.

---

## 7. Verification performed

* `pytest` (backend, whole suite): **132 passed**.
* `node --test frontend/tests/`: **21 passed, 0 failed**.
* `manage.py check --deploy` with the full production environment: boots clean;
  the only warnings are `SECURE_SSL_REDIRECT` and `SECURE_HSTS_PRELOAD`, both
  set by `render.yaml`/env in the real deployment.
* Live run — Gunicorn + `UvicornWorker` + `config.asgi:application`, requests
  from a cross-origin page:
  * `GET /api/auth/csrf/` → 200; `POST /api/auth/login/` → 200 with HttpOnly
    `nexora_access` (Path=/) and `nexora_refresh` (Path=/api/auth/); `GET /api/me/` → 200.
  * `ws://…/ws/app/` with cookies → **`connection.ready`**, ping answered.
  * without cookies → **`{"type":"auth.error","code":"NO_CREDENTIAL"}` then close 4401**
    (the fix that ends the infinite reconnect).
  * foreign `Origin` → **rejected 403**; allowed origin → accepted.
* Live DOM run (jsdom) of the real `navigation.js` / `ui.js`:
  header rendered, hamburger `aria-controls="app-drawer"` / `aria-expanded`
  toggling, drawer items role-aware
  (`Dashboard | Chats | Groups | Members | Profile | Settings | Appearance | Sign out`
  for an admin, without `Dashboard`/`Members` otherwise), backdrop present,
  Esc and backdrop close it and focus returns to the button, profile menu shows
  name · role + Profile/Settings/Sign out, nav footer no longer contains
  profile/theme, toasts stack in the top region, connection banner starts idle
  (not stuck on "Reconnecting").
* Static checks: no `onrender.com`/`vercel.app` outside `config.js`, no
  `console.log` in shipped JS, no Docker artefacts, no global `overflow-x: hidden`.
