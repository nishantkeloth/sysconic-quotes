# Performance & Scalability Audit — Phase 12

## Scaling assessment against requested tiers

| Tier | Assessment |
|---|---|
| 100 companies | **Ready as-is.** Every list endpoint checked is either already paginated or operates on small per-request result sets. `list_companies()` (`PERF-001`) is comfortably fine at this scale. |
| 1,000 companies | **Ready with one fix.** `list_companies()`'s full-table-scan-and-count-in-Python pattern starts to add real latency to the platform-admin dashboard around this range; the fix is small (an aggregation query). |
| 10,000 companies | **Needs work.** Same `PERF-001` issue becomes a hard blocker, not just a latency annoyance, at this scale; also the point at which the lack of any caching layer or read-replica strategy starts to matter for the platform-admin surface specifically (tenant-facing routes remain fine since they're already scoped to one company's data). |
| 100 concurrent users | **Ready as-is.** Stateless serverless functions scale horizontally by design; no in-memory session state exists anywhere that would break under concurrency. |
| 1,000 concurrent users | **Ready, with the Supabase connection-pooling caveat below.** |
| 10,000 concurrent users | **Needs verification.** Depends entirely on the Supabase project's plan tier and its pooler (PgBouncer) connection limit, which is outside this repository's control and was **NOT VERIFIED** in this audit (no dashboard access). Vercel serverless functions each open their own DB connection per invocation; at very high concurrency this is the most likely bottleneck, and is a platform-configuration question, not an application-code one, once Supabase's pooler is confirmed to be in transaction mode. |
| Millions of quotation line items | **Needs work.** Line items live inside `quotes.quote_data` as a single JSONB blob per quote, not as normalized rows. This is fine for typical AV/LED quote sizes (tens to low hundreds of line items) but means there is no way to index, filter, or paginate *within* a quote's line items at the database level — the entire blob is always read and written as one unit. Not a problem at current usage; would become one only if quotes routinely grew to thousands of line items each, which is not this product's current use case. |
| Large product catalogues | **Partially ready.** `ilike '%...%'` search on `products`/`customers`/`vendors` (leading-wildcard) does not use a standard B-tree index efficiently; fine for hundreds to low thousands of rows per company, will slow down for a company with a very large catalog. A `pg_trgm` GIN index would fix this cleanly without an architecture change. |
| High-volume PDF generation | **Needs work.** PDF generation (both the ReportLab path and the PDFShift path) runs synchronously inside the HTTP request/response cycle, subject to Vercel's function duration limit. Fine for one-at-a-time, human-triggered generation; would need to move to a background-job pattern for bulk/batch PDF generation. |
| Bulk imports | **Partially ready.** Products (500/request) and customers/vendors (1000/request) have explicit caps and insert in a single batched `.insert(rows)` call — reasonable for the stated caps, but a true "import a 50,000-row CSV" flow would need chunking/background processing, which doesn't exist today. |
| Bulk exports | **Not implemented at all** — `PRIV-001`. |
| Large attachments | **Ready within stated limits** (4MB images, 8MB proposal reference docs) — these caps are sensible for the product's actual use case. |
| Concurrent quote editing | **Needs work.** No optimistic-concurrency/version column on `quotes` — see the Phase 7 table in `MULTI-TENANCY-AUDIT.md`. Two people editing the same quote's `quote_data` simultaneously will silently last-write-wins overwrite each other. |

## Detailed inspection

- **Database indexes:** Basic single-column `company_id` indexes exist on the core tables; no composite indexes tuned to the actual query patterns used (e.g. `list_quotes()`'s `.eq('company_id',...).order('updated_at', desc=True)` would benefit from a composite `(company_id, updated_at)` index). See `DATABASE-RLS-AUDIT.md`.
- **Query plans:** Not obtainable — no live database connection was available to this audit; recommend running `EXPLAIN ANALYZE` on the handful of hot queries (`list_quotes`, `list_products` with search, `pp_project_detail`'s sub-resource fetches) directly in the Supabase SQL editor as a follow-up.
- **Pagination / cursor pagination:** Offset-based (`limit`/`offset`) throughout, not cursor-based. Offset pagination degrades at very large offsets (rare in this app's per-company data volumes) — not worth changing proactively, but worth knowing as a future limitation.
- **N+1 queries:** Not found as a systemic pattern — batched `.in_()` lookups are used correctly where multiple related rows need enrichment (reviewer names, activity actor names).
- **Large joins:** Not applicable — the Supabase query builder doesn't perform SQL joins directly; related data is fetched with separate, batched queries instead, which avoids join-related performance cliffs but trades off for more round-trips (see `pp_project_detail()`'s dozen sequential fetches, `PERF-002`).
- **Full-table scans:** Likely on the `ilike` search patterns at scale (see product-catalogue tier above); not measured directly.
- **Connection pooling / serverless connection usage:** Handled by Supabase's platform pooler; not independently configurable or verifiable from this repository.
- **Cache design / tenant-aware cache keys:** No caching layer exists anywhere in the application. Not a correctness risk (nothing to leak across tenants because nothing is cached), but a missed performance opportunity for read-heavy views (e.g. the product catalog, company profile) as usage grows.
- **Background queues / PDF job handling / email job handling / retry logic / dead-letter handling:** None of these exist. All PDF generation and email sending happen synchronously inline with the triggering HTTP request; a failure mid-way (e.g. PDFShift succeeds but Graph's send fails) is caught and reported as an error to the user, but there is no automatic retry or dead-letter queue — the user must manually retry the action.
- **Idempotency:** See `API-003`.
- **Horizontal scaling / statelessness:** **PASS** — the entire backend is stateless serverless functions with no in-process session/cache state; this is a genuinely strong foundation for horizontal scale.
- **Session storage:** Not applicable — stateless JWT (see `AUTHORIZATION-MATRIX.md` for the trade-offs that creates).
- **Long-running requests:** The Zoho sync routes are explicitly time-boxed (`MANUAL_SYNC_TIME_BUDGET_SECONDS = 8`, `CRON_SYNC_TIME_BUDGET_SECONDS = 45`) specifically because a prior full-sync-in-one-request design blew past Vercel's function duration limit — a good, evidence-based fix already in place for that one workflow, but the same risk pattern (a synchronous operation that could grow unbounded) exists for PDF generation and bulk import as usage scales.
- **Memory usage:** `list_companies()` and `pp_project_detail()` are the two routes most likely to have unbounded memory growth as data volume increases (see `PERF-001`, `PERF-002`).
- **API payload size:** No explicit `MAX_CONTENT_LENGTH` configured in any Flask app (relies on Vercel's platform-level limit).
- **Image optimization:** No server-side resizing/compression of uploaded images — stored as-uploaded.
- **Bundle size:** `index.html` is 415KB / 4,471 lines as a single file with no code-splitting (inherent to the vanilla-JS, no-build-step architecture) — a reasonable trade-off at this product's current complexity, but will become a real page-load-time concern if the file keeps growing at its current rate.
- **Lazy loading:** Not applicable to the current architecture (everything loads as one HTML file).
- **CDN usage:** Vercel's static hosting serves `index.html` via its edge network by default — this is platform-provided, not application-configured, and was not independently verified.
- **Rate limiting / per-tenant quotas / noisy-neighbour protection:** `AUTH-003`, `SUB-001` — none exist today. A single company generating a large volume of AI/PDF requests could currently consume a disproportionate share of Vercel function time and external API rate limits (Gemini/PDFShift/Brave) with no per-tenant throttle to protect other tenants.

## What should move from synchronous HTTP requests to background jobs

Prioritized by current risk of hitting a request-duration or rate-limit wall:

1. **Bulk product/customer/vendor import** beyond the current per-request caps (if those caps are ever raised).
2. **PDF/proposal generation for large multi-option quotes** — currently fine for typical sizes, but there is no upper bound enforced on `quote_data` size that would guarantee the ReportLab/PDFShift render stays within Vercel's function time budget.
3. **Zoho full-portfolio sync** — already partially addressed via time-boxing; a genuine queue (even a simple `pg`-backed job table processed by the existing cron) would be a more robust fix than time-boxed-with-resumption.
4. **Bulk export** (once built, per `PRIV-001`) — should be async/background from day one rather than a synchronous request, given it will need to touch every tenant-owned table.
