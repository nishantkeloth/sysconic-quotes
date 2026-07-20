# Database & Row-Level Security Audit — Phase 4

## Platform
Supabase-hosted PostgreSQL. All application access goes through the Supabase service-role key (`SUPABASE_SERVICE_KEY`), which bypasses RLS unconditionally regardless of whether policies exist on a table (this is standard, expected Supabase/Postgres behavior for the service role, not a misconfiguration in itself — the risk is the *absence* of RLS as a backstop, not the service role bypassing it in normal operation).

## Schema/migration completeness caveat
`schema.sql` and the nine `migration-*.sql` files are **not a complete, trustworthy source of truth** for the live schema. `customers`, `company_integrations`, and `projects` — all actively read/written by deployed routes — have no `CREATE TABLE` anywhere in the tracked SQL files (see `REL-001`). The table below is built from a combination of `schema.sql`, `migration-project-performance.sql`, and the columns/filters actually used in `api/*.py` for the three undocumented tables; column-level detail for those three (constraints, indexes, exact types) is **NOT VERIFIED** and would need a live schema export to confirm precisely.

## Constraint / index / integrity review (schema.sql + migrations, as tracked)

| Item | Status | Evidence |
|---|---|---|
| Primary keys | **PASS** | Every tracked table uses `id uuid default gen_random_uuid() primary key` |
| Foreign keys | **PASS**, mostly with `on delete cascade` | e.g. `users.company_id references companies(id) on delete cascade`, `quotes.company_id` same pattern |
| Tenant-aware foreign keys | **PARTIAL** | Child quote tables (`quote_versions`, `quote_version_reviewers`, `quote_emails`, `quote_activity`) FK only to `quote_id`, not directly to `company_id` — correct relationally, but means no composite tenant-aware index/constraint is possible on those tables directly |
| Unique constraints | **PARTIAL** | `companies.slug`, `users.email`, `invites.token` are globally unique (appropriate); no composite `(company_id, quote_ref)`-style tenant-scoped uniqueness exists for quote numbering (see `MULTI-TENANCY-AUDIT.md`) |
| Composite unique constraints | **PARTIAL** | `quote_version_reviewers` has `unique(version_id, user_id)` (good); product dedup uses a `(company_id, model_key, brand_key)`-style index per `migration-dedupe-products.sql` (referenced in `api/products.py` comments, file not fully read in this pass — **NOT VERIFIED** in full detail) |
| Indexes | **PASS** (basic coverage) | `idx_quotes_company`, `idx_users_company`, `idx_products_company`, `idx_vendors_company`, plus several `quote_versions`/`quote_version_reviewers` indexes |
| Composite indexes starting with `tenant_id` | **PARTIAL** | Most indexes are single-column `company_id`; no evidence of deliberately-ordered composite indexes (e.g. `(company_id, status, updated_at)` for the quotes list/sort/filter pattern actually used in `list_quotes()`) — a performance, not correctness, gap |
| Cascade behavior | **PASS** | `on delete cascade` used consistently for company_id/quote_id FKs, which is appropriate (deleting a company should not orphan its data) |
| Soft deletion | **FAIL** | No `deleted_at`/`is_deleted` pattern anywhere — all deletes (`delete_quote`, `delete_product`, `delete_customer`, `delete_vendor`, `delete_project`) are hard deletes. No accidental-deletion recovery path exists beyond a Supabase-level PITR restore (platform-level, **NOT VERIFIED** whether enabled — see `DEVOPS-RELIABILITY-AUDIT.md`) |
| Timestamps | **PASS** | `created_at`/`updated_at` present on the tables that need them; `quotes_updated_at` trigger auto-maintains `updated_at` |
| Optimistic concurrency / version control | **FAIL** | No version/row-lock column on `quotes` to detect concurrent-edit conflicts on the live `quote_data` field (see `MULTI-TENANCY-AUDIT.md` Phase 7 table) |
| Quote revision integrity | **PARTIAL** | See `QUO-001` |
| Transaction usage | **NOT VERIFIED / likely PARTIAL** | The Supabase Python client's `.execute()` calls are individually atomic per-statement; multi-step flows like `_freeze_commercial_baseline()` (baseline insert → section inserts → line inserts → projects update, `api/quotes.py`) are **not wrapped in a single database transaction** — a mid-sequence failure could leave a partial baseline. No explicit transaction/RPC-wrapped multi-statement write was found anywhere in the codebase. |
| Connection pooling | **NOT VERIFIED** | Handled by Supabase's pooler (PgBouncer) at the platform level, not app code; not independently confirmable from the repo |
| Query pagination | **PARTIAL** | See `PERF-002` |
| N+1 queries | **PARTIAL** | Several list-then-lookup patterns (e.g. `_reviewers_by_version()`, activity-log actor-name enrichment) correctly batch with `.in_()` rather than looping — good; `pp_project_detail()`'s dozen sequential per-resource queries are not N+1 in the classic sense but are still many round-trips per page load |
| Unbounded queries | **PARTIAL** | `PERF-001`, `PERF-002` |
| Full-table scans | **NOT VERIFIED** | No `EXPLAIN` output available in this audit (no live DB connection); `ilike '%...%'` search patterns (`api/products.py`, `api/customers.py`, `api/vendors.py`) are not index-friendly for a leading wildcard, which will force a scan at scale — worth a `pg_trgm` index if catalog sizes grow |
| Database functions | **PARTIAL** | Only `update_updated_at()` (a simple trigger function) found in tracked SQL |
| Triggers | **PASS** (limited scope) | `quotes_updated_at` trigger, correctly scoped |
| Views | **NOT FOUND** | No views defined anywhere in tracked SQL |
| Materialized views | **NOT FOUND** | None — `api/platform.py`'s `list_companies()` computes aggregates in Python instead (`PERF-001`) |
| Search functions | **NOT FOUND** | No `tsvector`/full-text search function; search is `ilike` |
| SECURITY DEFINER functions | **NOT FOUND** | None in tracked SQL — this is a genuinely low-risk item, since SECURITY DEFINER functions are a common RLS-bypass vector and none exist to misuse |
| Database backup assumptions | **NOT VERIFIED** | Platform-managed (Supabase); plan tier and PITR/backup retention settings were not accessible in this audit |

## Row-Level Security status table

| Table | Tenant-owned | Tenant column | RLS enabled | SELECT policy | INSERT policy | UPDATE policy | DELETE policy | Risk | Required action |
|---|---|---|---|---|---|---|---|---|---|
| `companies` | Yes (is tenant root) | `id` | No | None | None | None | None | Critical | Enable RLS + tenant_isolation-style policy |
| `users` | Yes | `company_id` | No | None | None | None | None | Critical | Enable RLS |
| `invites` | Yes | `company_id` | No | None | None | None | None | High | Enable RLS |
| `quotes` | Yes | `company_id` | No | None | None | None | None | Critical | Enable RLS |
| `quote_versions` | Yes (via parent) | none (indirect) | No | None | None | None | None | High | Enable RLS via join to `quotes`, or add direct `company_id` column |
| `quote_version_reviewers` | Yes (via parent) | none (indirect) | No | None | None | None | None | High | Same as above |
| `quote_emails` | Yes (via parent) | none (indirect) | No | None | None | None | None | High | Same as above |
| `quote_activity` | Yes (via parent) | none (indirect) | No | None | None | None | None | Medium | Same as above |
| `vendors` | Yes | `company_id` | No | None | None | None | None | High | Enable RLS |
| `products` | Yes | `company_id` | No | None | None | None | None | High | Enable RLS |
| `customers` | Yes | `company_id` (confirmed in app code; not in tracked schema) | No | None | None | None | None | High | First: add to a tracked migration (`REL-001`); then enable RLS |
| `company_integrations` | Yes | `company_id` (confirmed in app code) | No | None | None | None | None | Critical (contains third-party credentials, `APP-002`) | Track schema + enable RLS + encrypt `credentials` column |
| `projects` | Yes | `company_id` (confirmed in app code) | No | None | None | None | None | High | Track schema + enable RLS |
| `project_commercial_baselines`, `project_baseline_sections`, `project_baseline_lines`, `project_cost_categories`, `project_cost_category_mappings`, `zoho_purchase_orders`, `zoho_purchase_order_lines`, `zoho_bills`, `zoho_bill_lines`, `zoho_expenses`, `zoho_invoices`, `zoho_payments`, `project_forecasts`, `project_progress_history`, `project_budget_revisions`, `project_health_scores`, `project_performance_settings`, `project_performance_snapshots`, `project_alerts`, `pp_sync_logs`, `pp_mapping_exceptions`, `project_ai_analyses` (21 tables) | Yes | `company_id` | **Yes** | `tenant_isolation` USING policy (`company_id = (auth.jwt() ->> 'company_id')::uuid`), applies to all commands (no separate per-command WITH CHECK — policy has no explicit `WITH CHECK` clause, so it defaults to reusing the USING expression for INSERT/UPDATE) | (same policy) | (same policy) | (same policy) | Medium (this is the one part of the schema already backstopped; the residual risk is only that the policy relies on `auth.jwt()`, which is empty for every request this app actually makes via the service role, so it is a true no-op against the app's own traffic — a real gate only against a hypothetical future anon/lower-privilege key) | Verify a WITH CHECK clause is added explicitly for INSERT/UPDATE if the policy is ever relied upon for a non-service-role connection; otherwise no action needed beyond documenting the limitation |

**Storage (`storage.objects`) policies:** **Not applicable / not evaluated as RLS policies** because all three buckets (`product-images`, `company-assets`, `proposals`) are configured **public**, meaning Supabase serves object reads with no policy evaluation at all for anonymous requests. This is a more severe condition than "RLS enabled with a weak policy" — there is no policy layer in effect. See `TEN-002`. Signed-URL generation (which would make bucket-level RLS policies meaningful) is not used anywhere in the codebase (`create_signed_url` — zero matches).

## SECURITY DEFINER / privileged functions
None found. This is a genuine positive — SECURITY DEFINER functions are one of the most common ways RLS gets silently bypassed, and this codebase has no attack surface there because it has no such functions at all.

## Realtime subscriptions
**NOT APPLICABLE.** No Supabase Realtime subscription usage was found anywhere in `index.html` or `api/*.py` (no `supabase.channel(...)`/`.on('postgres_changes', ...)` pattern). Nothing to leak.

## RPC functions
**NOT APPLICABLE.** No custom Postgres RPC functions (`sb.rpc(...)`) are called anywhere in the codebase — every operation goes through the standard REST query builder.

## search_path security
**NOT APPLICABLE** — moot given no SECURITY DEFINER functions exist to have an unsafe `search_path`.

## Summary
The database layer's single largest gap is coverage, not policy quality: where RLS exists (the 21 Project Performance tables), the policy pattern itself is sound and matches Supabase's own recommended tenant-isolation idiom. The problem is that this pattern was never retrofitted onto the ~10+ original, highest-traffic tables, and three tables aren't even tracked in a migration file to retrofit it onto. Closing `TEN-001` is mechanically straightforward — the exact `DO $$ ... FOREACH ... EXECUTE FORMAT ...` pattern already proven in `migration-project-performance.sql:455-476` can be directly reused against the remaining table list.
