# Remediation Roadmap — Phase 20

Grouped per the audit's required tiers. Finding IDs reference `FINDINGS.json`. No fixes have been implemented as part of this audit — this is a plan, not a changelog.

---

## P0 — Immediate blockers before onboarding any external company

### 1. Rotate the Supabase service-role key and JWT secret (`APP-001`)
- **Problem:** A live-format secret sits in plaintext in `.env.example` on disk.
- **Business risk:** Full, RLS-bypassing database compromise if the value is genuine.
- **Technical approach:** Rotate `SUPABASE_SERVICE_KEY` in the Supabase dashboard; update Vercel's encrypted env store for both `sysconic-quotes` and `sysconic-quotes-staging`; generate a fresh high-entropy `JWT_SECRET` and rotate it (this logs out all current sessions — acceptable now, communicate to the one colleague using the app). Replace `.env.example`'s values with obvious placeholders.
- **Files affected:** `.env.example`, Vercel project environment settings (not in-repo).
- **Database changes:** None.
- **Tests required:** None new; confirm the app still authenticates after rotation.
- **Complexity:** Small.
- **Dependencies:** None.
- **Sequence:** Do this first, independent of everything else — it's a 15-minute task with no code changes.
- **Rollback:** Keep the old key valid for a short overlap window if Supabase supports it, otherwise expect a brief outage during the swap.
- **Acceptance criteria:** New key confirmed working in both environments; old key confirmed revoked; `.env.example` contains no real value.

### 2. Make all Supabase Storage buckets private + switch to signed URLs (`TEN-002`)
- **Problem:** `product-images`, `company-assets`, `proposals` are all public with permanent unsigned URLs.
- **Business risk:** Any party with a file URL can read another company's logo, product image, or full commercial proposal PDF with zero authentication.
- **Technical approach:** Flip all three buckets to private in Supabase; replace every `get_public_url()` call with `create_signed_url()` (short TTL, e.g. 10 minutes) generated at the moment a file is displayed/downloaded by an authenticated, company-scoped request; update the frontend to request a fresh signed URL rather than caching the stored `image_url`/`logo_url`/proposal URL indefinitely.
- **Files affected:** `api/products.py`, `api/auth.py`, `api/proposal.py`, `index.html` (wherever these URLs are rendered/cached).
- **Database changes:** None required (existing `image_url`/`logo_url` columns can keep storing the *path*; only how it's resolved to a fetchable URL changes) — though consider renaming the stored value from "URL" to "path" for clarity in a follow-up, non-blocking cleanup.
- **Tests required:** File-access cross-tenant test (item 7 in `TESTING-GAP-ANALYSIS.md`).
- **Complexity:** Medium.
- **Dependencies:** None — can proceed in parallel with item 1.
- **Sequence:** Do immediately after item 1; this is the second-most time-sensitive fix.
- **Rollback:** Revert bucket privacy setting if the signed-URL frontend change isn't ready yet — but treat any delay as a live risk window, not an acceptable resting state.
- **Acceptance criteria:** Fetching any stored file URL without a valid, company-scoped signed URL returns 403/404; the app's UI continues to display logos/images/proposals correctly for authenticated, authorized users.

### 3. Enable RLS on every remaining tenant table (`TEN-001`)
- **Problem:** Only 21 of the ~30+ tenant tables have database-level RLS; the original core schema (companies, users, quotes, products, customers, etc.) has none.
- **Business risk:** No database-level backstop against a future missed `.eq('company_id', ...)` filter.
- **Technical approach:** Reuse the exact pattern already proven in `migration-project-performance.sql:455-476` (a `DO $$ ... FOREACH t IN ARRAY [...] LOOP ... EXECUTE FORMAT('alter table %I enable row level security', t); EXECUTE FORMAT('create policy tenant_isolation on %I using (company_id = (auth.jwt() ->> ''company_id'')::uuid)', t); END LOOP; END $$;`) against the remaining table list: `companies`, `users`, `invites`, `quotes`, `vendors`, `products`, `customers`, `company_integrations`, `projects`. For the four child quote tables with no direct `company_id` column (`quote_versions`, `quote_version_reviewers`, `quote_emails`, `quote_activity`), either add a `company_id` column (denormalized, backfilled from the parent `quotes` row, kept in sync — the cleaner long-term fix) or write a policy that joins through `quote_id` (works but is slower and more complex to maintain).
- **Files affected:** New migration file (see item 4 below — must be created as part of fixing the schema-tracking gap first).
- **Database changes:** Yes — `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` + `CREATE POLICY` on ~9-13 tables, plus optionally new `company_id` columns on 4 child tables.
- **Tests required:** RLS policy test (item 5 in `TESTING-GAP-ANALYSIS.md`).
- **Complexity:** Medium.
- **Dependencies:** Item 4 (schema must be accurately tracked before this migration can be written correctly for `customers`/`company_integrations`/`projects`, whose exact current column set isn't confirmed from the repo alone).
- **Sequence:** After item 4 (schema-truth), before onboarding any external company.
- **Rollback:** `DROP POLICY`/`ALTER TABLE ... DISABLE ROW LEVEL SECURITY` — non-destructive, reversible in seconds; the service-role key continues to work unaffected either way since it bypasses RLS by design.
- **Acceptance criteria:** RLS enabled and a `tenant_isolation` policy present on every tenant table; app functionality unchanged (service-role calls are unaffected); the RLS test suite item passes.

### 4. Establish an accurate, tracked database schema baseline (`REL-001`)
- **Problem:** `schema.sql` and the migration files don't include `customers`, `company_integrations`, or `projects`.
- **Business risk:** Any schema/security work planned off the tracked files (including item 3 above) will be wrong or incomplete for those three tables.
- **Technical approach:** Export the live schema directly from Supabase (dashboard schema export or `pg_dump --schema-only` against the production connection string, run carefully and read-only), reconcile it against `schema.sql` + all migration files, and commit a corrected, complete baseline. Going forward, adopt a strict rule: every schema change ships as a new, numbered migration file, no ad hoc dashboard DDL.
- **Files affected:** `schema.sql` (or a new consolidated baseline file), new migration-process documentation.
- **Database changes:** None (read-only export) — this item is about documentation catching up to reality, not changing the database.
- **Tests required:** A CI schema-drift check (nice-to-have, not blocking).
- **Complexity:** Small.
- **Dependencies:** None.
- **Sequence:** Before item 3.
- **Rollback:** N/A (documentation-only change).
- **Acceptance criteria:** Every table actually used by `api/*.py` has a corresponding `CREATE TABLE` in a tracked file, with columns matching the live schema.

### 5. Fix the `AUTH-001` same-company permission gap
- **Problem:** `get_version()`, `list_versions()`, and `quote_pdf()` don't check `_can_view_quote()`.
- **Business risk:** Any teammate can read another teammate's private quote review history and download their quote PDF.
- **Technical approach:** Add the existing `_can_view_quote()` check (already used elsewhere in the same file) to all three routes.
- **Files affected:** `api/quotes.py`, `api/pdf.py`.
- **Database changes:** None.
- **Tests required:** Item 2 in `TESTING-GAP-ANALYSIS.md`'s regression suite, specifically covering this case.
- **Complexity:** Small.
- **Dependencies:** None.
- **Sequence:** Can be done in parallel with anything above — it's a three-line code change.
- **Rollback:** Trivial revert.
- **Acceptance criteria:** A non-creator, non-admin, non-`can_view_all_quotes`, non-assigned-reviewer user receives 403 from all three routes.

---

## P1 — Required before production SaaS launch (external, untrusted companies at scale)

- **Rate limiting on auth endpoints** (`AUTH-003`) — Medium complexity, needs a shared store (Redis/Upstash) reachable from serverless functions.
- **Session management overhaul** (`AUTH-002`, `TEN-003`) — shorter token TTL, refresh-token flow, server-side revocation table. Medium-Large complexity; will be backward-incompatible for any existing long-lived tokens (acceptable, given current user count is effectively one company).
- **Encrypt integration credentials at rest** (`APP-002`) — Medium complexity, requires a new encryption key and a one-time re-encryption of existing stored credentials.
- **Restrict CORS to known origins** (`API-001`) — Small complexity.
- **Bump vulnerable dependencies** (`APP-004`) — Small complexity, verify via the existing (soon-to-exist, per P1 CI item below) test suite after bumping.
- **Add security response headers** (`APP-005`) — Small complexity, `vercel.json` change only.
- **Sanitize/validate HTML before PDF rendering** (`API-004`) — Medium complexity.
- **Complete the XSS review across all 78 `innerHTML` sites** (`APP-003`) — Small-Medium complexity.
- **Expand CI: lint, pip-audit, secret scan, and a real backend test suite as required merge gates** (`REL-006`, `TEST-001`) — Large complexity (the backend test suite is the bulk of the effort) but the single highest-leverage investment in this entire roadmap, since it makes every other fix in this document verifiable and non-regressing going forward.
- **Add a platform-admin and company-level audit log** (`OBS-001`, `OBS-002`) — Small-Medium complexity, new table(s) + write-through instrumentation.
- **Add structured logging + error monitoring** (`REL-003`) — Small complexity (a lightweight Sentry-style integration).
- **Confirm/fix staging-production database separation** (`REL-004`) — Medium complexity, blocked on item 4 above (schema must be accurate to replay onto a fresh staging DB).
- **Delete orphaned route files** (`REL-002`) — Small complexity, pure cleanup.
- **Fix `remove_image()` to actually delete storage objects** (`REL-005`) — Small complexity.

## P2 — Required before significant scaling

- **Optimistic concurrency on `quotes.quote_data`** (concurrent-editing gap, Phase 7 table) — Medium complexity, adds a version column and a conflict-detection check on save.
- **Quote-numbering server-side sequence + uniqueness constraint** (Phase 7 gap) — Medium complexity, needs careful migration since existing quotes' refs live in client-supplied JSONB today.
- **Consolidate or parity-test the four duplicated pricing implementations** (`QUO-002`) — Large complexity if consolidating (architectural change to work around Vercel's one-file-per-route constraint); Small if just adding the parity test as an interim safeguard.
- **Fix `list_companies()` and `pp_project_detail()`'s unbounded queries** (`PERF-001`, `PERF-002`) — Small-Medium complexity.
- **Add pagination to the remaining unbounded list endpoints** — Small complexity.
- **Add per-tenant rate limits / usage quotas** (ties into `SUB-001`) — Medium-Large complexity.
- **Awarded-quote reopening safeguard** (`QUO-001`) — Medium complexity, requires an explicit reopen action and automatic pre-edit snapshotting.
- **Build the subscription/entitlements foundation** (`SUB-001`) — Large complexity; this is a genuine "design before build" item — see `PROPOSED-TARGET-ARCHITECTURE.md`.

## P3 — Maturity improvements

- **Tenant data export and deletion workflow** (`PRIV-001`).
- **Subprocessor inventory and retention policy documentation** (`PRIV-001`).
- **Soft-delete pattern across tenant tables** (accidental-deletion recovery).
- **Granular permission model beyond the two-role + two-flag system** (if/when custom roles are actually needed by a customer — not needed preemptively).
- **Multi-company-per-user membership model** (only if a real customer need arises — the current 1:1 model is coherent and shouldn't be changed speculatively).
- **Global-admin impersonation with full audit controls** (only build if a genuine support-access need arises; the current "doesn't exist" state is safer than a half-built version).
- **Caching layer with tenant-aware keys** (once read volume actually justifies it).
- **Composite/tuned indexes based on real query-plan data** (once a live database is available to profile against).

## Suggested execution sequence

P0 items 1 and 5 can start immediately and in parallel (no dependencies, minutes of work). Item 4 should follow, then items 2 and 3 (2 can run in parallel with 4/3 once bucket privacy is flipped, but the signed-URL frontend work benefits from being tested against the now-accurate schema). Once all five P0 items are closed and the cross-tenant test (`TESTING-GAP-ANALYSIS.md` item 1) passes, the product can be reasonably classified as ready for a first trusted external pilot company, with P1 items following before a broader, self-serve launch.
