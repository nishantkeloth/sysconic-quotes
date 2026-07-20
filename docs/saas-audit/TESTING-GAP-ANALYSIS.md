# Testing Maturity & Gap Analysis — Phase 18

## What exists today

`tests/run-tests.mjs` — a single, hand-rolled Node test file (no Jest/Mocha/Vitest) that boots the real `index.html` inside `jsdom` with a mocked `fetch`, then exercises: the client-side pricing formula (`calcItem`), a specific prior regression (typed text surviving a redraw — a real bug this test was written to catch and does catch), quote structure operations (add/delete item, add option), VAT math, the save-payload shape, and multi-option print-layout rendering. This is a genuinely well-targeted, evidence-driven test file — it exists because of real bugs that happened, not as boilerplate. It is wired into CI (`.github/workflows/tests.yml`) and runs on every push/PR to `dev`/`staging`/`main`.

**What it does not, and cannot, cover:** anything server-side. It never calls the real Flask backend, never touches a real or mocked Supabase instance, and therefore has zero coverage of authentication, authorization, tenant isolation, RLS, the review/approval workflow's server-side rules, or the Zoho/Graph/Gemini integrations.

## Coverage against the requested checklist

| Test type | Exists? |
|---|---|
| Unit tests (frontend pricing/logic) | Yes — partial, good quality where it exists |
| Unit tests (backend) | **No** |
| Integration tests | **No** |
| API tests | **No** |
| Database policy tests | **No** |
| RLS tests | **No** |
| Cross-tenant tests | **No — this is the single most important gap in the entire audit** |
| Authorization tests | **No** |
| End-to-end tests | **No** |
| Quote calculation tests | Yes (frontend only) — no equivalent test exists for the three server-side duplicate implementations (`api/pdf.py`, `api/proposal.py`, `api/quotes.py`'s `_calc_item_pp`), so `QUO-002`'s four-way-drift risk is itself untested |
| Currency/rounding tests | Partial (VAT math tested on the frontend only) |
| Approval workflow tests | **No** — the server-side reviewer-assignment/decision logic (the most business-critical and most carefully-written part of `api/quotes.py`) has zero automated coverage |
| File access tests | **No** |
| Subscription tests | **Not applicable yet** (no subscription feature exists) |
| Migration tests | **No** |
| Load tests | **No** |
| Security tests | **No** |
| Backup/restore tests | **No** |
| Smoke tests | **No** |

## Recommended minimum regression suite (priority order)

**1. Cross-tenant isolation test (highest priority — the audit's own gating requirement).** Exactly the scenario laid out in `MULTI-TENANCY-AUDIT.md`'s "Proposed cross-tenant automated test" section: two tenants, one object of each type per tenant, every read/write/delete attempted across the boundary, plus the two currently-open exceptions (public storage buckets, `AUTH-001`) explicitly asserted as known-failing until fixed. This single test, run in CI on every PR, is what should ultimately let this product move from "NOT READY" to "READY."

**2. Authorization/permission tests.** For each row in `AUTHORIZATION-MATRIX.md`'s permission matrix: attempt the action as a role that should be denied, assert 403/404; attempt as a role that should be allowed, assert success. Specifically include a regression test for `AUTH-001` (non-privileged teammate hitting `/versions`, `/versions/<vid>`, and `/pdf/quote/<qid>`) so it cannot silently reopen after being fixed.

**3. Pricing-formula parity test.** Feed an identical fixed line-item input through all four independent implementations (`api/pdf.py`, `api/proposal.py`, `api/quotes.py`, `index.html`'s JS) and assert byte-identical output. This is the direct test for `QUO-002` and would have caught any future drift immediately rather than relying on manual code review to notice it.

**4. Auth lifecycle tests.** Register → login → invite → accept-invite → forgot-password → reset-password → logout, including the edge cases already partially covered by the code (expired invite, already-accepted invite, suspended company, generic anti-enumeration responses) — currently verified only by manual reading, not by an automated assertion.

**5. RLS policy tests (once `TEN-001` is closed).** A pgTAP-style test (or an equivalent script using a lower-privileged Supabase key) asserting every tenant table denies cross-company access at the database layer, independent of and in addition to the application-layer test in item 1.

**6. Review/approval workflow tests.** Submit for review → assigned reviewer approves/requests changes → aggregate status recomputes correctly → admin override paths (force-approve, cancel, reassign) all behave as documented in the code's own extensive comments. This workflow is the most carefully engineered part of the codebase and currently has the least test coverage relative to its complexity.

**7. File-access tests.** Once `TEN-002` is closed (signed URLs), assert a Tenant-B file URL is inaccessible to Tenant A and to an anonymous request; assert a legitimate signed URL expires as configured.

**8. Migration/schema tests.** Once `REL-001` is closed (accurate, tracked schema), a CI check that the tracked schema matches a live schema dump, preventing this specific drift from recurring.

**9. Load test.** A basic k6/Locust script against `list_quotes`, `list_products` (with search), and `pp_project_detail` at a synthetic multi-thousand-row scale, to get real numbers behind the `PERFORMANCE-SCALABILITY-AUDIT.md` assessment rather than architectural reasoning alone.

**10. Smoke test (post-deploy).** A minimal script (register a throwaway test company, log in, create a quote, generate a PDF, tear down) run automatically against `staging` immediately after every deploy — the cheapest possible test to write and the most direct protection against a broken production release, and a natural fit for `REL-006`'s expanded CI.

## Coverage estimate
Given the total surface area (12 backend route files covering roughly 90+ endpoints, plus the frontend), current automated test coverage of the *backend* is effectively **0%** by any meaningful measure — the one existing test file, while good, tests the frontend in isolation with the backend entirely mocked out. Frontend coverage of the specific behaviors it targets (pricing math, typing persistence, print layout) is solid but narrow relative to the full 4,471-line file's surface area.
