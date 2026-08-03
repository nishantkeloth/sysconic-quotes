-- ─────────────────────────────────────────────────────────────────────────────
-- FSM Module 5 — Engineer Management
--
-- Field service engineers who tickets/work orders get assigned to. Optionally
-- linked to a `users` row (if the engineer also logs into the app) via
-- user_id, but not required — some companies track subcontractor engineers
-- who never get an app login.
--
-- Skills/certifications are free-form text[] rather than a fixed enum, since
-- AV/ELV certifications vary widely by manufacturer and change over time
-- (CTS, CTS-I, CTS-D, Crestron, Extron, QSC Q-SYS, Biamp, Dante Level 1-3,
-- AVIXA, etc.) — the frontend offers common suggestions but doesn't restrict
-- input, matching how `category`/`tags` free-text fields work elsewhere in
-- the app (e.g. products.category).
--
-- Phase 1 scope: profile, skills/certs, territory, availability status.
-- Deferred to a later phase: GPS location tracking, leave calendar,
-- performance metrics (first-time-fix rate etc. — these get computed from
-- fsm_tickets/fsm_work_orders once there's enough data, not stored here).
--
-- RLS follows the same pattern as migration-crm-deals.sql and the other FSM
-- Phase 1 tables — defense-in-depth only, real tenant isolation is enforced
-- in application code via claims['company_id'].
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists fsm_engineers (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  user_id uuid references users(id),
  name text not null,
  email text,
  phone text,
  skills text[] default '{}',
  certifications text[] default '{}',
  territory text,
  availability text not null default 'available'
    check (availability in ('available','busy','on_leave','off_duty')),
  is_active boolean not null default true,
  notes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_fsm_engineers_company on fsm_engineers(company_id);
create index if not exists idx_fsm_engineers_active on fsm_engineers(company_id, is_active);

alter table fsm_engineers enable row level security;
drop policy if exists tenant_isolation on fsm_engineers;
create policy tenant_isolation on fsm_engineers
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Verify.
select table_name, column_name, data_type
from information_schema.columns
where table_name = 'fsm_engineers'
order by ordinal_position;
