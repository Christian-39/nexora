# NEXORA — Follow-up production performance investigation and safe infrastructure fix

**Date:** 2026-09-28 (UTC times below) · **Source:** `Christian-39/nexora` @ `bcc60c1` · **Branch delivered:** `fix/perf-follow-up` — commits `19c971f` (performance) and `d913da2` (320 px chat header fix) (+ `0001-Performance-follow-up-….patch`)
**Production probed with the supplied test account:** backend `nexora-f397.onrender.com`, frontend `nexora-eight-lilac.vercel.app`.

> **Update (second pass, same day).** Three claims in the first version of this report have been corrected by further measurement, and the corrections are in §10–§12:
> 1. The "critical standing defect" RC-7 (message send + authenticated WebSocket returning 500) was **transient** — both work in production now; they were collateral of connection-quota exhaustion. No Render log hunt is needed.
> 2. Phase 12 was recorded as "cannot be executed — no browser". It has now been **executed for real** in headless Chromium across all ten required widths, and it found a genuine 320 px overflow, now fixed.
> 3. A `select_for_update()` defect I suspected in the send path **does not exist**; it is guarded by `@transaction.atomic`. Recorded in §12 with the evidence, plus the regression test added to stop that guarantee from being removed silently.

> **Honesty note.** Everything below marked *measured* was actually executed, either against live production or against a full local reproduction (real MySQL wire protocol + real Redis + the exact production ASGI stack `gunicorn -k UvicornWorker`, behind a TCP proxy injecting a controlled database RTT). I have **no deploy access**, so post-deploy production numbers cannot exist yet; the "after" columns are from the local reproduction that matched production *before* numbers within ~10%. No number in this report is invented.

---

## 1. Confirmed root causes (with evidence)

### RC-1 — Persistent DB connections do not work under ASGI: every request pays a full remote MySQL handshake ~1.9 s  ·  SEVERITY: CRITICAL · FIXED (code)
* Production: `/health/ready/` (exactly one `SELECT 1` + one Redis set/get) took **1.81–2.13 s on three consecutive warm requests** — no warm-up effect, so no connection reuse. Requests with zero SQL (bad-cookie 401, warm `/api/public/config/`, CSRF) took **0.06–0.12 s** on the same instance.
* Local reproduction (150 ms RTT proxy, `CONN_MAX_AGE=300`, HEAD code): instrumentation logged **`newconn=2` on every request**, each fresh connect costing **1.30–1.53 s** (TCP+TLS+MySQL auth+init ≈ 8–10 round trips).
* Mechanism (proved by stack traces): Django's ASGI handler wraps each request in an asgiref `ThreadSensitiveContext` whose single-use executor thread is destroyed at request end. `CONN_MAX_AGE` connections are **thread-local**, so they die with the thread. The second connection per request is Django's MySQL backend re-discovering `mysql_server_data` (`SELECT VERSION()` **on a temporary extra connection**) because that cache is per-wrapper-instance and ASGI creates a fresh wrapper per request.
* **Availability side-effect (observed live):** at ~06:45 UTC, after my measurement traffic (~100+ fresh connections/hour), **every DB-touching production endpoint began returning fast 500s** (`/health/ready/` 500 in 0.12 s; `live` and Redis-cached endpoints stayed 200), recovering by ~06:52. This is the signature of a shared-host MySQL connection quota (`max_connections_per_hour` / `max_user_connections`) being exhausted by the churn. The churn is not just latency — it is an outage mechanism.

### RC-2 — Geographic separation of backend and database (~150 ms/round-trip)  ·  SEVERITY: HIGH · NOT FIXABLE IN CODE; MIGRATION RECOMMENDED
* Each sequential SQL query costs one RTT. With the DB host secret (Render dashboard) I could not name the datacenter, but the behavioral fit is exact: replaying production traffic locally with a 150 ms RTT proxy reproduced every production endpoint within ~10% (e.g. `/api/me/` prod 3.51–3.91 s vs replica 3.22–3.49 s; conversations prod 4.38–5.04 s vs replica 4.17–4.24 s).
* Even after all code fixes, at 150 ms RTT the floor is ~0.45 s per DB-touching request (pool checkout ping + autocommit + ≥1 query). At 2 ms RTT the same code serves every endpoint in **18–62 ms**.

### RC-3 — Per-request query overhead in the auth path + a real N+1  ·  SEVERITY: MEDIUM · FIXED
* Every authenticated request did **2 queries before any endpoint work** (user fetch + session existence). Merged into **1** (`DeviceSession` joined to `User`; every security check preserved).
* `/api/notifications/` had a genuine **N+1**: `conversation_id = CharField(source="conversation.id")` loaded the full related conversation per row (5 extra SELECTs for 5 notifications, measured). Fixed by reading the local FK column. Conversations list was already well batched (verified — no blind optimizations added).

### RC-4 — PBKDF2 at 1,000,000 iterations dominates login CPU  ·  SEVERITY: MEDIUM · FIXED
* Measured **256 ms verify on a fast sandbox CPU**; on Render's 0.5 vCPU starter this scales to roughly 1–2.5 s, matching prod login (5.35–6.26 s) vs the replica (3.17 s on fast CPU). Switched the preferred hasher to **Argon2id at OWASP parameters (m=19456 KiB, t=2, p=1)** — equivalent-or-better security, tens of ms to verify; PBKDF2 remains valid and hashes upgrade transparently on next login.

### RC-5 — Frontend chat-boot waterfall  ·  SEVERITY: HIGH · FIXED
* Measured production chain: `/api/me/` 3.85 s → conversations 5.04 s → messages 4.54 s = **13.4 s sequential** — the reported ~12.6 s chat boot is exactly this waterfall, not chat rendering.
* Fixes: (a) `bootstrap()` now resolves from the tab-scoped verified profile snapshot and revalidates `/api/me/` in the background (backend stays authoritative; a background 401 clears the snapshot and redirects; the login page requests `{fresh:true}`); (b) chat boot prefetches the likely initial thread's detail + history **in parallel** with the conversation list — `api.js` in-flight GET dedup plus a new coalescing `loadLatest()` guarantee `open()` joins those exact requests. Nothing with a genuine data dependency was parallelized: the *list* request never waits on anything, and thread prefetch only fires when the thread id is already known (deep link / remembered).

### RC-6 — Cold start ≈ hosting, not code  ·  SEVERITY: HIGH · HOSTING-LEVEL
* Reproduced live: first request after idle **37.86 s TTFB**, immediately followed by 0.13–0.14 s warm requests.
* App boot measured locally: `import django` 24 ms, `django.setup()` 356 ms, **gunicorn exec → first 200 in 0.75 s** (2 workers). Even ×5 on a slow vCPU that is <4 s. Migrations run at *build* time (`render-build.sh`), not at boot. **≥ 30 s of the cold start is Render instance wake** (free-plan spin-down behavior; `render.yaml` says `plan: starter`, so verify what the real service uses — an always-on paid instance removes this entirely).

### RC-7 (RESOLVED — see the correction below) — message send and authenticated WebSocket returned HTTP 500  ·  SEVERITY: was CRITICAL · **TRANSIENT, NOT A STANDING DEFECT**
* What was seen: during the production quota incident, `POST …/messages/` returned **500** (twice, ~3.7 s) and the authenticated `wss://…/ws/app/` upgrade returned **HTTP 500**, while anonymous WS worked.
* **Re-tested after the database recovered, with REST demonstrably healthy** (see §10): authenticated `wss://…/ws/app/` → **handshake OK in 3.94 s**, `connection.ready` frame received with presence payload; `POST …/messages/` → **HTTP 201 in 8.13 s**, message persisted (`f0f1ec9c…`, `delivery.recipients: 1`).
* Conclusion: both failures were **collateral of RC-1/RC-4 connection-quota exhaustion**, not independent bugs. The discriminator that looked damning — "anonymous WS works, authenticated WS fails" — is explained by the auth middleware: the anonymous path returns `NO_CREDENTIAL` **before issuing any query**, so it is the only request shape on the whole surface that needs zero database connections. Once connections were available again, the authenticated path worked unchanged.
* Operator consequence: **no separate fix is required, and no Render log hunt is needed.** Removing the connection churn (RC-1) removes the condition that produced these 500s. This entry is kept rather than deleted because the earlier version of this report called it a critical standing defect, and that was wrong.

---

## 2. Deployment topology (Phase 1 — verified where possible)

```
Browser
  ↓ HTTPS            Vercel CDN edge (x-vercel-id showed the PoP nearest the client; static only)
Vercel (static frontend)
  ↓ HTTPS/WSS        cross-origin to Render
Render web service   nexora-f397.onrender.com (216.24.57.x, Gunicorn+UvicornWorker+Channels,
  |                  WEB_CONCURRENCY=2, healthCheckPath /health/ready/)
  ├── Redis          NEAR the backend — measured: Redis-cached responses & throttle ops 0.06–0.12 s total
  ├── MySQL          FAR from the backend — measured: ~1.9–2.1 s per fresh connection, ~150 ms per query RTT;
  |                  behaves like shared hosting with per-hour connection quotas (outage observed)
  └── S3-compatible private bucket (region in dashboard secrets; not exercised by these endpoints)
```
The DB/Redis/bucket hostnames are `sync:false` dashboard secrets and appear nowhere in the repo or DNS I can query, so provider/city names could not be independently confirmed — only their latency behavior. Nothing in the request path calls other external APIs (push is currently disabled in config).

## 3. Phase 2/3 latency decomposition (measured)

| Component | Measured |
|---|---:|
| Python/middleware/JWT/renderer (no SQL) | 0.06–0.12 s |
| Redis (cache hit + throttle) | included above — negligible |
| Fresh MySQL connection (TCP+TLS+auth+init) | ~1.3–1.5 s at 150 ms RTT (local), ~1.8–2.1 s prod |
| Each sequential SQL query | ~0.15 s (RTT-bound; server execution itself trivial) |
| Serializer/app CPU | ~0.01–0.15 s |
| **Verdict** | **L: combination — G (connection churn) × A (geography) dominate; C (sequential queries × RTT) second; B/F minor (auth 2→1, notifications N+1); K (frontend waterfall) multiplies it all ×3 at chat boot; no slow queries, no missing indexes found (all SQL trivially fast server-side; cursor pagination, no COUNT(*) on lists)** |

Per-endpoint steady-state SQL counts (local, production settings):

| Endpoint | Queries before | after | Notes |
|---|---:|---:|---|
| `/api/me/` | 2 | **1** | auth merge |
| `/api/conversations/` | 8 | **7** | already batched (subqueries + one page-wide latest-message + `cache.get_many` presence) |
| `/api/notifications/` | 8 (N+1) | **2** | FK column fix + auth merge |
| unread-count / unread-summary | 4 | **3** | |
| `/api/groups/`, `/api/members/` | 3 | **2** | |
| login | 8 + 2 hash ops | 8, cheap hash | writes are RTT-bound |
| ASGI extras per request | +`SELECT VERSION()` on an extra connection | **0** (process-wide cache) | |

## 4. Before/after measurements (Phase 15)

**Production (real, pre-deploy = "Before"):** min/avg/max over 3–4 samples.
**After columns are the local replica** (same ASGI stack, real MySQL protocol) at the stated DB RTT — the replica's *before* numbers matched production within ~10%, and they are labeled as such, not claimed as production.

| Operation | Prod before (min–max) | After, DB still remote (150 ms RTT) | After + same-region DB (2 ms RTT) |
|---|---:|---:|---:|
| Login | 5.35–6.26 s | 1.87–2.80 s | **0.08 s** |
| `/api/me/` | 3.51–3.91 s | 0.47 s | **0.02–0.03 s** |
| Conversations | 4.38–5.04 s | 1.39–1.40 s | **0.05–0.06 s** |
| Messages page (30) | 4.54 s | ~1.4 s | **0.08 s** |
| Chat boot (API chain) | **13.4 s measured** (≈ reported 12.6 s) | **1.88 s** (parallelized) | **0.11 s** |
| Notifications | 4.13–4.48 s | 0.62 s | **0.02 s** |
| Unread count | 3.91–4.09 s | 0.77 s | **0.02 s** |
| Groups | 3.72–3.99 s | 0.62 s | **0.02 s** |
| Members | 3.87–3.97 s | 0.62–1.30 s | **0.02 s** |
| Profile prefs | 3.70–3.81 s | ~0.5 s | **0.02 s** |
| Send message | HTTP **500** (prod defect, RC-7) | 0.12 s (local 201) | 0.12 s |
| WS connect→ready | HTTP **500** (prod defect, RC-7) | 39 ms + first frame 19 ms | same |
| Cold start | 37.86 s | unchanged — hosting | unchanged — hosting |

## 5. What was changed (code)

Backend — `backend/…`
* `config/db_pool_backend/` (new): process-wide SQLAlchemy pool (`dj-db-conn-pool`, PRE_PING, RECYCLE 280 s) + process-wide `mysql_server_data` cache. Engine switched only for MySQL, only outside tests; `CONN_MAX_AGE` forced to 0 under the pool; sizing deliberately small (3+5/process) for shared-host `max_user_connections`, env-tunable.
* `config/settings.py`: pool wiring; `DATABASE_CONN_HEALTH_CHECKS` (default on) for the non-pooled path; `PASSWORD_HASHERS` → tuned Argon2id first (`config/hashers.py`), PBKDF2 kept for existing hashes.
* `apps/accounts/authentication.py`: 1-query session+user auth; identical security semantics (token-user binding via `user_id` claim in the WHERE clause, revocation/expiry, `is_active`, CSRF double-submit).
* `apps/notifications/serializers.py`: N+1 removed.
* `requirements.txt`: `django-db-connection-pool`, `SQLAlchemy`, `argon2-cffi`.
* `tests/test_deployment_config.py`: contract updated — production engine must be the pooled MySQL backend with PRE_PING and `CONN_MAX_AGE=0`.
* `config/settings_bench.py` + `apps/core/bench_middleware.py`: measurement tooling only; inert unless explicitly loaded.

Frontend — `frontend/…`
* `assets/js/auth.js`: cached-session fast path + background revalidation; `session-expired` emitted if a background 401 invalidates a page that proceeded from cache; login page uses `{fresh:true}`.
* `assets/js/messages.js`: `loadLatest()` coalesces concurrent callers (prerequisite for safe prefetch).
* `assets/js/chat.js`: initial-thread prefetch parallel to the list; `open()` joins the prefetch instead of re-fetching.
* `sw.js`: `v1.4.1` cache bump so deployed clients pick up the new modules.

**Not changed (deliberately):** no permission checks removed, no public caching of authenticated responses, no service-worker caching of private data, no global `overflow-x:hidden`, no blind `prefetch_related`, no new indexes (no query pattern needed one — every slow query was RTT-bound, not execution-bound).

## 6. Tests performed

| Check | Result |
|---|---|
| Backend pytest (hermetic, as previous audits) | **156 passed / 0 failed** (155 baseline + 1 new transactional-invariant guard) |
| Frontend `node --test` | **58 passed / 0 failed** |
| `manage.py check`, `makemigrations --check` | clean / no drift |
| Local end-to-end on the real ASGI stack | login, me, conversations, detail, messages, send (201), WS connect→`connection.ready`→ping/pong |
| Production (test account) | login, me, conversations, messages, notifications, unread, groups, members, prefs measured; send + auth'd WS exposed RC-7; anonymous WS handshake verified |
| Chat UI (Phase 12) | **Executed in a real browser** (headless Chromium) at 320/360/375/390/393/414/430/768/1024/1280 px with every message type. Found and fixed a real 320 px overflow (§11). After the fix: no element crosses the viewport at any width; `.bubble__tools` (reply + three-dot) unclipped, non-overlapping, 34×34; actions menu stays on-screen incl. 320×568. No global `overflow-x` hack. |

## 7. Is the database migration actually necessary?

**Yes — but it is the third fix, not the first.** Do not overclaim: moving the database will NOT fix the 500s on send/WebSocket (RC-7), the cold start (RC-6), or the frontend waterfall (RC-5), and before pooling it wouldn't have fixed the churn cost either. The measured contributions to a 4 s endpoint were roughly: ~1.9 s connection churn (fixed in code), ~0.6–1.8 s query RTTs (halved by code, removed only by geography), ~0.1 s app. After the code fixes, geography remains the difference between a 1.4–1.9 s and a 0.05–0.11 s chat experience, and the shared-host connection quotas remain an availability risk. **Recommended order: deploy code fixes → verify → migrate DB to the backend's region (managed MySQL, not shared hosting) → verify again.**

### Safe migration plan (Phase 8 — no production data touched today)
1. Provision managed MySQL 8 **in the Render service's region**; confirm `utf8mb4` + `utf8mb4_unicode_ci` server defaults and generous `max_user_connections`.
2. Freeze schema changes; take a full dump from the current DB: `mysqldump --single-transaction --routines --triggers --events --hex-blob --default-character-set=utf8mb4 --set-gtid-purged=OFF`.
3. Verify the backup restores into a scratch DB (row counts + spot checks) **before** touching anything else.
4. Restore into the new instance; re-run `manage.py migrate` (must be a no-op) to confirm schema identity, FKs, indexes and constraints.
5. Verify per table (users, device sessions, conversations, participants, messages, receipts, reactions, deletions, attachments/media, notifications, push subscriptions, groups, memberships, platform settings, audit, security events, token blacklist): `SELECT COUNT(*)` source vs destination, plus `CHECKSUM TABLE` and referential spot-joins (messages→conversations, receipts→messages, participants→users).
6. Cutover in a short maintenance window: stop web+workers → final incremental dump/restore of rows changed since step 2 (`updated_at`/PK ranges) → re-verify counts → switch `DATABASE_URL` on **all four services** → start → run the endpoint smoke matrix above.
7. Keep the old database untouched and readable for ≥ 2 weeks; roll back by switching `DATABASE_URL` back.

## 8. Remaining bottlenecks & required operator actions
1. **Deploy the branch** (Vercel + Render; new SW `v1.4.1` prompts once). Then re-measure production against §4 — the 150 ms-RTT column is the honest expectation while the DB stays remote.
2. ~~Pull the Render logs for RC-7~~ — **no longer required.** Message send and authenticated WebSocket were re-verified working in production (201 / `connection.ready`); those 500s were quota collateral, not a standing defect (§10).
3. **Always-on instance** (RC-6): confirm the real plan; free instances sleep ≈15 min and wake in 30–60 s. No code change can hide that from a login.
4. **Regional DB migration** per §7.
5. Verify the DB provider's connection quotas; keep total pool size across the 4 services under `max_user_connections`.
6. Push remains disabled in production config (`push:false`, null VAPID) — unchanged from the previous audit's finding.

## 9. Phase 16 classification

| # | Bottleneck | Level | Evidence | Severity | Fixed? | Migration needed? |
|---|---|---|---|---|---|---|
| 1 | ASGI connection churn (2 handshakes/request) | CODE | `newconn=2` logs, ready 1.8–2.1 s, stack traces | Critical | **Yes (pool)** | No |
| 2 | Auth 2-queries/request, notifications N+1, `SELECT VERSION()`/request | CODE/DATABASE | captured SQL | Medium | **Yes** | No |
| 3 | PBKDF2 1M iterations on 0.5 vCPU | CODE | 256 ms on fast CPU; prod-vs-replica login gap | Medium | **Yes (Argon2id)** | No |
| 4 | DB ~150 ms from backend; shared-host quotas | NETWORK/REGION | latency replay match ±10%; observed quota outage | High | No (impossible in code) | **Yes** |
| 5 | 37.9 s cold start | HOSTING | 0.75 s app boot vs 37.86 s TTFB | High | No | Plan change, not DB |
| 6 | Chat boot = 3-leg request waterfall | FRONTEND WATERFALL | 13.4 s measured chain | High | **Yes (parallelized)** | No |
| 7 | Chat rendering / action-control wrap | CHAT-RENDERING | Real-browser matrix at 10 widths, all message types | Low | **Held** (prior fix intact) | No |
| 7b | Chat header name overflowed 2.6 px at 320 px | CHAT-RENDERING/CODE | `.thread__name` inline `<span>` → ellipsis inert; measured right edge 322.6 px | Low | **Yes (`display:block`)** | No |
| 8 | Send message + auth'd WS = HTTP 500 | NETWORK/HOSTING (symptom of #4) | Re-tested after recovery: WS `connection.ready`, send 201 — both healthy | was Critical, now **transient** | **Yes, via #1/#4** | No |

---

## 10. Follow-up verification session (post-recovery, production)

All numbers below are single measurements taken against production **after** the database recovered, with deliberately minimal traffic (each request still costs two fresh DB connections until the pooling fix is deployed). They are "before" numbers — the fixes are not deployed.

| Probe | What it isolates | Time | Result |
|---|---|---|---|
| `POST /api/auth/login/` | Argon2/PBKDF2 + DB | 6.01 s | 200 |
| `PATCH /api/me/preferences/` | **DB write, no channel layer** | 4.19 s | 200 |
| `POST /api/conversations/{id}/typing/` | **channel layer only, no DB write** | 4.94 s | 200 |
| `POST /api/conversations/{id}/read/` | **DB writes + channel-layer fan-out** | 4.92 s | 200 |
| `wss://…/ws/app/` (authenticated) | WS auth (2 DB queries) + `group_add` | 3.94 s | **handshake OK, `connection.ready`** |
| `POST /api/conversations/{id}/messages/` | full send: write + receipts + notify + broadcast | 8.13 s | **201, persisted** |
| `GET /api/settings/` | admin read | 3.99 s | 200 |
| `GET /api/conversations/{id}/` | conversation detail | 6.31 s | 200 |

What this adds to the diagnosis:

* **Writes are not the problem; round-trips are.** A pure DB write (4.19 s) costs the same as a pure read, and the channel-layer-only endpoint (4.94 s) — which performs *no* DB work of its own — costs the same again, because it still pays the authentication connection handshake. The send path (8.13 s) is the slowest endpoint on the surface precisely because it is the one that chains the most sequential work behind that handshake.
* **Redis is healthy, including writes.** `UserRateThrottle` is a *global default* throttle, so every one of those 200s already performed a Redis `cache.set` on the same instance the channel layer uses. This is what rules out the "managed Redis is rejecting writes" hypothesis for the earlier WS failure.
* Cache/CDN and no-SQL paths remain 0.06–0.16 s (e.g. a 404 in 0.16 s), so the flat ~4 s floor is entirely the DB connection path — consistent with RC-1.

## 11. Phase 12 executed for real: browser responsive matrix

The previous version of this report stated that a real-browser matrix could not be run. That was resolved: headless Chromium (Playwright) was installed and the actual frontend was driven at **320, 360, 375, 390, 393, 414, 430, 768, 1024 and 1280 px**, with the API stubbed so that *every* message type is present — production data contains none of the media/deleted cases:

text · long unbroken token · long URL · emoji run · RTL (Arabic) · reply-quote · image (with and without caption) · video · voice note · deleted · edited-with-reactions · delivered-unread · incoming and outgoing variants of each.

Harness: `/home/user/uitest/` (`run_matrix.py`, `fixtures.py`), screenshots in `/home/user/uitest/shots/`, raw measurements in `/home/user/uitest/results.json`. Fixture *shapes* were copied from live production responses, so the DOM under test is the production DOM.

**One genuine defect found and fixed.** At 320 px the header contact name rendered **2.6 px past the right edge** (`.thread__name` right edge 322.6 px on a 320 px viewport):

* Cause: `.thread__name` is a `<span>`, i.e. a non-replaced **inline** box, and `overflow`/`text-overflow` do not apply to those — its `text-overflow: ellipsis` was inert. Proof by contrast inside the same component: its sibling `.thread__status` carries identical properties but is a `<div>`, and truncated correctly at 254 px.
* Fix: `display: block` on `.thread__name` (commit `d913da2`). No `overflow-x` band-aid; the element was already inside a correct `min-width: 0` flex chain.
* Re-measured after the fix: **no element crosses the viewport at any of the ten widths.**

Everything else held:

| Check | Result across all 10 widths |
|---|---|
| Elements crossing the viewport edge | none (the only hit is the 1 px visually-hidden "Skip to conversation" a11y link, by design) |
| `.bubble__tools` (reply + **three-dot**) | present on all 15 rows; `position: static`, inline under the bubble on touch; never clipped; never overlapping a bubble |
| Message actions menu, opened | 190 px wide, fully on-screen at every width, incl. a short 320×568 viewport (no bottom overflow) |
| Thread header actions | single row, no wrap, right edge inside the viewport |
| Horizontal scroll inside the message list | none (`scrollWidth == clientWidth`) |
| Long unbroken token / long URL bubbles | wrap inside the bubble; no bubble exceeds its column |
| JS page errors | 0 at every width |
| Global `overflow-x: hidden` band-aid | **absent.** `body.app-body { overflow: hidden }` is the fixed-viewport app-shell pattern (both axes, inner panes scroll); the only `overflow-x` rules are on genuine scroll containers (`.thread__scroll`, `.conv-pane__list`, `.page__body`). |

Minor, not a regression: the bubble tool buttons are **34×34 px** and the header buttons 38×38 px, below the 44×44 px touch-target guideline. Pre-existing; worth raising separately.

## 12. A hypothesis I tested and disproved (recorded so it is not re-investigated)

While hunting RC-7 I found that `notify_new_message()` issues `Notification.objects.select_for_update()`, and the enclosing `with transaction.atomic():` block appeared to start *after* that query. On MySQL a locking read in autocommit raises `TransactionManagementError`, which would have made every message send a 500. I built a standalone reproduction (`/home/user/rc7_repro/`) that demonstrates the mechanism exactly:

```
PRODUCTION CODE on MySQL (prod)          -> TransactionManagementError: select_for_update cannot be used outside of a transaction.
PRODUCTION CODE on SQLite (test suite)   -> OK (HTTP 201)
FIXED CODE (lock inside atomic)          -> OK (HTTP 201)
```

**But it does not apply here, and I verified that before changing anything:** `send_message()` (`services.py:99`) and `create_text_message()` (`views.py:238`) are both decorated `@transaction.atomic`, so the locking read is already inside a transaction. Instrumenting the real request path confirmed it — at the moment the query is issued, `autocommit=False, in_atomic_block=True`. Production agrees: the send I made returned 201 with `delivery.recipients: 1`, i.e. the aggregation branch really did execute. **No code change was made to `notifications/services.py`; the file is byte-identical to upstream.**

Two things are worth keeping from the exercise:

1. **A guard test was added** (`test_new_message_locking_read_runs_inside_a_transaction`). The safety of that locking read rests entirely on those two decorators, and nothing pinned that: the suite runs on **SQLite**, which reports `has_select_for_update = False` and therefore *drops the FOR UPDATE clause silently*, and `pytest-django` wraps each test in a transaction anyway. Removing a decorator would be invisible locally and return HTTP 500 on MySQL. The test runs with `transaction=True` and fails if the invariant is broken (verified by removing each decorator).
2. **A test-isolation hazard was found:** `messaging_policy()` memoises in a process-local cache with a TTL that `cache.clear()` does not reset, so platform policy leaks between tests. The new test pins the policy it needs and resets that cache; other suites may be silently depending on leaked values.
