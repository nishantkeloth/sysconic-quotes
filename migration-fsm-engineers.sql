-- ─────────────────────────────────────────────────────────────────────────────
-- FSM Module 5 — Engineer Management
--
-- fsm_engineers already existed from the original Phase 1 session (created
-- alongside fsm_sites/fsm_assets/fsm_tickets/etc.) with only a minimal
-- column set (id, company_id, user_id, name, phone, email, territory,
-- is_active, created_at, updated_at) — Engineers was never fully built out.
-- This migration adds the columns Module 5 needs on top of that, using
-- `add column if not exists` so it's safe to re-run and won't touch the
-- columns that already exist.
--
-- Skills/certifications are free-form text[] rather than a fixed enum, since
-- AV/ELV certifications vary widely by manufacturer and change over time
-- (CTS, CTS-I, CTS-D, Crestron, Extron, QSC Q-SYS, Biamp, Dante Level 1-3,
-- AVIXA, etc.) — the frontend offers common suggestions but doesn't restrict
-- input.
--
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists fsm_engineers (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  user_id uuid references users(id),
  name text not null,
  email text,
  phone text,
  territory text,
  is_active boolean not null default true,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

alter table fsm_engineers add column if not exists skills text[] default '{}';
alter table fsm_engineers add column if not exists certifications text[] default '{}';
alter table fsm_engineers add column if not exists notes text;

alter table fsm_engineers add column if not exists availability text;
update fsm_engineers set availability = 'available' where availability is null;
alter table fsm_engineers alter column availability set default 'available';
alter table fsm_engineers alter column availability set not null;
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'fsm_engineers_availability_check'
  ) then
    alter table fsm_engineers add constraint fsm_engineers_availability_check
      check (availability in ('available','busy','on_leave','off_duty'));
  end if;
end $$;

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
