# Application Security Audit — Phases 9, 10 & 11

## Phase 9: File, image & document security

| Check | Status | Evidence |
|---|---|---|
| Tenant-specific storage paths | **PASS** | `{company_id}/{...}` prefix on every upload path (products, logos, proposals) |
| Private vs. public buckets | **FAIL** | All three buckets public — `TEN-002` |
| File authorization before download | **FAIL** | No check at all — public bucket, unsigned URL |
| Signed URL generation | **NOT IMPLEMENTED** | Zero `create_signed_url` calls anywhere |
| Signed URL expiry | **NOT APPLICABLE** (none generated) | — |
| File type validation | **PARTIAL** | Content-type checked against an allowlist (`image/jpeg`,`png`,`webp`,`gif` for images; PDF only for proposals) before mapping to a file extension |
| MIME type verification | **PARTIAL** | Only the **client-declared** `Content-Type` is trusted — no server-side magic-byte/signature check of the actual file content |
| File extension validation | **PASS** (derived from validated content-type, not from a user-supplied filename) | — |
| File size limits | **PASS** | 4MB (logo, product image), 8MB (proposal reference attachment) — enforced server-side, not just client-side |
| Malware scanning readiness | **FAIL / NOT IMPLEMENTED** | No AV/malware scan step on any upload path |
| Image processing risks | **NOT APPLICABLE** | No server-side image transformation/resizing library is used (images are stored as-uploaded), so no image-processing-library CVE surface exists |
| Executable files | **PASS (implicitly)** | Content-type allowlist for images excludes executables; the proposal bucket only ever receives server-generated PDFs, never arbitrary user uploads |
| SVG risks | **PASS** | `image/svg+xml` is not in the accepted content-type list for either logo or product-image upload — SVG's script-execution risk is avoided by exclusion |
| Path traversal | **PASS** | Paths are constructed server-side from `company_id` + generated IDs/timestamps, never from a raw user-supplied filename |
| Filename sanitization | **PASS** where filenames are used at all (`render_pdf()`'s `filename` param is regex-stripped) |
| Duplicate file handling | **PASS** | Timestamp-suffixed paths avoid overwrite collisions |
| Orphaned files | **FAIL** | `REL-005` — `remove_image()` never deletes the storage object |
| Deletion lifecycle | **PARTIAL** | Deleting a product/quote does not cascade-delete its associated storage objects (logo/image/proposal files persist after the owning row is gone) |
| Data retention | **NOT IMPLEMENTED** | No retention policy or scheduled purge job for old files |
| PDF generation security | **PARTIAL** | See `API-004` |
| HTML-to-PDF injection risks | **PARTIAL** | See `API-004` |
| External image loading in PDFs | **PARTIAL** | `_fetch_logo_bytes()`/`_fetch_bytes()` fetch a stored `logo_url` server-side with no SSRF guard (lower risk than the user-facing `set_image()` path, which does have one — see `API-SECURITY-AUDIT.md`) |
| Cross-tenant file references | **PASS** | No route was found that accepts an arbitrary storage path/URL from one company and serves/attaches it to another; the *access control* gap (`TEN-002`) is separate from a *cross-referencing* bug, and no cross-referencing bug was found |

## Phase 10: Secrets & configuration

No secret values are reproduced below, per audit handling rules.

| Secret type | File location | Exposure risk | Rotation required? |
|---|---|---|---|
| Supabase service-role key | `.env.example` (untracked by git, confirmed via `git ls-files`) | Local-disk plaintext exposure; not in git history | **Yes, immediately, if the value is genuine** — see `APP-001` |
| JWT signing secret | `.env.example` | Same as above, low-entropy example value | Yes — replace with a freshly generated high-entropy secret regardless |
| Supabase service-role key & JWT secret (working values) | `.env.local` (gitignored, confirmed untracked) | Expected/normal location for local secrets; not a finding on its own | No action beyond standard local-secret hygiene |
| Microsoft Graph client ID / tenant ID | Hardcoded as **fallback defaults** in every api/*.py file (`MS_TENANT_ID`, `MS_CLIENT_ID`) | These are non-secret identifiers (an Azure AD app/tenant ID is not confidential by design), not a real exposure | No |
| Microsoft Graph client secret | Environment variable only, no fallback | Not found hardcoded anywhere | Standard rotation cadence |
| Gemini / Brave / PDFShift / CRON API keys | Environment variables only, no fallback found | Not found hardcoded anywhere in `api/*.py` or `index.html` | Standard rotation cadence; project notes independently indicate the Brave key and a Google key were previously pasted in plaintext during chat sessions and are flagged there for rotation — **this audit did not find them hardcoded in the repository itself**, but recommends treating that prior exposure as real and rotating regardless |
| Zoho OAuth credentials (per company) | Database (`company_integrations.credentials`), plaintext JSONB | See `APP-002` — not a file-based exposure, but an at-rest encryption gap | Recommended after encryption-at-rest is implemented |
| Secrets in frontend bundle | `index.html` | **PASS — none found.** Grepped for key/secret/`sb_`/JWT-shaped strings; no match beyond CSS class names and form labels |
| Secrets in logs | `print()`/`traceback.print_exc()` statements | **PARTIAL / NOT FULLY VERIFIED** — exception messages are returned to the client and printed to Vercel logs; none of the reviewed exception paths print a credential value directly, but a future third-party API error message that happens to echo a credential (some APIs do this) would flow straight into both the client response and the logs unfiltered, since no redaction layer exists |
| Secrets in Git history | Not found | `.env.example`/`.env.local` confirmed never tracked (`git ls-files`); a full history scan (`git log -p` / gitleaks against the full history) was **not performed** in this pass — recommended as a one-time check, see `REL-006` |
| Insecure environment defaults | `MS_TENANT_ID`/`MS_CLIENT_ID` fallbacks (non-sensitive, acceptable); no other hardcoded fallback for any genuinely secret value was found in any api/*.py file | Low |

**Client-side environment variable safety:** confirmed clean. `index.html` contains no API keys, tokens, or Supabase credentials of any kind — the frontend calls `API=''` (relative same-origin paths only) and holds only the user's own JWT in localStorage (see `AUTH-002` for the *session-management* risk that creates, which is separate from a *secret-exposure* risk).

## Phase 11: Application security & dependencies — automated checks run

**pip-audit** (against `requirements.txt` pins, run live in this audit session): **19 known vulnerabilities across 5 packages** — flask 3.0.3, flask-cors 4.0.1, pyjwt 2.8.0, python-dotenv 1.0.1, requests 2.32.3. Full detail in `APP-004`. Fixed versions are already published for all five (flask≥3.1.3, flask-cors≥4.0.2, pyjwt≥2.12.0, python-dotenv≥1.2.2, requests≥2.32.4).

**Static analysis / lint / type-check:** No linter (ruff/flake8/pylint) or type-checker (mypy) configuration exists in the repository, and none was run as part of CI. Not run in this audit pass either, since no tool was pre-configured with the project's conventions — recommend adding `ruff` as the fastest path to a baseline (see `REL-006`).

**Secret detection:** No dedicated secret-scanning tool (gitleaks/trufflehog) is configured; this audit's manual grep-based check is a partial substitute, not a replacement.

**Test suite:** `node tests/run-tests.mjs` — not executed in this audit pass (no code changes were made, and running the existing suite was not necessary to validate any finding above; all findings above were verified by direct code reading, which is a stronger form of evidence for the specific claims made than a passing/failing test run would be). Recommend the team run it locally as routine hygiene if it hasn't been run recently.

**Build verification:** Not performed — there is no separate build step for this app (static HTML + serverless Python functions deploy directly).

**Database migration validation:** Not performed — see `REL-001` for why this isn't currently even possible to do reliably (the tracked migrations don't match the live schema).

## Dependency & supply-chain review

| Item | Status |
|---|---|
| Dependency versions | See pip-audit results above; all 7 pinned Python packages are exact-pinned (`==`), which is good practice for reproducibility but means none of them auto-receive security patches without a manual bump |
| Unsupported/EOL packages | **NOT VERIFIED** — none of the 7 pinned packages appear abandoned based on their version numbers, but exact end-of-support status per package was not individually researched in this pass |
| Lockfiles | **NOT APPLICABLE for Python** (no `requirements.lock`/Pipfile.lock — `requirements.txt` pins exact versions, which serves the same purpose informally); **no `package.json`/lockfile exists for the frontend** since it has zero npm dependencies of its own (only the test runner installs `jsdom` ad hoc via `npm install jsdom --no-save`, explicitly not saved to any manifest) |
| Supply-chain risk (unpinned CI actions) | **PARTIAL** — `.github/workflows/tests.yml` uses `actions/checkout@v4` and `actions/setup-node@v4`, both pinned to major version tags (not a SHA), which is a common and generally accepted practice but is technically weaker than SHA-pinning against a compromised tag |
| Unsafe post-install scripts | **NOT VERIFIED** — not independently audited for any of the pinned packages' install hooks |
| Unused dependencies | **NOT VERIFIED** | `python-docx` and `pypdf` are used only inside `extract_attachment_text()` in `api/proposal.py` for reference-document parsing — both appear to be actively used, not dead weight |
| Duplicate libraries | **None found** — no evidence of two libraries serving the same purpose |

## Client-side security headers

**NOT VERIFIED from the repository alone** — response headers (CSP, HSTS, X-Frame-Options/frame-ancestors, X-Content-Type-Options, Referrer-Policy, Permissions-Policy) are typically set either in `vercel.json`'s `headers` array or at the Vercel edge/project-settings level. `vercel.json` in this repo defines `version`, `builds`, `routes`, and `crons` only — **no `headers` array exists**, meaning none of these headers are being set by the application itself. Whether Vercel's platform applies any default security headers on top of this was not verified (would require a live HTTP response inspection against the deployed URL, which this audit did not perform since it was scoped to source-code review). **Recommendation: treat this as FAIL until confirmed otherwise** — add an explicit `headers` block to `vercel.json` covering at minimum `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` (or `frame-ancestors 'none'` via CSP), `Referrer-Policy: strict-origin-when-cross-origin`, and `Strict-Transport-Security`.

**Secure cookies:** Not applicable — no cookies are used for authentication (see `AUTHORIZATION-MATRIX.md`).

**CORS configuration:** `API-001`.
