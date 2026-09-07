# Payment Vouchers → QtCal: Merge & Data Migration Plan

Written 2026-09-05 after reviewing both live codebases directly (not just the public app URLs). This replaces the earlier generic options with a plan grounded in what's actually in the code.

## What I found

**Source — `sysconic-pv`** (github.com/nishantkeloth/sysconic-pv, live at sysconic-pv.vercel.app): a single Python/Flask app on Vercel (`app.py`, `@vercel/python`), talking to a Postgres database over a plain `DATABASE_URL` via `psycopg2` — this is the "Supabase" you mean, though the connection string itself isn't committed to the repo, so I can't see which Supabase project it points to. One real table, `vouchers` (payee, amount, currency, payment_method, category — category doubles as the cost-center field, project as free text, invoice_no, due_date as *text* not a date, remarks, submitted_by as a free-text name, status, plus three fixed columns `rajesh_status` / `ajith_status` / `nishant_status`). Approval happens over emailed/WhatsApp (Twilio) magic links using `itsdangerous` tokens hitting `/action/<token>/<decision>`, with a separate 4-digit PIN for an admin-override path. There's also a `quotes` table and `/api/quotes` routes already sitting in this same app — dead code, nothing in the frontend calls them, so I've ignored it.

**Target — QtCal** is `sysconic-quotes-saas`, at `github.com/nishantkeloth/sysconic-quotes`, checked out locally under `sysconic-saas-v3/`. This is not the small SharePoint-backed prototype (`sysconic-quotes-v2`) I looked at earlier — that one has no `.git` and saves everything as a JSON blob into a SharePoint list, so it's a dead end for this plan. The real QtCal is a proper multi-tenant SaaS: Flask backend, one Vercel serverless function per feature (`api/auth.py`, `api/quotes.py`, `api/products.py`, `api/customers.py`, `api/vendors.py`, `api/projects.py`, plus a large FSM suite, delivery challans, AV room designer, CRM deals, and more), **Supabase Postgres** via the `supabase` Python client, JWT auth with bcrypt passwords, and a real `company_id`-scoped data model (`companies`, `users`, `quotes`, `projects`, `company_integrations`, etc.) with RLS enabled on every core table. It's actively developed — last commit Sept 2, 2026. There is currently nothing voucher-related in it at all.

Two details make the merge cleaner than it could have been: QtCal's `projects` table already carries `zoho_project_id` / `zoho_project_no`, so vouchers can link to real project rows instead of PV's free-text project name; and `company_integrations` already stores per-company provider credentials as JSON, which is a natural home for the Twilio/WhatsApp approver config that's currently hardcoded as environment variables in PV.

## Recommended data model in QtCal's Supabase

Rather than copying PV's three hardcoded approver columns as-is, generalize approvals into a child table — it costs almost nothing extra and means the approval chain isn't frozen to exactly Rajesh/Ajith/you:

```sql
-- migration-add-vouchers.sql (draft — matches this repo's existing migration style)

create table if not exists vouchers (
  id              uuid default gen_random_uuid() primary key,
  company_id      uuid references companies(id) on delete cascade not null,
  payee           text not null,
  amount          numeric not null,
  currency        text default 'AED',
  payment_method  text default 'Bank Transfer',
  category        text not null,               -- also the cost-center value (office/hr/it/marketing/finance/operations)
  project_id      uuid references projects(id) on delete set null,
  project_name_freeform text,                   -- fallback when no matching QtCal project exists
  invoice_no      text,
  due_date        date,
  remarks         text,
  submitted_by    uuid references users(id),
  submitted_at    timestamptz default now(),
  status          text default 'pending' check (status in ('pending','approved','rejected')),
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);
create index if not exists idx_vouchers_company on vouchers(company_id);
create index if not exists idx_vouchers_project on vouchers(project_id);

create table if not exists voucher_approvals (
  id                uuid default gen_random_uuid() primary key,
  voucher_id        uuid references vouchers(id) on delete cascade not null,
  approver_user_id  uuid references users(id) not null,
  status            text default 'pending' check (status in ('pending','approved','rejected')),
  token             text unique,                -- same magic-link pattern PV already uses
  responded_at      timestamptz,
  created_at        timestamptz default now(),
  unique(voucher_id, approver_user_id)
);
create index if not exists idx_voucher_approvals_voucher on voucher_approvals(voucher_id);

alter table vouchers enable row level security;
alter table voucher_approvals enable row level security;
-- policies to follow the same shape as migrate-enable-rls-core-tables.sql
```

The PIN-based admin override in PV should retire in favor of QtCal's existing `role = 'admin'` check (and, if you want finer control later, the `roles`/`role_permissions` system already scaffolded in `api/auth.py` — the same mechanism `can_review` uses for quotes could gate voucher approval).

## Data migration (existing PV rows → QtCal tables)

1. Confirm Rajesh, Ajith, and you already exist as rows in QtCal's `users` table (under the right `company_id`) — the old app only ever stored their names as text, so the new schema needs real `user_id`s to reference.
2. For each PV voucher: resolve `project` (free text) against QtCal's `projects.name`; where there's no match, keep the name in `project_name_freeform` rather than forcing a bad link.
3. Insert one `vouchers` row per PV voucher, then explode `rajesh_status` / `ajith_status` / `nishant_status` into up to three `voucher_approvals` rows pointing at the resolved user IDs.
4. Run it as a one-off Python script (`psycopg2` against the PV `DATABASE_URL` for reads, the `supabase` client or a direct Postgres connection against QtCal for writes) rather than a manual export/import — the project/user resolution step needs real logic, not just a column copy. I haven't written this script yet since it needs your live `DATABASE_URL` / `SUPABASE_SERVICE_KEY` values, which I don't have and shouldn't ask you to paste into chat — better to run it from your machine where those secrets already live in each project's `.env`.
5. Take a Supabase backup/point-in-time snapshot of both projects before running it, and dry-run against a handful of rows first (print what would be inserted, don't commit) before doing the full set.

## Wiring it into the app

Follow the existing per-feature pattern exactly: a new `api/vouchers.py` (standalone Flask app, decodes the JWT itself like every other module, uses `create_client(SUPABASE_URL, SUPABASE_KEY)`), a new block in `vercel.json` routing `/api/vouchers/(.*)` to it, and a new section added to the single `index.html` alongside Quotes/Customers/Vendors in the nav — reusing QtCal's existing login/JWT instead of the separate PIN. The emailed/WhatsApp magic-link approval mechanism can carry over almost unchanged (same `itsdangerous`-style token, same Twilio call), just reading approver contact info from `company_integrations` (provider `'twilio'`) instead of hardcoded env vars.

## Suggested sequence

1. Review/adjust the schema above, then apply the migration to QtCal's Supabase.
2. Write and dry-run the data migration script; execute it once verified.
3. Build `api/vouchers.py` + the `vouchers` UI section in QtCal.
4. Point the approval magic-links at the new QtCal routes; verify email/WhatsApp still fire correctly.
5. Run both apps in parallel briefly, then retire `sysconic-pv.vercel.app` (or redirect it).

## Status: implemented, not yet deployed (2026-09-05)

Everything below is written to disk in this repo but NOT committed, pushed, or
deployed -- `git status` also shows several unrelated in-progress changes of
yours (delivery challans, FSM engineers, the AV room 3D viewer) sitting
uncommitted, so I did not touch git at all. Review with `git diff` and commit
only what you want, whenever you want.

What's done:
- `migration-add-vouchers.sql` -- the `vouchers` + `voucher_approvals` tables,
  RLS policies, and a `features->>'vouchers'=true` flip for every non-internal
  company. Not yet run against Supabase -- run it in the SQL Editor when ready.
- `api/vouchers.py` -- list/create/delete, the emailed magic-link
  approve/reject route (`/api/vouchers/action/<token>/<decision>`, no login
  needed, same UX as PV), an in-app respond route for approvers who are
  already logged in, and an admin force-override replacing PV's PIN.
- `vercel.json` -- routes `/api/vouchers*` to the new file (3-line diff).
- `index.html` -- a new "Finance \u2192 Payment Vouchers" sidebar entry, page
  wiring, and a working page: list with status filter, a new-voucher modal,
  inline approve/reject for whoever's turn it is, and admin force/delete
  (112-line diff, isolated to new functions plus small additions to five
  existing dispatch tables -- verified the whole inline `<script>` still
  parses with `node --check` after editing).
- `scripts/migrate_pv_vouchers.py` -- the one-off data migration, dry-run by
  default (`--apply` to write). I could not run this myself: PV's
  `DATABASE_URL` only exists as a Vercel dashboard env var on the sysconic-pv
  project, not in any file I can reach. The script's docstring covers pulling
  it via `vercel env pull`.
- Confirmed 2026-09-05: all three approvers (Rajesh, Ajith, Nishant) already
  have `users` rows in QtCal, and the migration script resolves them by email
  via `--approver-emails` rather than needing new accounts.

What I could not verify: which `companies` row is Sysconic's real tenant
row (versus the internal/platform-admin one) -- I tried querying Supabase's
REST API directly using the service key already sitting in `.env.local`, from
both the device shell and this cloud sandbox, and neither has outbound network
access to reach `supabase.co`. The migration script works around this by
auto-detecting the single non-internal company at run time (and refusing to
guess if there's more than one) -- so this resolves itself the moment you run
it, no separate lookup needed.

## Remaining steps (in order)

1. `git diff` to review the four changed/new files above.
2. Run `migration-add-vouchers.sql` in the Supabase SQL Editor for this project.
3. Set a `VOUCHER_APPROVER_EMAILS` env var in Vercel for this project (comma-separated, e.g. Rajesh's, Ajith's, and your email) -- `api/vouchers.py` reads it to know who to notify on a new submission.
4. Commit and deploy (`vercel --prod` or push + your usual flow) once you're happy with the diff.
5. Pull PV's `DATABASE_URL` (`vercel env pull` in the sysconic-pv repo) and run `scripts/migrate_pv_vouchers.py` without `--apply` first -- check the printed project/approver matches look right -- then with `--apply`.
6. Spot-check a voucher end to end (submit one, approve via the emailed link, approve via the in-app button) before retiring `sysconic-pv.vercel.app`.

## Known gaps / fast-follows (deliberately out of scope for this first pass)

- No WhatsApp notifications -- QtCal has no Twilio integration anywhere yet (only a TODO comment in `api/fsm_tickets.py`), so voucher approval requests are email-only for now, same as everything else in this app.
- Approver list is one env var (`VOUCHER_APPROVER_EMAILS`), not a Settings-page UI. Moving it into `company_integrations` (provider `voucher_approvers`) so it's editable per-company without a redeploy is a natural next step.
- PV's dashboard analytics (monthly trend chart, top-projects-by-spend, Excel export) weren't ported -- the new page covers submit/list/approve, not reporting. Worth a follow-up pass if you use those regularly.
- `submitted_by` on migrated rows will be blank -- PV only ever stored the submitter as a free-text name, which doesn't reliably match a `users` row, so the migration script leaves it null rather than guessing.
