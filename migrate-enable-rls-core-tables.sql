-- ─────────────────────────────────────────────────────────────────────────────
-- TEN-001 remediation: Row-Level Security on every core tenant table, as a
-- defense-in-depth safety net.
--
-- IMPORTANT CONTEXT: this app authenticates with a custom JWT (not Supabase
-- Auth), and every api/*.py route talks to Postgres using the Supabase
-- SERVICE-ROLE key, which bypasses RLS unconditionally. That means these
-- policies are NOT a live gate on the app's normal request path today --
-- tenant isolation on that path is still enforced entirely in application
-- code (every query is already scoped `.eq('company_id', claims['company_id'])`).
-- What RLS buys here is a second, independent layer that would still hold
-- the line if: (a) a future code path is ever added that queries with a
-- lower-privilege/anon key, (b) a bug omits a company_id filter in app code,
-- or (c) direct DB access is ever granted to a non-service-role credential.
--
-- Policies read auth.jwt() ->> 'company_id', matching the pattern already
-- established in migration-project-performance.sql and
-- migrate-add-sessions-table.sql.
--
-- Already run and verified on both staging and production (confirmed via
-- `select relname, relrowsecurity from pg_class ...` showing rls_enabled=true
-- on all 13 tables below). This file is kept for documentation / so a fresh
-- database can reach the same state. Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

-- companies: scoped by its own id, not a company_id column.
alter table companies enable row level security;
drop policy if exists tenant_isolation on companies;
create policy tenant_isolation on companies
  using (id = (auth.jwt() ->> 'company_id')::uuid);

-- Tables with a direct company_id column.
alter table users enable row level security;
drop policy if exists tenant_isolation on users;
create policy tenant_isolation on users
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table invites enable row level security;
drop policy if exists tenant_isolation on invites;
create policy tenant_isolation on invites
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table quotes enable row level security;
drop policy if exists tenant_isolation on quotes;
create policy tenant_isolation on quotes
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table vendors enable row level security;
drop policy if exists tenant_isolation on vendors;
create policy tenant_isolation on vendors
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table products enable row level security;
drop policy if exists tenant_isolation on products;
create policy tenant_isolation on products
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table customers enable row level security;
drop policy if exists tenant_isolation on customers;
create policy tenant_isolation on customers
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table company_integrations enable row level security;
drop policy if exists tenant_isolation on company_integrations;
create policy tenant_isolation on company_integrations
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table projects enable row level security;
drop policy if exists tenant_isolation on projects;
create policy tenant_isolation on projects
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Tables that hang off quotes rather than carrying company_id directly --
-- scoped by joining through quotes.
alter table quote_versions enable row level security;
drop policy if exists tenant_isolation on quote_versions;
create policy tenant_isolation on quote_versions
  using (quote_id in (select id from quotes where company_id = (auth.jwt() ->> 'company_id')::uuid));

alter table quote_version_reviewers enable row level security;
drop policy if exists tenant_isolation on quote_version_reviewers;
create policy tenant_isolation on quote_version_reviewers
  using (version_id in (
    select qv.id from quote_versions qv
    join quotes q on q.id = qv.quote_id
    where q.company_id = (auth.jwt() ->> 'company_id')::uuid
  ));

alter table quote_emails enable row level security;
drop policy if exists tenant_isolation on quote_emails;
create policy tenant_isolation on quote_emails
  using (quote_id in (select id from quotes where company_id = (auth.jwt() ->> 'company_id')::uuid));

alter table quote_activity enable row level security;
drop policy if exists tenant_isolation on quote_activity;
create policy tenant_isolation on quote_activity
  using (quote_id in (select id from quotes where company_id = (auth.jwt() ->> 'company_id')::uuid));

-- Verify.
select relname as table_name, relrowsecurity as rls_enabled
from pg_class
where relname in (
  'companies','users','invites','quotes','quote_versions',
  'quote_version_reviewers','quote_emails','quote_activity',
  'vendors','products','customers','company_integrations','projects'
)
order by relname;
