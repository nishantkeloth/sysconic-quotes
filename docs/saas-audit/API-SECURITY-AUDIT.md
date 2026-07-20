# API Security Audit — Phase 8

All 12 deployed route files were read in full except the tail of `api/projects.py` (lines 1030–1334, containing additional Zoho project-import/targeted-sync routes referenced by `vercel.json`'s `/api/pp-sync/*` routes) — that section is architecturally consistent with the rest of the file (same `verify_token`/`_require_pp`/`company_id` pattern sampled extensively elsewhere in the file) but its individual routes are not enumerated line-by-line below; flagged **NOT FULLY ENUMERATED** rather than guessed.

Legend: **Tenant source** = where `company_id` comes from. **Perm** = additional authorization beyond "any authenticated user". All routes use JWT Bearer auth unless noted.

## api/auth.py

| Method | Route | Auth required | Tenant source | Permission | Input validation | Sensitive data returned | Cross-tenant risk |
|---|---|---|---|---|---|---|---|
| POST | `/api/auth/register` | No (public) | New/existing company via invite lookup | None | Required-field check, password length ≥8, email-exists check | JWT (self) | None — self-scoped |
| POST | `/api/auth/login` | No (public) | Looked-up user's own row | None | Required fields | JWT (self) | None |
| GET | `/api/auth/me` | Yes | JWT | None | — | User + company profile | None |
| PUT | `/api/auth/me/ms365-email` | Yes | JWT | None (self-only) | Non-empty string | — | None |
| POST | `/api/auth/invite` | Yes | JWT | `role == 'admin'` | Email presence, duplicate-membership check | Invite URL/token | None |
| POST | `/api/auth/accept-invite` | No (token-gated) | Invite's `company_id` | Valid, unexpired, unaccepted token | — | JWT | None |
| GET | `/api/auth/invite-details` | No (public, token-gated) | Invite's `company_id` | Valid unaccepted token | — | Email/role/company name only | Low — intentionally public preview, minimal data |
| POST | `/api/auth/forgot-password` | No (public) | Looked-up user | None | — | Generic message always | None (anti-enumeration) |
| POST | `/api/auth/reset-password` | No (token-gated) | Token's user | Valid, unused, unexpired token | Password length ≥8 | — | None |
| POST | `/api/auth/team/<user_id>/reset-password` | Yes | JWT | `role == 'admin'`, target scoped to `.eq('company_id', ...)`, platform-admin targets blocked | — | Reset URL | None |
| GET | `/api/auth/team` | Yes | JWT | `role == 'admin'` | — | Team roster (excludes platform admins) | None |
| PUT | `/api/auth/team/<user_id>/role` | Yes | JWT | `role == 'admin'`, target `.eq('company_id',...)`, last-admin guard, platform-admin blocked | Role in (admin,user) | — | None |
| PUT | `/api/auth/team/<user_id>/can-review` | Yes | JWT | Same pattern | Bool coercion | — | None |
| PUT | `/api/auth/team/<user_id>/can-view-all-quotes` | Yes | JWT | Same pattern | Bool coercion | — | None |
| PUT | `/api/auth/invites/<token>/role`, `/can-review`, `/can-view-all-quotes` | Yes | JWT | `role == 'admin'`, invite `.eq('company_id',...)` | — | — | None |
| GET/PUT | `/api/auth/company-profile` | Yes | JWT | GET: any user. PUT: `role == 'admin'` | Field whitelist, 500-char truncation | Company profile (no secrets) | None |
| POST | `/api/auth/company-profile/logo` | Yes | JWT | `role == 'admin'` | Content-type/size (4MB) checks | Public URL | **TEN-002** (bucket is public) |

## api/quotes.py

| Method | Route | Auth | Tenant source | Permission | Validation | Sensitive data | Cross-tenant risk |
|---|---|---|---|---|---|---|---|
| GET | `/api/quotes` | Yes | JWT | `_can_view_all_quotes` scoping | limit≤1000, offset≥0 | Quote list (own company only) | None |
| GET | `/api/quotes/<qid>` | Yes | JWT + `.eq('company_id',...)` | `_can_view_quote` | — | Full quote incl. pricing | None |
| GET | `/api/quotes/<qid>/activity` | Yes | Same | `_can_view_quote` | — | Activity log + actor names | None |
| POST | `/api/quotes` | Yes | JWT | Any user | — | — | None |
| POST | `/api/quotes/<qid>/create-project` | Yes | JWT + company scope | `_can_edit_quote`, feature flag `project_performance` | Awarded-status check, won_option_index range check | Project id | None |
| PUT | `/api/quotes/<qid>` | Yes | JWT + company scope | `_can_edit_quote`, draft-lock (423 if locked) | Field whitelist | — | None |
| DELETE | `/api/quotes/<qid>` | Yes | JWT + company scope | admin or creator | — | — | None |
| POST | `/api/quotes/<qid>/duplicate` | Yes | JWT + company scope | `_can_edit_quote` | — | — | None |
| GET | `/api/quotes/company-users` | Yes | JWT | Any user | — | id/name/email only | None |
| GET | `/api/quotes/reviewer-candidates` | Yes | JWT | Any user | — | id/name/email only | None |
| POST | `/api/quotes/<qid>/submit-review` | Yes | JWT + company scope | `_can_edit_quote`; reviewer_ids re-validated `.eq('company_id',...).eq('can_review',True)` | Non-empty reviewer list | — | None |
| GET | `/api/quotes/<qid>/versions` | Yes | JWT + company scope | **Missing `_can_view_quote` — `AUTH-001`** | — | Version metadata | **Same-company IDOR (AUTH-001)** |
| GET | `/api/quotes/<qid>/versions/<vid>` | Yes | JWT + company scope | **Missing `_can_view_quote` — `AUTH-001`** | — | Full pricing snapshot | **Same-company IDOR (AUTH-001)** |
| GET | `/api/quotes/<qid>/customer-sends`, `/customer-sends/<sid>` | Yes | JWT + company scope | `_can_view_quote` (correctly present here) | — | Sent-quote snapshot | None |
| GET | `/api/quotes/review-queue` | Yes | JWT | Self-scoped, or admin `?scope=all` | — | — | None |
| POST | `/api/quotes/<qid>/versions/<vid>/approve`, `/request-changes` | Yes | JWT + company scope | Must be assigned reviewer on that version | Comment required for request-changes | — | None |
| POST | `/api/quotes/<qid>/versions/<vid>/admin-decide`, `/cancel-review`, `/reassign-reviewers` | Yes | JWT + company scope | `role == 'admin'` | Decision enum, comment-required-for-reject | — | None |
| POST | `/api/quotes/<qid>/email` | Yes | JWT + company scope | `_can_edit_quote` | Recipient/HTML required | Sends real customer email + PDF | None (but see `API-004`) |

## api/products.py, api/customers.py, api/vendors.py

Uniform pattern across all three files, verified independently for each: every list/create/update/delete/bulk-import route requires a valid JWT and scopes every query with `.eq('company_id', claims['company_id'])`; update/delete first verify the target row belongs to the caller's company before acting (returns 404 otherwise, not a cross-company error that would reveal existence). Bulk import caps: products 500/request, customers/vendors 1000/request. `POST /api/products/<pid>/image` and `image-search`/`online-search` additionally call the external Brave Search API and an SSRF guard (`is_safe_public_url()`) before fetching a user-supplied image URL — a solid, specific control. `DELETE /api/products/<pid>/image` never removes the underlying storage object (`REL-005`).

| Method | Route | Auth | Tenant source | Permission | Validation | Cross-tenant risk |
|---|---|---|---|---|---|---|
| GET/POST | `/api/products`, `/api/customers`, `/api/vendors` | Yes | JWT | Any user | limit≤100, offset≥0 | None |
| PUT/DELETE | `/api/products/<id>`, `/api/customers/<id>`, `/api/vendors/<id>` | Yes | JWT + company scope | Any user | — | None |
| POST | `/api/products/bulk`, `/api/customers/bulk`, `/api/vendors/bulk` | Yes | JWT | Any user | Item-count caps | None |
| GET | `/api/products/image-search`, `/api/products/online-search` | Yes | JWT (for products/BRAVE key check) | Any user | Query required | None (queries external service only) |
| POST/DELETE | `/api/products/<pid>/image` | Yes | JWT + company scope | Any user | Content-type/size/URL-safety checks | `TEN-002` (public bucket) |

## api/vendors.py duplication note
`clean_vendor`/`clean_customer`/`clean_product` all whitelist fields and truncate to 500 chars — good baseline input validation against mass-assignment. No email-format or phone-format validation was found (accepted as free text), which is a minor data-quality gap, not a security one.

## api/pdf.py

| Method | Route | Auth | Tenant source | Permission | Validation | Sensitive data | Cross-tenant risk |
|---|---|---|---|---|---|---|---|
| GET | `/api/pdf/quote/<qid>` | Yes | JWT + `.eq('company_id',...)` | Any user (no `_can_view_quote` check — **note**: unlike `/api/quotes/<qid>`, this route has no visibility-scoping beyond company membership; a plain user could fetch a teammate's private quote's PDF even though they couldn't fetch its JSON via `GET /api/quotes/<qid>`) | `which` param defaults to 'all' | Full PDF incl. pricing | **Same-company IDOR, same class as `AUTH-001`, not previously catalogued — see `FINDINGS.json` `AUTH-001` note extension** |
| POST | `/api/pdf/render` | Yes | JWT (not company-scoped — takes raw HTML from the request body, not a DB-fetched quote) | Any user | Filename sanitization | Renders arbitrary client-submitted HTML | See `API-004` |

**New finding surfaced here:** `quote_pdf()` in `api/pdf.py` does not call `_can_view_quote()`-equivalent logic before generating and returning the PDF, unlike `get_quote()` in `api/quotes.py`. This is the same class of gap as `AUTH-001` (same-company, not cross-tenant) and has been folded into `AUTH-001`'s scope in `FINDINGS.json` rather than duplicated as a separate ID — treat both `get_version()`/`list_versions()` and `quote_pdf()` as needing the same fix.

## api/ai.py, api/bom.py, api/proposal.py

| Method | Route | Auth | Tenant source | Permission | Validation | Sensitive data sent externally | Cross-tenant risk |
|---|---|---|---|---|---|---|---|
| POST | `/api/ai/review` | Yes | None (no DB query — quote JSON comes from request body) | Any user | Quote object required | Full quote content → Google Gemini | None (nothing to leak from DB) |
| POST | `/api/bom/generate` | Yes | JWT (product catalog fetch scoped) | Any user | Description length cap, currency cap | Company's product catalog (cost/margin) → Gemini | None — scoped before send |
| POST | `/api/proposal/draft` | Yes | JWT (quote fetch scoped) | Any user | Attachment size cap 8MB, brief/attachment required | Quote equipment summary + attachment text → Gemini | None — scoped before send |
| POST | `/api/proposal/render` | Yes | JWT (quote fetch scoped) | Any user | `kind` enum, content.title required | Generates + stores PDF publicly | `TEN-002` |

## api/platform.py

All routes gated by `require_platform_admin(claims)` (checks `is_platform_admin` JWT claim). See `AUTHORIZATION-MATRIX.md` for the full breakdown; no route in this file exposes tenant business content, only metadata/counts.

| Method | Route | Auth | Permission | Cross-tenant risk |
|---|---|---|---|---|
| GET/POST | `/api/platform/companies` | Yes | Platform admin | None — metadata/counts only (see `PERF-001` for the unbounded-query scalability note) |
| POST | `/api/platform/companies/<cid>/suspend`, `/reactivate` | Yes | Platform admin | None (but `OBS-001` — unaudited) |
| POST | `/api/platform/companies/<cid>/invite` | Yes | Platform admin | None — explicitly targets the named `cid`, correctly blocks inviting into the internal company |
| GET/POST | `/api/platform/admins` | Yes | Platform admin | None |

## api/integrations.py

| Method | Route | Auth | Tenant source | Permission | Validation | Cross-tenant risk |
|---|---|---|---|---|---|---|
| GET | `/api/integrations` | Yes | JWT | admin | — | None (masked credential display) |
| POST | `/api/integrations/connect` | Yes | JWT | admin | Required-field check via adapter, live connection test | None |
| DELETE | `/api/integrations/<provider>` | Yes | JWT | admin | — | None |
| GET/POST | `/api/integrations/sync-config` | Yes | JWT | admin | Provider must already be connected | None |
| POST | `/api/integrations/sync-customers`, `/sync-vendors` | Yes | JWT | admin | — | None |
| GET/POST | `/api/integrations/run-auto-sync` | **`CRON_SECRET`, not user JWT** | Iterates all companies with `auto_sync_enabled=true` | `CRON_SECRET` match | — | None — correctly scoped per-company inside the loop |
| POST | `/api/integrations/zoho/create-project` | Yes | JWT + company scope | Feature flag `project_performance` | Multiple required-field/state checks | None |

## api/projects.py (incl. merged Project Performance routes)

Uniform pattern verified: every route requires a JWT, checks `has_feature(claims, 'projects')` or `_require_pp(claims)` (which itself checks `has_feature(claims, 'project_performance')`), and scopes to `company_id`. Record-level access within Project Performance additionally narrows to the assigned Project Manager/Salesperson for non-management users (`_project_access()`), mirroring the quotes module's ownership model.

| Method | Route | Auth | Tenant/record scope | Permission | Cross-tenant risk |
|---|---|---|---|---|---|
| GET/POST | `/api/projects` | Yes | JWT | Feature flag | None |
| GET/PUT/DELETE | `/api/projects/<pid>` | Yes | Company scope | Feature flag (+ admin for delete) | None |
| GET | `/api/project-performance` (portfolio) | Yes | Company scope + `_project_access` for non-managers | Feature flag | None |
| GET | `/api/project-performance/<pid>` | Yes | Company scope + `_project_access` | Feature flag | None |
| POST | `/api/project-performance/<pid>/recalculate` | Yes | Company scope + `_project_access` | Feature flag | None |
| POST/PUT/DELETE | `/api/project-performance/<pid>/forecast[/<fid>]` | Yes | Company scope + `_project_access` | Feature flag; removal requires a reason | None |
| POST | `/api/project-performance/<pid>/completion` | Yes | Company scope + `_project_access` | Feature flag | None |
| POST | `/api/project-performance/<pid>/budget-revision` | Yes | Company scope | Feature flag + `can_manage` | None |
| GET/POST | `/api/project-performance/cost-categories` | Yes | Company scope | Feature flag (+ `can_manage` for POST) | None |
| GET/PUT | `/api/project-performance/alerts[/<aid>]` | Yes | Company scope | Feature flag | Status enum validated |
| — (untitled remainder) | `/api/pp-sync/*` routes (targeted/portfolio sync, Zoho project import) | **NOT FULLY ENUMERATED** — see file-level note above | Presumed company-scoped per the pattern sampled throughout this file | Presumed feature-flag gated | **NOT FULLY VERIFIED** — recommend a follow-up read of `api/projects.py` lines 1030–1334 before treating this section as cleared |

## Cross-cutting API security checklist

| Check | Status |
|---|---|
| Broken object-level authorization | `AUTH-001` (and its `quote_pdf()` extension above) — otherwise not found |
| Broken function-level authorization | Not found — every admin/platform-admin-only route was verified to check the correct claim |
| Mass assignment | **PASS** — every create/update route uses an explicit field whitelist (`clean_product`, `clean_customer`, `clean_vendor`, `CONTENT_FIELDS`/`allowed` list in `update_quote`, `COMPANY_PROFILE_FIELDS`), never `**request.json` |
| Excessive data exposure | **PARTIAL** — `quote_pdf()`/`get_version()`/`list_versions()` over-expose relative to their intended audience (see `AUTH-001`); otherwise responses are reasonably scoped |
| Missing input validation | **PARTIAL** — most routes validate required fields and types; discount/margin bounds are not strictly validated everywhere (noted in `MULTI-TENANCY-AUDIT.md`'s Phase 7 table) |
| SQL injection | **NOT APPLICABLE / PASS** — all queries go through the Supabase query builder (parameterized), no raw SQL string concatenation found anywhere |
| Command injection | **PASS** — no `subprocess`/`os.system`/`eval` usage found in any api/*.py file |
| SSRF | **PARTIAL** — `api/products.py`'s `set_image()` has an explicit, well-designed SSRF guard (`is_safe_public_url()`, rejects private/loopback/link-local/reserved IPs); `_fetch_logo_bytes()` (api/pdf.py) and `_fetch_bytes()` (api/proposal.py), which fetch a company's own stored `logo_url`, have **no equivalent guard** — lower risk since the URL originates from the company's own prior upload, not a fresh user-supplied field per request, but worth the same defense-in-depth treatment |
| Path traversal | **PASS** — storage paths are built from `company_id` + generated identifiers/timestamps, never from raw user-supplied filenames without sanitization (`render_pdf()`'s filename is regex-stripped of `\r\n"\\`) |
| Unsafe file uploads | **PARTIAL** — content-type and size are validated for logo/product-image uploads; there is no server-side magic-byte verification (only the client-declared `content_type` is trusted for the extension mapping) — a mismatched declared-vs-actual content type would not be caught |
| XSS | See `APP-003` (not exhaustively verified) |
| CSRF | Not applicable (see Authentication table) |
| Open redirects | Not verified (see Authentication table) |
| Unrestricted resource consumption | `PERF-001`, `PERF-002` |
| Missing rate limits | `AUTH-003` |
| Missing request size limits | **NOT VERIFIED** — Flask's default `MAX_CONTENT_LENGTH` is unset in every file (no explicit cap found), relying entirely on Vercel's platform-level request body size limit as the backstop |
| Verbose internal errors | `API-002` |
| Secrets in responses | **PASS** — `mask()` is correctly applied to integration credentials in API responses; no other route was found to echo a secret value |
| Unsafe CORS | `API-001` |
| Unsafe webhook processing | **NOT APPLICABLE** — no inbound webhooks exist |
| Missing webhook signature validation | **NOT APPLICABLE** (same reason) |
| Replay attacks | **PARTIAL** — the cron endpoints accept a static `CRON_SECRET` with no nonce/timestamp, so a captured value is replayable indefinitely until rotated; low real-world risk since it's a bearer secret over HTTPS, not a signed, time-boxed request |
| Missing idempotency keys | `API-003` |
| Public endpoints that should be private | **PASS** — every intentionally-public endpoint (register/login/forgot-password/reset-password/accept-invite/invite-details) is a legitimate pre-auth flow; none were found to be accidentally public |
