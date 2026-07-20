# SaaS Readiness Scorecard

Scores are 0–100, evidence-based against what was directly observed in the repository. A high average is deliberately not allowed to hide a critical failure — see the **Gating Classification** below, which overrides the numeric average.

## Overall Score: 52 / 100

**Classification: NOT READY FOR MULTI-TENANT PRODUCTION**

This classification is driven by three specific, independently-verified gating triggers (full detail in the linked findings):

| Gating rule (from audit spec) | Triggered? | Finding |
|---|---|---|
| A verified cross-tenant data access path exists | **No** | Every application-code path checked correctly scopes to `company_id` from the JWT |
| Tenant isolation depends only on frontend filtering | **No** | Isolation is enforced server-side in every route checked |
| Tenant-owned database tables lack enforceable isolation | **Yes** | `TEN-001` — RLS covers only 21 of the newer Project Performance tables; the original core schema has none |
| Service-role or admin credentials exposed to the browser | **No** | Confirmed absent from `index.html` and all client-side code |
| Authorization exists only in the UI | **No** | Every privileged UI action checked has a matching server-side check |
| Critical secrets are committed or publicly exposed | **Partially** | `APP-001` — not committed to git, but a live-format secret sits in plaintext on disk, which this audit treats as a Critical finding in its own right even though it technically falls short of "publicly exposed" |
| Global admins not separated from tenant admins | **No** | `is_platform_admin` is structurally isolated onto a dedicated internal company row |
| Financial calculations are unreliable | **No** (data-integrity concern, not unreliability) | `QUO-002` — floating point + 4-way duplication is a drift risk, not a demonstrated bug |
| Quote records alterable post-acceptance without traceability | **Partially** | `QUO-001` — a status-change log entry is always created, but content diffs after reopening an awarded quote are not captured |
| No reliable backup/recovery mechanism exists | **NOT VERIFIED** | Supabase manages backups at the platform level; this audit had no dashboard access to confirm the plan tier's backup/PITR configuration — see `DEVOPS-RELIABILITY-AUDIT.md` |
| Critical production changes deployable without validation | **Yes** | `REL-006` — CI runs only a frontend test suite; nothing gates a merge to `main` beyond that |

**Two independent triggers plus the storage-bucket exposure (`TEN-002`, which is the direct analogue of "tenant-owned data lacks enforceable isolation" applied to file storage rather than database rows) are sufficient on their own for the NOT READY classification.** This is a fixable-in-days situation, not a rebuild.

## Per-domain scores

| Domain | Score /100 | Basis |
|---|---|---|
| Tenant isolation | 55 | Application-code discipline is strong and verified consistent; zero database-level backstop (`TEN-001`) and public storage buckets (`TEN-002`) are severe structural gaps |
| Authentication | 45 | Solid password hashing and generic-response anti-enumeration on forgot-password; no rate limiting (`AUTH-003`), no session revocation, 30-day non-refreshable tokens (`AUTH-002`) |
| Authorization | 60 | Consistent, correct company-scoping and role checks almost everywhere; one confirmed same-company IDOR (`AUTH-001`); coarse two-role model with no granular permissions |
| Database security | 40 | Partial RLS coverage only; schema/migration files don't match the live database (`REL-001`); no encryption at rest for stored third-party credentials (`APP-002`) |
| API security | 50 | Consistent tenant scoping at the endpoint level; wide-open CORS (`API-001`), inconsistent error handling (`API-002`), untrusted-HTML-to-PDF surface (`API-004`), no rate limiting |
| Application security | 45 | No hardcoded secrets in frontend code (verified); 19 known dependency CVEs (`APP-004`); unencrypted integration credentials (`APP-002`); XSS coverage not exhaustively verified (`APP-003`) |
| Quotation integrity | 65 | Pricing formula is correct where checked; internal review/approval workflow is genuinely well-designed and server-enforced; floating-point math and duplicated formulas (`QUO-002`); awarded-quote reopening gap (`QUO-001`) |
| Scalability | 55 | Reasonable pagination on most list endpoints; a few unbounded queries (`PERF-001`, `PERF-002`); serverless/stateless design is inherently horizontally scalable |
| Reliability | 40 | No structured error monitoring (`REL-003`); schema/migration drift (`REL-001`); dead orphaned route files (`REL-002`); staging/production database separation unresolved (`REL-004`, NOT VERIFIED) |
| Observability | 30 | Print-statement-only logging; no platform-admin audit log (`OBS-001`); business audit log lacks IP/before-after values (`OBS-002`) |
| DevOps / CI-CD | 35 | Three-branch flow exists and is coherent; CI gates almost nothing beyond one frontend test file (`REL-006`) |
| Testing | 30 | One genuinely good frontend regression suite exists; zero backend/API/RLS/cross-tenant/security test coverage (`TEST-001`) |
| Privacy readiness | 30 | No subprocessor inventory, retention policy, or tenant export/deletion workflow (`PRIV-001`); four live external subprocessors already in use |
| Subscription readiness | 15 | No billing integration, no entitlements model beyond a single ad hoc `features` JSONB flag, no usage metering (`SUB-001`) |

## How to read this scorecard

A domain score in the 50s–60s reflects genuinely solid underlying engineering discipline undermined by missing "boring but load-bearing" infrastructure — this describes most of this codebase. Scores below 40 (observability, DevOps, testing, privacy, subscription) reflect areas that simply haven't been built yet, which is normal and expected for a product at this stage that has so far been built and used by its own developer and one colleague, not sold externally. None of the low scores reflect *incorrect* engineering — they reflect infrastructure that hasn't been needed yet and now is.
