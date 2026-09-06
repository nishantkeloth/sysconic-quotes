-- ─────────────────────────────────────────────────────────────────────────────
-- Payment Vouchers module (merged in from the standalone sysconic-pv app).
-- Idempotent / safe to re-run, matching this repo's other migration-*.sql
-- files. Generalizes PV's three hardcoded approver columns
-- (rajesh_status/ajith_status/nishant_status) into a voucher_approvals child
-- table instead, so the approval chain isn't frozen to exactly those three
-- people, and links to a real `projects` row instead of a free-text name.
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists vouchers (
  id              uuid default gen_random_uuid() primary key,
  company_id      uuid references companies(id) on delete cascade not null,
  payee           text not null,
  amount          numeric not null,
  currency        text default 'AED',
  payment_method  text default 'Bank Transfer',
  category        text not null,               -- also doubles as the cost-center value (office/hr/it/marketing/finance/operations), matching PV
  project_id      uuid references projects(id) on delete set null,
  project_name_freeform text,                   -- fallback when no matching QtCal project exists yet
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
  token             text unique,                -- signed JWT magic-link token (api/vouchers.py make_action_token)
  responded_at      timestamptz,
  created_at        timestamptz default now(),
  unique(voucher_id, approver_user_id)
);
create index if not exists idx_voucher_approvals_voucher on voucher_approvals(voucher_id);

-- RLS, same shape as migrate-enable-rls-core-tables.sql. Note the same caveat
-- applies: api/vouchers.py talks to Postgres with the service-role key, which
-- bypasses RLS, so this is a defense-in-depth layer, not the live gate --
-- tenant isolation on the real request path is the .eq('company_id', ...)
-- filters already in api/vouchers.py.
alter table vouchers enable row level security;
drop policy if exists tenant_isolation on vouchers;
create policy tenant_isolation on vouchers
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table voucher_approvals enable row level security;
drop policy if exists tenant_isolation on voucher_approvals;
create policy tenant_isolation on voucher_approvals
  using (voucher_id in (select id from vouchers where company_id = (auth.jwt() ->> 'company_id')::uuid));

-- Turn the module on for every real tenant company (this app is effectively
-- single-tenant for Sysconic today, so this just flips it on where it
-- matters; the internal/platform-admin company row is left untouched since
-- is_internal companies don't use feature-gated modules the same way).
update companies set features = features || '{"vouchers": true}'::jsonb where is_internal = false;
