# Multi-Tenancy & Cross-Tenant Isolation Audit — Phases 2, 3 & 7

## Tenancy model

**Shared application, shared database, `company_id`-scoped rows — enforced entirely in application code, with database-level RLS present on only a minority subset of tables.**

`companies` is the first-class tenant entity (`schema.sql:2-14`). There is no `organizations`/`organization_members` join table — `users.company_id` is a direct, single-valued foreign key (`schema.sql:19`), meaning **one user belongs to exactly one company today**. This is coherent and consistently applied, but is a real constraint if multi-company-per-user membership is ever required — it would need a genuine data-model migration, not a config change.

## Per-table tenant-column audit

| Table | Tenant column present? | How isolation is actually enforced | Notes |
|---|---|---|---|
| `companies` | N/A (is the tenant) | — | — |
| `users` | `company_id` (NOT NULL FK) | App-code `.eq('company_id', ...)` everywhere | Platform admins parked in a dedicated `is_internal=true` company, not inside a real tenant (verified, `migrate-internal-company.sql`) |
| `invites` | `company_id` (NOT NULL FK) | App-code | — |
| `quotes` | `company_id` (NOT NULL FK) | App-code, verified on every route in `api/quotes.py` | — |
| `quote_versions` | **None** — reachable only via `quote_id` | Parent `quotes.company_id` validated before every access | Verified safe in every current code path; no DB-level guarantee (`TEN-001`) |
| `quote_version_reviewers` | **None** — reachable only via `version_id`→`quote_id` | Same as above | `_is_assigned_reviewer_on_quote()` (api/quotes.py:87-99) itself has no company_id filter — safe only because every caller pre-validates upstream |
| `quote_emails` | **None** — reachable only via `quote_id` | Parent-validated | Same fragility as `quote_versions` |
| `quote_activity` | **None** — reachable only via `quote_id` | Parent-validated | Same fragility |
| `vendors` | `company_id` (NOT NULL FK) | App-code | — |
| `products` | `company_id` (NOT NULL FK) | App-code | — |
| `customers` | `company_id` (confirmed via `api/customers.py` usage) | App-code | **Table not defined in any tracked `.sql` file** — schema drift, see `REL-001` |
| `company_integrations` | `company_id` (confirmed via `api/integrations.py` usage) | App-code | Not in tracked schema either; credentials stored in plaintext JSONB (`APP-002`) |
| `projects` | `company_id` (confirmed via `api/projects.py`) | App-code | Not in tracked schema either |
| `project_commercial_baselines` and 20 other Project Performance tables | `company_id` present | App-code **+ database RLS** (`migration-project-performance.sql:449-482`) | The one part of the schema with a real database-level backstop |
| Number sequences (quote refs) | N/A | Quote `ref` is a client-supplied string field inside `quote_data` JSONB, not a database sequence | See note below — no server-enforced uniqueness |
| API/integration credentials | `company_id` on `company_integrations` | App-code | See `APP-002` for the plaintext-storage concern |
| Jobs / queued tasks | N/A — no queue table exists | Cron routes take `company_id` from each row being processed in a loop (`_sync_contacts`, `_sync_project_actuals`), never from caller input | Correctly scoped per company within the batch |
| Audit logs | `quote_activity.quote_id`→company; no platform-level audit table exists at all | N/A | See `OBS-001`, `OBS-002` |

**Quote numbering:** quote reference numbers live inside the `quote_data` JSONB blob (`option.ref`), not as a server-generated, uniqueness-enforced sequence column. This audit found no server-side uniqueness check or per-company sequence generator for quote references — a genuine gap against the requested "quote numbering is unique within each tenant, sequences cannot collide across companies" requirement. **Status: FAIL** (not evaluated for a database-level unique constraint anywhere in `schema.sql` or migrations). Recommend adding a `quote_ref` sequence/counter per company, server-generated on quote creation, with a composite unique index on `(company_id, quote_ref)`.

## Tenant context propagation checklist

| Propagation point | Status | Evidence |
|---|---|---|
| Derived from authenticated identity | **PASS** | `claims['company_id']` from verified JWT, never request body/query/URL |
| Included in trusted server-side context | **PASS** | Same as above |
| Validated against active company membership | **PARTIAL** | The JWT's `company_id` is fixed at token issuance; a user removed from a company mid-session keeps using the old token until it expires (up to 30 days — compounds `AUTH-002`/`TEN-003`) |
| Propagated through every service/repository layer | **PASS** (single-layer app — no separate service/repo split, but the one layer that exists is consistent) | — |
| Included in background jobs | **PASS** | Cron sync loops iterate rows already scoped to a specific `company_id`; not caller-controlled |
| Included in cache keys | **NOT APPLICABLE** | No caching layer exists in the codebase |
| Included in storage paths | **PASS** (organizational) / **FAIL** (as an access-control boundary) | Paths are `{company_id}/...` but the bucket is public — see `TEN-002` |
| Included in search indexes | **NOT APPLICABLE** | No external search index (Elasticsearch/Algolia/etc.); in-app search uses Postgres `ilike` scoped by `company_id` |
| Included in analytics/reports | **PARTIAL** | Project Performance dashboard scopes correctly (`.eq('company_id', ...)`); the platform-admin `list_companies()` view is intentionally cross-company (that's its job) and exposes only metadata/counts, never business content — verified |
| Included in exports | **NOT APPLICABLE — no export feature exists** | See `PRIV-001` |
| Included in webhook processing | **NOT APPLICABLE — no inbound webhooks exist** | Zoho/Graph/Gemini/Brave/PDFShift are all outbound calls initiated by the app |
| Included in integration calls | **PASS** | Zoho credentials are looked up per-`company_id` (`_get_creds_for`), never a shared/global credential |

## Endpoints that accept `company_id`/`tenant_id` from the browser

**None found.** Every route derives `company_id` exclusively from `claims['company_id']` (the decoded, server-signed JWT). No route in any of the 12 deployed files reads `company_id` from `request.json`, `request.args`, or a URL path segment. This was checked file-by-file and is one of the strongest findings in this audit.

## Cross-tenant access review (Phase 3)

Each category below was checked against the actual route implementations, not assumed:

| Category | Finding |
|---|---|
| Insecure direct object references | Every `<id>`-parameterized route (`GET/PUT/DELETE /api/quotes/<qid>`, `/api/products/<pid>`, `/api/customers/<cid>`, `/api/vendors/<vid>`, `/api/projects/<pid>`, etc.) chains `.eq('id', <id>).eq('company_id', claims['company_id'])` — a foreign-company ID returns 404, not another tenant's data. **PASS**, verified across all files. |
| Broken object-level authorization | One same-company (not cross-tenant) instance found: `AUTH-001`. |
| Cross-company reads/updates/deletes | **PASS** — none found; every mutating route re-validates company ownership before acting. |
| Cross-company file access | **FAIL** — `TEN-002` (public buckets, no per-request authorization check at all, cross-tenant or same-tenant). |
| Cross-company report generation / PDF access | **PASS** at the generation step (PDF/proposal generation always re-fetches the quote scoped to `company_id`); **FAIL** at the storage-serving step (`TEN-002`) — the generated file itself is then publicly retrievable by URL. |
| Cross-company exports | **NOT APPLICABLE** — no export feature exists. |
| Cross-company search results | **PASS** — all in-app search (`products`, `customers`, `vendors`) is scoped by `company_id` before the `ilike` filter is applied. |
| Cross-company cache leakage | **NOT APPLICABLE** — no cache layer. |
| Cross-company notification leakage | **PASS** — review-assignment emails are sent only to users looked up within the same company (`reviewer_candidates()`/`submit_review()` both filter `.eq('company_id', claims['company_id'])`). |
| Cross-company background job leakage | **PASS** — see propagation table above. |
| Cross-company AI context leakage | **PASS**, independently re-verified — AI Review (`api/ai.py`) takes quote JSON directly from the request body (never queries the DB, so nothing to leak); Auto-BOM (`api/bom.py`) and Proposal drafting (`api/proposal.py`) both fetch their grounding data with an explicit `company_id` filter before it ever reaches Gemini. |
| Global administrator privilege leakage | **PASS** — `api/platform.py` routes expose only `id, name, slug, plan, status, created_at, user_count, quote_count` for the company list; no route in `platform.py` reads `quote_data`, `products`, `vendors`, or `customers` content for any company. No impersonation/"log in as tenant" mechanism exists anywhere (this is a positive from an abuse-surface standpoint, though it also means there is currently no supported "support access" workflow if one is ever needed operationally — see `PROPOSED-TARGET-ARCHITECTURE.md`). |

## Proposed cross-tenant automated test (highest priority test in this entire audit)

The single most important automated test this codebase does not yet have:

```
1. Create Tenant A (company + admin user) and Tenant B (company + admin user).
2. As Tenant A, create: a customer, a product, a quote (with at least one line item),
   and a product image upload.
3. As Tenant A, generate a PDF and (if Project Performance is enabled) award the quote
   to produce a commercial baseline.
4. Using Tenant A's valid JWT, attempt against every Tenant-B-owned object ID:
   a. GET  each object by ID (quote, product, customer, vendor, project, quote version,
      quote_email, activity log entry) → assert 404, never 200 with Tenant B's data.
   b. PUT  each object → assert 404/403, never a successful mutation.
   c. DELETE each object → assert 404/403, never a successful deletion.
   d. Attempt to pass Tenant B's company_id explicitly in a request body where the API
      accepts one → assert it is ignored/rejected, not honored.
5. Fetch Tenant B's public storage URLs (logo, product image, proposal PDF) using no
   auth at all → currently EXPECTED TO SUCCEED (this is TEN-002 — the test should FAIL
   the overall suite until that finding is closed).
6. Attempt Tenant B's Zoho sync trigger using Tenant A's admin JWT → assert 403/404.
7. As a Tenant-A non-admin, non-reviewer, non-creator user, call
   GET /api/quotes/<a-teammate's-qid>/versions → currently EXPECTED TO SUCCEED
   (this is AUTH-001 — same-company, not cross-tenant, but should also fail the suite
   until closed).

The suite as a whole must fail if even one cross-tenant operation in step 4 succeeds,
or if step 5/7's currently-open findings are not explicitly tracked as known-failing
until remediated.
```

This is formalized further as the top item in `TESTING-GAP-ANALYSIS.md`'s recommended minimum regression suite.

## Quotation domain integrity (Phase 7 summary — full findings in `FINDINGS.json`)

| Check | Status | Note |
|---|---|---|
| Quote numbering unique per tenant | **FAIL** | No server-enforced sequence/uniqueness — ref lives in client-supplied JSONB |
| Sequences cannot collide across companies | **NOT VERIFIED** | Follows from the above — no sequence exists to evaluate |
| Revisions immutable after issue | **PARTIAL** | `QUO-001` — content editable again after reverting status to draft, with only a generic status-change log entry, no automatic pre-edit snapshot |
| Previous revisions retrievable | **PASS** | `quote_versions` (internal review) and `quote_emails` (customer sends) both retain full historical snapshots |
| Draft/sent/awarded/lost states controlled | **PASS** | `schema.sql:70` CHECK constraint; `update_quote()` enforces the draft-only-editable rule |
| Invalid status transitions prevented | **PARTIAL** | The specific draft-lock rule is enforced; no formal state-machine validation of every transition pair exists (e.g. nothing stops `sent`→`awarded`→`sent` if that's ever a real business need to block) |
| Approval workflow cannot be bypassed | **PASS** | Reviewer assignment and decision recording are both server-validated (`submit_review()` only allows `can_review`-flagged, same-company users; `_record_reviewer_decision()` requires the caller to be an assigned reviewer) |
| Approval limits server-enforced | **NOT APPLICABLE** | No monetary approval-limit concept exists in the current design (reviewer-based, not threshold-based) |
| Discounts validated | **PASS** (structurally) | `disc`/`discAdd` are plain numeric fields multiplied into cost; no negative-value or >100% guard was found at the API layer — **PARTIAL** on strict input validation |
| Tax calculation consistent | **PASS** | Same VAT formula (`ts * rate/100`) used consistently where checked |
| Currency precision / rounding deterministic | **PARTIAL** | `QUO-002` — floating point, `round()` (Python banker's-rounding-adjacent behavior) vs JS `Math.round()`, duplicated across 4 files |
| Line totals match quote totals | **PASS** where checked (server recomputes from line items rather than trusting client-submitted totals for PDF/proposal generation) |
| Product price changes don't silently alter issued quotes | **PASS** | `quote_data` stores a full line-item snapshot (cost/margin/etc.) at save time, independent of the live `products.default_cost` |
| Terms & conditions versioned | **PARTIAL** | `terms_data` is snapshotted per quote/version/send (good), but there is no separate, reusable "terms template version" concept — each quote carries its own copy |
| PDF corresponds to correct revision | **PASS** | PDF/proposal generation always re-fetches the *live* `quotes` row scoped by `company_id`; there is no PDF-to-arbitrary-past-version linkage feature to misfire |
| PDFs uneditable post-finalization without a revision | **PARTIAL** | Ties to `QUO-001` — the underlying quote can be reopened and a new PDF regenerated without a forced new revision number |
| Accepted quotes preserve immutable commercial snapshot | **PARTIAL** | Only guaranteed when Project Performance's "Create Project" baseline-freeze step is actually run; not guaranteed for every awarded quote |
| Attachments linked to correct tenant/revision | **NOT APPLICABLE** | No general-purpose quote-attachment feature exists (only the proposal-generation flow's transient reference-document upload, which is not persisted per-revision) |
| Deleted products don't break historical quotes | **PASS** | Line items are snapshotted into `quote_data`, not foreign-keyed live to `products` |
| Time zones / validity dates handled correctly | **NOT VERIFIED** | `date`/validity fields are free-text/date strings inside `quote_data`; no explicit timezone-normalization logic was found, but no bug was demonstrated either |
| Concurrent editing doesn't silently overwrite | **FAIL** | No optimistic-concurrency/version column on `quotes` for the live `quote_data` field — two simultaneous editors' autosaves will last-write-wins overwrite each other with no conflict warning |
| Financially significant changes audited | **PARTIAL** | `OBS-002` — status transitions are logged; granular before/after value diffs are not |

**Floating-point in monetary calculations:** confirmed. See `QUO-002` in `FINDINGS.json` for full detail — Python `float`/`round()` and JavaScript `Number`/`Math.round()` are used throughout `api/pdf.py`, `api/proposal.py`, `api/quotes.py`, and `index.html`'s client-side pricing engine, with the identical formula hand-duplicated four times.
