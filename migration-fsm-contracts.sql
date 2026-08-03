-- ─────────────────────────────────────────────────────────────────────────────
-- FSM Module 10 — Contract Management
--
-- A contract belongs to a customer, and optionally scopes down to one site
-- (site_id null = covers every site for that customer). Coverage/exclusions/
-- billing rules/escalation are kept as free text rather than structured
-- sub-tables — AV service contracts vary a lot company to company, and
-- forcing a rigid schema here would fight real-world contract wording more
-- than it'd help. SLA response/resolution hours ARE structured numeric
-- fields since Module 11 (SLA Management) needs to compute against them.
--
-- fsm_sites.contract_id is a soft link (added here, not in the original
-- fsm_sites migration) so a site can show "covered under AMC-2026-004"
-- without having to look up a contract by customer_id + date range every
-- time — set explicitly when a site is assigned to a contract, not
-- auto-resolved.
--
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists fsm_contracts (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  customer_id uuid references customers(id) not null,
  site_id uuid references fsm_sites(id),
  contract_number text,
  contract_type text not null check (contract_type in
    ('warranty','amc','cmc','time_material','labour_only','parts_only','fully_comprehensive')),
  coverage_notes text,
  excluded_items text,
  sla_response_hours numeric,
  sla_resolution_hours numeric,
  billing_rate numeric,
  billing_notes text,
  working_hours text,
  holiday_calendar_notes text,
  escalation_matrix text,
  start_date date,
  end_date date,
  is_active boolean not null default true,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_fsm_contracts_company on fsm_contracts(company_id);
create index if not exists idx_fsm_contracts_customer on fsm_contracts(company_id, customer_id);
create index if not exists idx_fsm_contracts_end_date on fsm_contracts(company_id, is_active, end_date);

alter table fsm_sites add column if not exists contract_id uuid references fsm_contracts(id);

alter table fsm_contracts enable row level security;
drop policy if exists tenant_isolation on fsm_contracts;
create policy tenant_isolation on fsm_contracts
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Verify.
select table_name, column_name, data_type
from information_schema.columns
where table_name in ('fsm_contracts','fsm_sites')
order by table_name, ordinal_position;
