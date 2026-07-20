# Authentication, Authorization & Global Admin Audit — Phases 5 & 6

## Authentication audit

| Control | Status | Evidence |
|---|---|---|
| Secure login | **PASS** | `bcrypt.checkpw()` against a stored hash; generic "Invalid email or password" for both wrong-email and wrong-password cases (no user-enumeration signal on login itself) |
| Email verification | **FAIL / NOT FOUND** | No email-verification step exists at registration — an account is usable immediately with an unverified email |
| Password reset | **PASS** | Token-based, 1-hour expiry, single-use (`used` flag), generic "if that email is registered..." response regardless of whether the account exists (`api/auth.py` `forgot_password()`) |
| Session expiry | **PARTIAL** | 30-day fixed JWT expiry, no idle-timeout concept — see `AUTH-002` |
| Refresh token handling | **FAIL / NOT FOUND** | No refresh-token flow exists; the same long-lived token is used for the full 30 days |
| Logout / session revocation | **FAIL** | `doLogout()` only clears client-side `localStorage`; the token remains valid server-side until natural expiry — see `AUTH-002` |
| MFA readiness | **FAIL / NOT FOUND** | No TOTP/SMS/WebAuthn scaffolding anywhere in the schema or auth routes |
| Brute-force protection | **FAIL** | See `AUTH-003` |
| Rate limiting | **FAIL** | See `AUTH-003` |
| Account lockout / abuse protection | **FAIL / NOT FOUND** | No failed-attempt counter or lockout logic |
| Invitation flow | **PASS** | Token-based, 7-day expiry, role/can_review/can_view_all_quotes carried onto acceptance; correctly scoped to the inviting admin's own `company_id` |
| Duplicate invitations | **PARTIAL** | `invite()` checks for an existing *member* with that email in the company, but does not check for an existing *pending invite* to the same email before creating a new one — repeated invites to the same address are possible (low-severity nuisance, not a security issue) |
| Expired invitations | **PASS** | `accept_invite()`/`register()`'s pending-invite lookup both filter `.gt('expires_at', now)` |
| Removed users | **PARTIAL** | No explicit "remove team member" endpoint was found in `api/auth.py`'s team-management routes (role change and can_review/can_view_all_quotes toggles exist; a hard-delete-user route was not found) — meaning offboarding a user may currently only be achievable via direct database action, which is itself an operational gap worth confirming with the product owner |
| Suspended companies | **PASS** | Checked at both `login()` and `me()` — a suspended company's users are blocked with a clear message |
| Disabled accounts | **NOT APPLICABLE** | No per-user (as opposed to per-company) disable/suspend flag exists — only company-level suspension |
| User enumeration | **PASS** | Both `login()` and `forgot_password()` return generic messages; `register()` does reveal "Email already registered" (409) on duplicate signup, which is a mild, common, and generally accepted enumeration surface for registration flows specifically (not flagged as a separate finding — low severity, industry-typical trade-off for good UX on this particular endpoint) |
| Secure cookie configuration | **NOT APPLICABLE** | No cookies are used for auth at all (Bearer token in Authorization header only) |
| CSRF protection | **NOT APPLICABLE (by design)** | Token-in-header auth is not vulnerable to classic CSRF; the residual risk is XSS-driven token theft, covered under `APP-003`/`AUTH-002` |
| OAuth callback security | **NOT APPLICABLE** | The app itself does not implement an OAuth login flow for end users (Microsoft Graph and Zoho OAuth are server-to-server / admin-configured integration credentials, not a user-facing "Sign in with..." flow) |
| Open redirects | **NOT VERIFIED** | Invite/reset links are built from `get_app_url()`, which prefers an explicit `APP_URL` env var and falls back to the incoming request's own `request.url_root` — this self-detection means the link always points back at whichever host issued it, which avoids a hardcoded-wrong-environment bug (previously an actual issue per `sysconic-code-and-architecture-audit.md`) but was not stress-tested here for header-injection/Host-header trust issues in the Vercel runtime specifically |

## Authorization audit

| Capability | Supported? | Evidence |
|---|---|---|
| User-to-company membership | Yes, 1:1 only | `users.company_id` single FK |
| One user belonging to multiple companies | **No** | Would require a real schema migration to a membership join table |
| Active company selection | **Not applicable** (follows from 1:1 above) | — |
| Role per company | Yes | `users.role` is itself company-scoped since a user belongs to one company |
| Permission per company | Partial | Only the two boolean add-ons (`can_review`, `can_view_all_quotes`), not a general permission system |
| Custom roles | **No** | Only `admin`/`user` at the database level (`CHECK` constraint, `schema.sql:23`) |
| Least-privilege access | Partial | The two boolean flags do allow narrower-than-admin grants for specific capabilities, which is a reasonable minimal implementation of least privilege even without full custom roles |
| Separation between tenant admin and SaaS global admin | **Yes, cleanly** | `is_platform_admin` is a fully independent boolean claim; platform admins live in a dedicated internal, non-customer company row |
| Approval limits | **No** | Reviewer-assignment-based approval, not monetary-threshold-based |
| Department/branch restrictions | **No** | Not present in the current data model |
| Record ownership where required | **Yes** | `created_by` on quotes/products/customers/vendors/projects consistently drives ownership-based visibility (`_can_view_quote`, `_can_edit_quote`) |

## Permission matrix (as currently implemented)

| Action | Company `user` | Company `user` + `can_review` | Company `user` + `can_view_all_quotes` | Company `admin` | Platform admin (`is_platform_admin`) |
|---|---|---|---|---|---|
| View own quotes | Yes | Yes | Yes | Yes | No (never queries quote content) |
| View all company quotes | No | No | Yes | Yes | No |
| Edit/delete own quotes | Yes (delete: creator or admin only) | Yes | Yes | Yes | No |
| Edit/delete teammate's quote | No | No | Yes (edit only, per `_can_edit_quote`) | Yes | No |
| Submit quote for review | Creator or `can_view_all_quotes`/admin | Same | Yes | Yes | No |
| Be assigned as a reviewer | No (must have `can_review`) | Yes | N/A | Yes (as a person, if also flagged) | No |
| Approve / request changes on an assigned review | Only if personally assigned | Only if personally assigned | Only if personally assigned | Only if personally assigned, **plus** admin-override path (force-approve/cancel/reassign) | No |
| View a quote's internal review versions | Creator, `can_view_all_quotes`, admin, or assigned reviewer (**gap: currently NOT enforced** — see `AUTH-001`) | — | — | — | No |
| Manage products/customers/vendors | Yes (any authenticated company user) | Yes | Yes | Yes | No |
| Manage team (invite/role/flags) | No | No | No | Yes | No (uses a separate platform-level invite-to-company route) |
| Manage company profile/branding | View: yes. Edit: no | Same | Same | Yes | No |
| Manage integrations (Zoho connect/sync) | No | No | No | Yes | No |
| Create/edit projects (Projects feature) | Yes, if feature enabled | Yes | Yes | Yes, plus delete | No |
| Project Performance: view | Only own (as PM/salesperson) if feature enabled | Same | Same | Yes (all) | No |
| Project Performance: manage (forecasts, budget revisions, cost categories) | No | No | No | Yes (or `can_manage_project_performance` flag) | No |
| View platform-wide company list/usage counts | No | No | No | No | Yes |
| Suspend/reactivate a company | No | No | No | No | Yes |
| Create a new company + admin | No | No | No | No | Yes |
| Create another platform admin | No | No | No | No | Yes |
| Invite a user into an arbitrary company | No | No | No | No | Yes |
| View/edit another company's business data | No | No | No | No | **No** (verified — platform routes never touch quote/product/customer content) |
| Impersonate a tenant user | No | No | No | No | **No such feature exists** |

## Frontend-only authorization checks

**None found that lack a matching server-side check.** Every role-gated UI action spot-checked (delete quote, manage team, platform company management, integrations connect/sync/disconnect, company profile editing) has a corresponding server-side `claims['role'] != 'admin'` / `require_platform_admin(claims)` / `_can_edit_quote()` gate. This was checked by tracing each privileged frontend action to its backend route rather than assumed.

## Tenant-admin privilege-escalation test (Phase 5's specific checklist)

| Can a normal tenant `admin`... | Result |
|---|---|
| Access global administration routes (`/api/platform/*`) | **No** — every route in `api/platform.py` calls `require_platform_admin(claims)`, which checks the `is_platform_admin` JWT claim, not `role` |
| View another company | **No** | 
| Change subscription data | **N/A — no subscription data model exists yet** |
| Impersonate users | **No — feature doesn't exist for anyone, including platform admins** |
| Alter protected system settings | **No** — no such settings surface exists |
| Assign themselves global privileges | **No** — `is_platform_admin` is never settable via any tenant-facing route; only `create_platform_admin()` (itself gated behind `require_platform_admin`) can set it |
| Change `company_id` in requests | **No** — `company_id` is never read from the request body/query/path anywhere |
| Access server-only APIs (e.g. the cron sync endpoints) | **No** — those require `CRON_SECRET`, which a tenant admin's JWT does not satisfy |

## Global administration & support access — Phase 6

| Capability | Status |
|---|---|
| Tenant creation | **PASS** — `create_company()` |
| Tenant suspension / activation | **PASS** — `suspend_company()`/`reactivate_company()` |
| Subscription assignment | **NOT IMPLEMENTED** — only a free-text `plan` field is set at creation, no ongoing subscription management |
| Usage visibility | **PASS (basic)** — user/quote counts per company; no storage/API/AI-usage metering |
| Health monitoring | **NOT IMPLEMENTED** |
| Support operations | **NOT IMPLEMENTED** — no ticketing/context surface |
| Feature flags | **PARTIAL** — `companies.features` JSONB exists and gates Project Performance, but there is no admin UI/route to manage it found in `platform.py`; it appears to be set only via direct database action today |
| Tenant configuration | **PARTIAL** — company profile/branding is tenant-self-service; no platform-admin override UI |
| Impersonation with controls | **NOT IMPLEMENTED** — and, notably, not implemented at all rather than implemented-insecurely, which is the safer of the two failure modes |
| Audit logs | **NOT IMPLEMENTED** — `OBS-001` |
| Data export | **NOT IMPLEMENTED** — `PRIV-001` |
| Tenant deletion workflow | **NOT IMPLEMENTED** — only suspend/reactivate exist; no delete-a-tenant route |
| Billing status | **NOT IMPLEMENTED** |
| Support ticket context | **NOT IMPLEMENTED** |

**Impersonation-readiness assessment:** since no impersonation mechanism exists today, there is nothing to audit for the specific sub-checklist (explicit authorization, reason capture, time-limited session, visible banner, full audit trail, start/end events, read-only mode, sensitive-action prevention, customer notification, no credential access). If/when impersonation is built, it should be designed against that full checklist from day one — see `PROPOSED-TARGET-ARCHITECTURE.md` for a recommended shape (a `support_sessions` table: `platform_admin_id, company_id, reason, started_at, expires_at, ended_at`, mirroring the model already sketched in the repo's own `multi-tenant-audit-spec.md`).
