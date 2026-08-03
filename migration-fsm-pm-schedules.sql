-- ─────────────────────────────────────────────────────────────────────────────
-- FSM Module 8 — Preventive Maintenance
--
-- A PM schedule targets a site (required) and optionally a specific asset
-- (e.g. "quarterly LED wall calibration" on one asset, vs. "half-yearly rack
-- inspection" for a whole site). When due, the generator (run manually from
-- the UI or via the /api/fsm_pm/run-auto-generate cron, same pattern as
-- api/integrations.py's run-auto-sync) creates a real fsm_tickets row +
-- fsm_work_orders row carrying the schedule's checklist, then advances
-- next_due_date by the schedule's frequency.
--
-- RLS follows the same pattern as the other FSM Phase 1 tables.
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists fsm_pm_schedules (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  site_id uuid references fsm_sites(id) on delete cascade not null,
  asset_id uuid references fsm_assets(id),
  name text not null,
  frequency text not null check (frequency in ('monthly','quarterly','half_yearly','yearly')),
  checklist jsonb default '[]',
  assigned_engineer_id uuid references fsm_engineers(id),
  next_due_date date not null,
  last_generated_at timestamptz,
  is_active boolean not null default true,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_fsm_pm_schedules_company on fsm_pm_schedules(company_id);
create index if not exists idx_fsm_pm_schedules_due on fsm_pm_schedules(company_id, is_active, next_due_date);

alter table fsm_pm_schedules enable row level security;
drop policy if exists tenant_isolation on fsm_pm_schedules;
create policy tenant_isolation on fsm_pm_schedules
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Verify.
select table_name, column_name, data_type
from information_schema.columns
where table_name = 'fsm_pm_schedules'
order by ordinal_position;
