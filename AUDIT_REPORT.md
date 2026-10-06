# Nexora Repository Audit & Patch Report

**Date:** 2026-10-06 | **Repository baseline:** `8093382` (working-tree changes are not committed)
**Scope:** Frontend deployment/configuration and request behavior, Django authentication/realtime contracts, MySQL/MariaDB integrity migrations, cache behavior, tests, and documentation.

## Executive summary

The Render failure is a deterministic Django migration bug: an unordered grouped aggregate calls `.first()`, which asks Django to add ordering that is not valid for that aggregate. The conversation migration now uses `.exists()` for its duplicate guard. The related long-key digest migrations use the same safe pattern, and the conversation migration also checks existing row shape before replacing the conditional private-chat constraint.

The patch also makes the Vercel API-origin contract explicit, improves safe request deduplication and refresh handling, and keeps private API responses out of the service-worker cache. **No production database was accessed or migrated.** MySQL/MariaDB migration behavior, the Render deployment, and live cross-origin authentication remain unverified.

## Confirmed root cause and database changes

- Render failed in `conversations.0007_integrity_mysql_unique_hashes`, inside `_backfill_storage_key_hashes`, because `.first()` was used on a grouped/annotated queryset without an explicit ordering. The exception happens before the migration is recorded as applied.
- The duplicate check now uses `.exists()` on fixed-width digest groups. Grouping on the canonical lowercase SHA-256 value avoids false duplicate matches from MySQL’s commonly case-insensitive collation when original object keys differ only by case.
- Attachment storage keys, push endpoints, and branding storage keys remain intact. Uniqueness is moved to 64-character SHA-256 digest columns rather than long indexed strings. Model saves and the push-subscription lookup keep those digests synchronized.
- The staging-column operations inspect the live table before adding the column, and named unique-constraint operations inspect existing constraints before adding them. This is intended to tolerate the known MySQL partial-DDL retry case; it has only been exercised with SQLite logic smoke tests, not a real MySQL/MariaDB server.
- Private conversation uniqueness is represented by `(kind, admin, member)` plus the existing conversation-shape check. Multiple group rows can retain `member=NULL`; private rows with both IDs set remain unique. The migration stops on malformed conversation shapes or duplicate private pairs rather than deleting or merging data.
- Legacy long-column unique indexes are inspected and removed where applicable. No production schema change has been run here. If an existing database contains duplicate keys or malformed/duplicate private rows, the migration requires deliberate data repair; it will not silently discard messages or subscriptions.

## Frontend, request, and reliability changes

- `frontend/build.mjs` injects public `API_BASE_URL` into every page’s JSON config block and versions the service-worker cache using the build identity and API origin. Vercel builds reject missing values, loopback hosts, and plain-HTTP API origins. Explicit `same-origin` mode is allowed only when the deployment actually proxies `/api/` and `/ws/`.
- The browser config has no inferred production host or loopback fallback. `API_BASE_URL` is public configuration, not a secret; no real frontend `.env` is committed.
- Concurrent GET deduplication now distinguishes response mode, headers, auth/cache policy, timeout, and retry settings. Caller-cancellable requests remain unshared. Pagination links are limited to the configured API origin so a foreign link cannot receive a bearer header or credentialed API request.
- Refresh remains single-flight and CSRF-protected. A definitive refresh `401` is latched for the tab; network failures and refresh `403` responses do not falsely log the user out. A `403` clears the in-memory CSRF token so a later explicit retry bootstraps it again.
- WebSocket authentication uses cookies rather than placing credentials in the URL. The service worker only considers explicitly allowlisted public config/branding routes for caching; all other `/api/` traffic stays network-only.
- Existing visual language and private-chat/group rules were preserved; this patch does not claim a redesign or a measured responsiveness/performance gain.

## Performance and observability assessment

The repository’s conversation-list path already uses subqueries, `select_related`/`prefetch_related`, a single latest-message fetch for a page, and batched presence reads. The repository includes query-count and realtime guards for these paths. However, the MySQL-backed tests that exercise those guards could not reach their test database in this environment, and no latency, throughput, or device benchmark was run. Treat the changes as code-level performance safeguards, not measured speedups.

## Security and environment notes

- HttpOnly cookies, explicit CSRF bootstrap, server-side authorization, refresh rotation/revocation, and the no-private-service-worker-cache policy are retained. The auth-related session-storage marker records only an anonymous-state flag; access tokens remain in memory in bearer mode.
- `backend/.env.local.bak` was removed from the working tree without reading or printing its contents. The deletion is pending commit, and deleting a file from the current tree does not remove it from Git history. If it contained credentials, rotate them and consider appropriate history cleanup.
- The Django API configures a deny-all CSP. Vercel static responses currently set frame, MIME-sniffing, referrer, and permissions headers, but **do not have a CSP**. The static HTML contains inline bootstrap scripts and style attributes; a safe strict policy needs a separate hashed-CSP or script-extraction change. This report does not claim a complete static-site CSP posture.
- Production requires configured MySQL/MariaDB, Redis, object storage, and matching CORS/CSRF origins. Cross-site frontend/API deployments also need compatible Secure/SameSite cookie settings. Those live deployment settings were not inspected or verified.
- The tracked backup file’s values were not exposed. No production secrets are included in this report.

## Verification results

| Check | Result | Limitation |
|---|---|---|
| `node --test frontend/tests/` | **75 passed, 0 failed** | Includes Vercel missing/loopback/HTTP rejection and same-origin acceptance tests. |
| `python manage.py check` | **No issues** | Code-level system check; not a database migration test. |
| `python manage.py makemigrations --check --dry-run --skip-checks` | **No changes detected** | Django warned it could not check migration-history consistency because local MySQL at `127.0.0.1:3306` refused the connection. |
| Full backend `pytest -q --tb=no --disable-warnings` | **53 passed; 123 setup/errors; 0 assertion failures** | The test database could not be created because MySQL at `127.0.0.1:3306` refused connections. The 123 errors are not passing backend integration tests. |
| Django 5.2.17 SQLite migration-function smoke tests | Digest backfills, duplicate/shape preflights, case-sensitive key distinction, and staging-column retry helpers passed | These exercise migration logic only; SQLite does not validate MySQL/MariaDB DDL, collation, locks, or deployment state. |
| Python compilation and `git diff --check` | Passed / clean | Static checks only. |

## Required follow-up before production

1. Take a verified database backup and reproduce the current migration state on a staging MySQL/MariaDB instance using the same server family/version as production.
2. Inspect whether the failed deploy left the staging digest column or any named indexes behind; rerun the patched migration normally and verify the final columns, nullability, and unique constraints. Do not fake the migration as applied.
3. If the migration reports duplicates or malformed conversations, resolve them deliberately and preserve associated messages/subscriptions before retrying.
4. Set Vercel `API_BASE_URL` to the real HTTPS API origin (or configure a working same-origin proxy), then test browser CORS, CSRF, cookie refresh, WebSocket reconnect, upload, and logout behavior from the deployed origins.
5. Run the complete backend suite and query-count/realtime tests against the configured MySQL test database. Perform mobile/PWA checks on target browsers and measure production-like performance before making latency or throughput claims.
6. Decide whether to add a hash-based CSP for Vercel static pages or move inline startup code into external assets; do not add `'unsafe-inline'` and describe it as a strict CSP.
