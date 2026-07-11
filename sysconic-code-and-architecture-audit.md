# Sysconic Quote Manager — Code & Architecture Audit

Scope: full pass over `api/*.py` and `index.html`, plus a specific check on whether staging data can leak into production.

---

## 1. Staging → Production data leak — verified NOT a code issue, but needs one manual check

Every backend file connects to Supabase the same way:

```python
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
```

There is **no hardcoded fallback URL or key anywhere** in the codebase. If an environment variable is missing, `create_client(None, None)` fails loudly — it cannot silently default to production's database. This means the code itself cannot cause staging writes to land in production.

That leaves one real possibility: **the two Vercel projects' environment variables are pointing at the same Supabase project.** This can only be confirmed from the Vercel dashboard, which I don't have access to. Please check:

- Open both `sysconic-quotes` (production) and `sysconic-quotes-staging` project settings → Environment Variables.
- Compare the `SUPABASE_URL` value on each. If they're identical, that's the leak — staging and production are literally the same database, and the fix is generating/pointing staging at its own separate Supabase project.
- While there, re-confirm the earlier fix is still in place: production's `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` should be scoped to **Production only**, not "Production and Preview" (this was flagged and fixed earlier this session — worth a re-check in case it drifted).

**Related but separate bug found:** `api/auth.py` hardcodes production's URL as the fallback for `APP_URL`:

```python
APP_URL = os.environ.get('APP_URL', 'https://sysconic-quotes.vercel.app')
```

If staging's Vercel project doesn't have its own `APP_URL` env var set, every invite email and password-reset email sent *from staging* will contain a link pointing at **production**. The recipient would click it and get "invalid or expired" errors, since their token only exists in staging's database. This doesn't cause data leakage, but it will look exactly like one from a user's perspective. Fix: set `APP_URL` explicitly on both projects, or remove the hardcoded fallback so a missing var fails loudly instead of silently pointing at production.

---

## 2. Backend corrections needed (by file)

**`api/zoho.py` — dead code, should be deleted.** This file is not registered in `vercel.json` at all, so it's unreachable in production. It's a leftover from before the per-company integrations rework: it uses global `ZOHO_CLIENT_ID`/`ZOHO_REFRESH_TOKEN`/etc. env vars (one Zoho account for the whole platform, not per-tenant), and its `sync-customers`/`sync-vendors` routes upsert on a column called `zoho_contact_id`, which no longer exists in the schema (it's `external_contact_id` now). If anyone ever re-registers this file thinking it's live, it will break immediately. Recommend deleting it.

**`api/integrations.py` — one dead route.** `search-customers` is not called from anywhere in `index.html` — the frontend now searches the local `customers` table (`/api/customers?search=`) instead, per the earlier fix. Safe to remove, or leave with a comment noting it's unused legacy.

**Inconsistent error handling across files.** `api/auth.py` and `api/platform.py` wrap every route body in `try/except` and return a clean `{'error': ...}` JSON response on failure. `api/quotes.py`, `api/customers.py`, `api/vendors.py`, and `api/products.py` do not — an unexpected exception (bad JSON body, a Supabase hiccup) will surface as a raw Flask 500 HTML error page instead of a JSON error the frontend can display. Worth bringing all files to the same standard.

**`api/quotes.py` has no pagination.** `list_quotes()` fetches every quote for a company with no `limit`/`offset`. Fine today; will slow down for any company with hundreds of quotes. `customers.py`/`vendors.py`/`products.py` already support `limit`/`offset` — quotes should get the same treatment before it becomes a problem.

**`api/platform.py` — `list_companies()` doesn't scale.** It pulls *every* row from `users` and `quotes` platform-wide just to compute per-company counts in Python. Fine while the platform is small; once there are real numbers of companies/users this should move to a count aggregation query (or a database view) instead of loading full tables into memory on every page load.

**`api/products.py` — SSRF exposure in image fetch.** `set_image()` will fetch any URL the client provides (`d.get('url')`) server-side with no restriction on internal/private IP ranges. A user could point this at internal infrastructure or a cloud metadata endpoint. Low risk today (any user with an account could already do more damage elsewhere), but worth adding a basic guard (reject non-public IPs) if this app will ever be exposed to less-trusted users.

**Model note:** `api/ai.py`, `api/bom.py`, and `api/proposal.py` all still use `gemini-2.5-flash` (10 requests/min, 250/day on the free tier). You already hit this rate limit once this session on the Proposal Generator. Switching all three to `gemini-3-flash` (1,500/day) is a one-line change per file and would meaningfully cut down on the 503 "high demand" errors — this is the offer I made earlier that's still open.

---

## 3. Frontend (`index.html`) corrections needed

**Fixed this session, but worth restating for the record:** the invite-acceptance screen didn't exist at all before today — the backend could validate an invite token, but nothing read `?invite=...` from the URL or showed a form to set a password. That's now built, along with the password-reset flow. Any invite links sent *before* today's fix are dead — worth re-sending any pending ones.

**JWT stored in `localStorage`, 30-day expiry, no revocation.** Standard for a lot of SPAs, but worth knowing the tradeoff: a stored-XSS anywhere in the app (or a stolen token via a shared/compromised machine) stays valid for up to 30 days with no way to revoke a single session — the only kill switch is rotating `JWT_SECRET`, which logs out everyone on the platform at once. Not urgent to fix, but worth knowing before this scales past a handful of trusted users.

**One file with a genuinely oversized inline asset.** The Print/PDF view still embeds a full base64 JPEG logo directly in the JS (tens of thousands of characters on one line) as the original hardcoded Sysconic default. It's no longer used when a company has its own logo configured (fixed this session), but it's still shipped to every browser on every page load. Worth removing entirely now that per-company logos work, both for page-weight and because it's the kind of line that makes the file hard to work with (it broke several of my own tooling attempts to read the file today).

**No client-side input length caps on a few free-text fields** (e.g., quote title, terms text) — the backend does cap most fields, but a few rely entirely on server-side truncation rather than also guiding the user in the UI. Cosmetic, not a real risk.

---

## 4. Does this meet standard SaaS-product architecture? Assessment

**What's solid:**
- Multi-tenancy via `company_id` scoping is applied consistently on every query I checked, across every table and every file.
- Auth is a reasonable JWT-claims model: `role` (company-level) and `is_platform_admin` (platform-level) are correctly kept as two independent, orthogonal dimensions.
- Per-company branding (Company Profile), per-company integrations (credentials never in global env vars), and a working global-admin/company-admin split are all in place — this is further along than a lot of early-stage SaaS products get.
- AI-assisted features (Auto-BOM, Proposal Generator, quote review) all ground their output in real data (catalog costs, live quote pricing) rather than letting the model invent numbers — good discipline.

**What's missing that a "standard" SaaS product would have:**

- **No Row-Level Security (RLS).** Every table's isolation depends entirely on every single backend route remembering to add `.eq('company_id', ...)`. It's been done consistently so far, but this is a single missed line away from a cross-tenant data leak, and there's nothing at the database layer to catch that mistake. Supabase supports RLS natively; worth enabling as defense-in-depth even with the service-role key bypassing it in normal operation — it becomes the safety net for the day someone forgets a filter.
- **No automated tests.** There's a `tests/` folder and a GitHub Actions workflow file, but I didn't see them exercised as part of this session's work — every feature this session was verified by manual clicking rather than a test suite. For a product onboarding paying customers, at minimum the pricing math (`calc_item`/`calc_opt`, duplicated in four different files) and the auth/invite/reset flows deserve real automated coverage, since a bug there is either a revenue-accuracy issue or a security issue.
- **No structured logging or error monitoring.** Errors mostly go to `print()`/`traceback.print_exc()`, visible only in Vercel's function logs if someone happens to check. No Sentry-style alerting means a broken feature in production could go unnoticed until a user reports it — which is close to what just happened with the password-reset email bug.
- **No rate limiting on public/unauthenticated endpoints.** `forgot-password`, `login`, `register`, and `accept-invite` have no throttling. Someone could hammer `/api/auth/login` or `/api/auth/forgot-password` with no pushback beyond Gemini's own external rate limits (which only apply to AI routes, not auth ones).
- **Business logic duplicated across files rather than shared.** This is a direct consequence of Vercel's one-file-per-route Python constraint (documented in the code comments, and a real constraint, not an oversight) — but it does mean `calc_item`/`calc_opt` pricing logic exists near-identically in `api/pdf.py`, `api/proposal.py`, and the frontend's JS. A pricing bug fix has to be applied in multiple places by hand, and that's exactly the kind of thing that quietly drifts out of sync over time. Worth a periodic "diff these functions against each other" check, or moving to a build step that can bundle shared modules if this becomes painful enough.
- **CORS is wide open** (`CORS(app)` with no origin restriction) on every backend file. Given this is bearer-token auth (not cookies), CSRF risk is lower than it would otherwise be, but it does mean any website could make authenticated calls on behalf of a user if it ever got hold of their token via another means (e.g. XSS). Worth restricting to the app's own domain(s) as a hardening step.

**Net assessment:** the core multi-tenant data model and the feature set are in genuinely good shape for the product's current stage — better than a lot of MVPs. What's missing is the "boring but load-bearing" SaaS infrastructure: tests, monitoring, RLS as a safety net, and rate limiting. None of these block usage today; all of them become more urgent the more real companies and real money run through this.

---

## 5. Suggested priority order

1. Confirm (from the Vercel dashboard) whether staging and production actually share a `SUPABASE_URL` — this is the one item I couldn't verify myself and is the most urgent given what you asked.
2. Set `APP_URL` explicitly on every environment so invite/reset emails never cross-link.
3. Delete `api/zoho.py` (dead, and would break if ever accidentally re-registered).
4. Bring `quotes.py`/`customers.py`/`vendors.py`/`products.py` error handling in line with `auth.py`'s try/except-everywhere pattern.
5. When there's time: RLS as a safety net, basic rate limiting on auth endpoints, and at least minimal automated tests around the pricing math.
