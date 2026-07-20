# Reliability, Data Protection & CI/CD Audit — Phases 13, 14 & 15

Each item is explicitly marked as **code-controlled** (this repo determines the outcome) or **platform-controlled** (Vercel/Supabase configuration, outside this repo, mostly NOT VERIFIED without dashboard access) per the audit's own instruction to separate the two.

## Reliability & data protection (Phase 13)

| Item | Controlled by | Status |
|---|---|---|
| Automated backups | Platform (Supabase) | **NOT VERIFIED** — depends on the project's plan tier; not accessible from this repo |
| Point-in-time recovery readiness | Platform (Supabase) | **NOT VERIFIED** — PITR is a paid-tier Supabase feature; plan tier unknown |
| Restore testing | Process (neither code nor platform alone) | **NOT VERIFIED / likely FAIL** — no restore-drill documentation or automation exists in the repo |
| Disaster recovery plan | Process | **NOT FOUND** — no DR runbook exists in the repository |
| Recovery Point Objective / Recovery Time Objective | Process | **NOT DEFINED** anywhere in the repo |
| Multi-region considerations | Platform | **NOT VERIFIED** — single Supabase project/region assumed, not confirmed |
| Database migration rollback | Code | **FAIL** — migrations are forward-only, manually-applied SQL with no down-migration or rollback tooling (`REL-001` compounds this) |
| Zero-downtime deployment readiness | Platform (Vercel handles atomic deploys) + Code | **PARTIAL** — Vercel's own deployment model is atomic per function, but a schema migration applied out-of-band (manually, via SQL editor) before the corresponding code deploys, or vice versa, has no coordination mechanism — a real risk given migrations are already confirmed to be applied inconsistently (`REL-001`) |
| Health checks / readiness checks | Code | **NOT FOUND** — no `/health` or `/ready` endpoint exists in any api/*.py file |
| Graceful shutdown | Platform | **NOT APPLICABLE** in the way it applies to long-running servers — Vercel functions are invoke-per-request, so this concept doesn't map directly; not a gap in this architecture |
| Retry policies | Code | **PARTIAL** — the Zoho sync adapter has some resilience (best-effort per-resource try/except with per-resource error logging in `_sync_project_actuals`), but no exponential-backoff retry exists for any external API call (Graph, Gemini, Brave, PDFShift, Zoho) |
| Timeout policies | Code | **PASS** — every external HTTP call reviewed (Graph, Gemini, Brave, PDFShift, Zoho) has an explicit `timeout=` value set, avoiding an indefinitely-hanging request |
| Circuit breakers | Code | **NOT IMPLEMENTED** — a degraded external service (e.g. Gemini rate-limited) fails each request independently with no circuit-breaker to stop hammering it |
| Idempotency | Code | `API-003` |
| Partial failure handling | Code | **PARTIAL** — `_sync_project_actuals()` correctly isolates failures per-resource (a failed invoice sync doesn't block the bills sync); `_freeze_commercial_baseline()`'s multi-table write sequence has no equivalent isolation/rollback (see `DATABASE-RLS-AUDIT.md`'s transaction-usage note) |
| Transaction boundaries | Code | **FAIL** — no explicit multi-statement transactions found anywhere; multi-step writes rely on each individual `.execute()` being atomic, not the sequence as a whole |
| Duplicate event handling | Code | **PARTIAL** — `zoho_create_project()`'s idempotency check (`API-003`); no equivalent exists for other multi-step flows |
| Data corruption detection | Code/Process | **NOT FOUND** — no checksums, reconciliation jobs, or data-integrity monitoring |
| Soft deletion | Code | **FAIL** — all deletes are hard deletes (`DATABASE-RLS-AUDIT.md`) |
| Accidental deletion recovery | Platform (PITR, if enabled) + Code (soft-delete, absent) | **NOT VERIFIED / weak** — recovery today would depend entirely on an unconfirmed platform-level PITR capability, with no application-level safety net |
| Tenant-level export | Code | **NOT IMPLEMENTED** — `PRIV-001` |
| Tenant-level deletion | Code | **NOT IMPLEMENTED** — only suspend/reactivate exist at the platform-admin level; no full tenant purge workflow |
| Tenant offboarding | Code/Process | **NOT IMPLEMENTED** |

## Observability & auditability (Phase 14)

| Item | Status |
|---|---|
| Structured logs | **FAIL** — plain `print()`/`traceback.print_exc()`, not structured JSON logging |
| Request/correlation IDs | **NOT FOUND** — no correlation ID is generated or propagated anywhere |
| Tenant ID in server logs | **PARTIAL** — `company_id` is available in scope wherever an error is logged, but is not consistently included in the actual log line's content (it would need to be explicitly added to each `print()`/exception message) |
| User ID in security events | **PARTIAL** — same caveat; available in scope, not systematically logged |
| Error monitoring | **FAIL** — `REL-003` |
| Performance monitoring | **NOT FOUND** — no APM |
| Database monitoring | **NOT VERIFIED** — platform-level (Supabase dashboard), not app-code |
| Uptime monitoring | **NOT VERIFIED** — not evidenced in the repo; may exist as an external, unconfigured-in-repo service |
| Security event monitoring | **NOT FOUND** |
| Failed login monitoring | **NOT FOUND** — no failed-attempt logging/alerting |
| Privilege-change monitoring | **FAIL** — `OBS-001` |
| Export monitoring | **NOT APPLICABLE** (no export feature) |
| Impersonation monitoring | **NOT APPLICABLE** (no impersonation feature) |
| Webhook monitoring | **NOT APPLICABLE** (no inbound webhooks) |
| Queue monitoring | **PARTIAL** — `pp_sync_logs` table records per-resource sync outcomes (a genuinely good, evidence-based pattern already in place for the Zoho sync specifically), but nothing equivalent exists for any other background/cron-driven process |
| Alerting | **NOT FOUND** |
| Log retention | **NOT VERIFIED** — governed by Vercel's platform-level log retention (plan-tier dependent), not application-configured |
| Sensitive-data redaction | **NOT IMPLEMENTED** — no redaction layer exists; error messages returned to clients and printed to logs are not filtered for accidental credential/PII leakage (noted in `APPLICATION-SECURITY-AUDIT.md`) |

**Business audit log (`quote_activity`) completeness against the requested checklist:**

| Requested field | Present? |
|---|---|
| Who performed the action | Yes (`actor_id`) |
| Tenant | Indirect only (via `quote_id`→`quotes.company_id`, not a direct column) |
| Action | Yes |
| Entity type | Implicit (always "quote") — no generalized entity_type column since this log is quote-specific only |
| Entity ID | Yes (`quote_id`) |
| Date/time | Yes (`created_at`) |
| Previous value | **No** |
| New value | **No** |
| IP address | **No** |
| User agent | **No** |
| Correlation ID | **No** |
| Impersonation context | **Not applicable** (no impersonation feature) |
| Approval events | **Yes** — `submitted_for_review`, `reviewer_approved`, `reviewer_requested_changes`, `admin_force_approved`, `admin_force_requested_changes`, `admin_cancelled_review`, `admin_reassigned_reviewers` are all distinct logged actions |
| Quote issue/acceptance events | **Yes** — `emailed_to_customer`, `status_changed` (covers awarded/lost) |

Append-only protection: **NOT VERIFIED** — no database trigger or RLS policy prevents an ordinary application code path (or, with the service-role key, literally any code) from updating/deleting `quote_activity` rows after the fact. There is no dedicated `INSERT`-only constraint at the database level.

## CI/CD (Phase 15)

| Item | Status |
|---|---|
| Branch strategy | **PASS** — clear `dev` → `staging` → `main` flow |
| Pull request checks | **PARTIAL** — the one existing test job does run on PRs into `staging`/`main`, but it is the only check |
| Automated tests | **PARTIAL** — frontend only, see `TEST-001` |
| Type checks | **FAIL** — none configured |
| Lint checks | **FAIL** — none configured |
| Security scans | **FAIL** — none configured |
| Dependency scans | **FAIL** — none configured (this audit's pip-audit run was manual/ad hoc, not part of CI) |
| Secret scans | **FAIL** — none configured |
| Build validation | **NOT APPLICABLE** in the traditional sense (no separate build artifact), though a basic "does every api/*.py file at least import cleanly" smoke check would be cheap and currently doesn't exist |
| Preview environments | **NOT VERIFIED** — Vercel provides these automatically per-PR/branch by default; not confirmed configured/used here |
| Environment isolation (dev/test/staging/production) | **PARTIAL/FAIL** — `REL-004` (staging DB separation status unresolved) |
| Database migration process | **FAIL** — manual, undocumented-in-git, unversioned application (`REL-001`) |
| Rollback process | **FAIL** — not defined for either code or schema |
| Infrastructure as code | **FAIL / NOT FOUND** — Vercel project configuration is managed via its dashboard, not as code (beyond `vercel.json`'s routing/build config, which is not full IaC) |
| Deployment approvals | **NOT VERIFIED** — depends on Vercel project/GitHub branch-protection settings, not visible from the repo alone |
| Production access control | **NOT VERIFIED** — who has push access to `main`/Vercel production is a platform/GitHub-org setting, not evidenced in this repo |
| Environment variable management | **PARTIAL** — correctly separated into Vercel's encrypted store per-project (based on code behavior — no hardcoded fallbacks for real secrets), but `APP-001`'s local-file risk sits outside that otherwise-sound system |
| Release versioning | **NOT FOUND** — no version tags/semver scheme observed in the repo (project notes mention manually bumping a "version label," which is a UI string, not a release-versioning process) |
| Feature flags | **PARTIAL** — the ad hoc `companies.features` JSONB exists for exactly one feature (Project Performance) and has no general-purpose flag-management tooling |
| Canary / gradual rollout readiness | **NOT IMPLEMENTED** — Vercel deploys are all-at-once to each environment |
| Automated smoke tests | **NOT FOUND** |
| Post-deployment verification | **NOT FOUND** — no automated check confirms a deploy succeeded beyond Vercel's own build-success signal |

**Flagged: code can currently reach `main` (production) with no automated check beyond one frontend JS test file.** This is `REL-006` and is one of the higher-leverage, lower-effort fixes in this entire audit — every other CI gap in this table becomes meaningfully cheaper to close once a second CI job exists to hang them off of.
