-- Run this once in Supabase → SQL Editor (production, and staging once it has
-- its own database — see the pending "separate staging Supabase project"
-- item). Everything is IF NOT EXISTS / additive — safe to re-run.
--
-- Adds the Project Performance module on top of the "Projects" v0 feature
-- shipped today (commits c4a1326..fbc41e1). Per the design principle this
-- module was scoped around: the `projects` table stays the one project
-- entity (extended here, not duplicated), quotes stays the Commercial
-- Baseline System, Zoho Books stays the Financial Actuals System, and
-- everything below is the layer that combines them.
--
-- NOTE: quotes.project_id and quotes.quote_ref already exist live in the
-- database (used by api/projects.py and api/integrations.py) but were never
-- captured in schema.sql, which has drifted. The ADD COLUMN IF NOT EXISTS
-- lines for them below are here to close that documentation gap, not to
-- introduce anything new.
--
-- After running this, enable the module for a company the same way
-- `projects` was enabled — there is no admin UI for toggling
-- companies.features yet (same gap as the existing `projects` flag), so set
-- it directly:
--   update companies set features = features || '{"project_performance": true}'::jsonb where slug = '...';

-- ─── Trigger helper function (schema.sql assumes this already exists for
-- quotes_updated_at, but it apparently was never actually run against this
-- database — CREATE OR REPLACE is idempotent either way, so this migration
-- no longer depends on schema.sql having been applied first). ─────────────
create or replace function update_updated_at()
returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

-- ─── Extend quotes (formalizing what already exists live + new field) ─────────
alter table quotes add column if not exists project_id uuid references projects(id) on delete set null;
alter table quotes add column if not exists quote_ref text;
alter table quotes add column if not exists won_option_index int;
create index if not exists idx_quotes_project on quotes(project_id);

-- ─── Extend projects (the one project entity — see note above) ────────────────
alter table projects add column if not exists quotation_id uuid references quotes(id) on delete set null;
alter table projects add column if not exists quote_ref text;
alter table projects add column if not exists won_option_index int;
alter table projects add column if not exists project_manager_id uuid references users(id) on delete set null;
alter table projects add column if not exists salesperson_id uuid references users(id) on delete set null;
alter table projects add column if not exists project_type text;
-- Frozen at award time — see project_commercial_baselines below for the full
-- immutable snapshot. These are denormalized copies on the project row
-- purely so the portfolio list/dashboard can read one row per project
-- instead of joining the baseline table on every page load (requirement:
-- dashboard must not do heavy joins/live calls at page load).
alter table projects add column if not exists original_selling_price numeric default 0;
alter table projects add column if not exists original_estimated_cost numeric default 0;
alter table projects add column if not exists original_gp numeric default 0;
alter table projects add column if not exists original_gp_pct numeric default 0;
-- Current calculated position — written by the calculation engine after
-- every sync / manual forecast edit, never computed ad hoc in the frontend.
alter table projects add column if not exists revenue_forecast numeric default 0;
alter table projects add column if not exists actual_cost numeric default 0;
alter table projects add column if not exists committed_cost numeric default 0;
alter table projects add column if not exists forecast_remaining_cost numeric default 0;
alter table projects add column if not exists estimate_at_completion numeric default 0;
alter table projects add column if not exists forecast_gp numeric default 0;
alter table projects add column if not exists forecast_gp_pct numeric default 0;
alter table projects add column if not exists margin_erosion_pct numeric default 0;
alter table projects add column if not exists invoiced_value numeric default 0;
alter table projects add column if not exists collected_value numeric default 0;
alter table projects add column if not exists net_cash_position numeric default 0;
alter table projects add column if not exists completion_pct numeric default 0;
alter table projects add column if not exists health_score numeric;
alter table projects add column if not exists health_status text check (health_status in ('healthy','at_risk','critical'));
alter table projects add column if not exists last_synced_at timestamptz;
create index if not exists idx_projects_zoho_project on projects(zoho_project_id);
create index if not exists idx_projects_quotation on projects(quotation_id);
create index if not exists idx_projects_health_status on projects(health_status);

-- ─── Commercial baseline (frozen the moment a quote is awarded — never
-- edited after creation; historical baseline must not change if product
-- master cost is later updated) ────────────────────────────────────────────
create table if not exists project_commercial_baselines (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade not null,
  quote_id uuid references quotes(id) on delete set null,
  quote_ref text,
  quote_revision int default 0,
  contract_value numeric default 0,
  total_estimated_cost numeric default 0,
  expected_gp numeric default 0,
  expected_gp_pct numeric default 0,
  created_by uuid references users(id),
  created_at timestamptz default now()
);
create index if not exists idx_pcb_project on project_commercial_baselines(project_id);
create index if not exists idx_pcb_company on project_commercial_baselines(company_id);

create table if not exists project_baseline_sections (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  baseline_id uuid references project_commercial_baselines(id) on delete cascade not null,
  section_name text,
  section_revenue numeric default 0,
  section_estimated_cost numeric default 0,
  created_at timestamptz default now()
);
create index if not exists idx_pbs_baseline on project_baseline_sections(baseline_id);

create table if not exists project_baseline_lines (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  baseline_id uuid references project_commercial_baselines(id) on delete cascade not null,
  section_id uuid references project_baseline_sections(id) on delete cascade,
  internal_product_id uuid references products(id) on delete set null,
  brand text,
  model text,
  description text,
  quantity numeric default 1,
  estimated_unit_cost numeric default 0,
  estimated_total_cost numeric default 0,
  estimated_unit_price numeric default 0,
  estimated_total_price numeric default 0,
  created_at timestamptz default now()
);
create index if not exists idx_pbl_baseline on project_baseline_lines(baseline_id);
create index if not exists idx_pbl_section on project_baseline_lines(section_id);
create index if not exists idx_pbl_product on project_baseline_lines(internal_product_id);

-- ─── Cost categories + mapping rules (requirement: configurable, must not
-- depend only on transaction descriptions). Full mapping config screen is a
-- Phase 2 UI item; the tables exist now because zoho_purchase_order_lines /
-- zoho_bill_lines / zoho_expenses reference cost_category_id from Phase 1. ─
create table if not exists project_cost_categories (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  name text not null,
  sort_order int default 0,
  is_active boolean default true,
  created_at timestamptz default now(),
  unique(company_id, name)
);

create table if not exists project_cost_category_mappings (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  cost_category_id uuid references project_cost_categories(id) on delete cascade not null,
  match_type text not null check (match_type in ('zoho_account','zoho_expense_account','zoho_item','zoho_vendor','transaction_type','section_name')),
  match_value text not null,
  created_at timestamptz default now()
);
create index if not exists idx_pccm_company on project_cost_category_mappings(company_id);

-- ─── Zoho Books actuals — staging tables synced by api/pp_sync.py. Never
-- read live from the frontend; idempotent upsert on
-- (company_id, zoho_*_id) so re-syncs never create duplicates. ────────────
create table if not exists zoho_purchase_orders (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade,
  zoho_purchase_order_id text not null,
  zoho_project_id text,
  vendor_name text,
  po_number text,
  po_date date,
  status text,
  total numeric default 0,
  billed_total numeric default 0,
  raw jsonb,
  synced_at timestamptz default now(),
  unique(company_id, zoho_purchase_order_id)
);
create index if not exists idx_zpo_project on zoho_purchase_orders(project_id);
create index if not exists idx_zpo_company on zoho_purchase_orders(company_id);

create table if not exists zoho_purchase_order_lines (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  purchase_order_id uuid references zoho_purchase_orders(id) on delete cascade not null,
  zoho_purchase_order_line_id text,
  zoho_item_id text,
  item_name text,
  quantity numeric default 0,
  rate numeric default 0,
  total numeric default 0,
  quotation_line_id uuid references project_baseline_lines(id) on delete set null,
  cost_category_id uuid references project_cost_categories(id) on delete set null,
  raw jsonb
);
create index if not exists idx_zpol_po on zoho_purchase_order_lines(purchase_order_id);
create index if not exists idx_zpol_item on zoho_purchase_order_lines(zoho_item_id);
create index if not exists idx_zpol_qline on zoho_purchase_order_lines(quotation_line_id);

create table if not exists zoho_bills (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade,
  zoho_bill_id text not null,
  zoho_purchase_order_id text,
  vendor_name text,
  bill_number text,
  bill_date date,
  status text,
  total numeric default 0,
  raw jsonb,
  synced_at timestamptz default now(),
  unique(company_id, zoho_bill_id)
);
create index if not exists idx_zb_project on zoho_bills(project_id);

create table if not exists zoho_bill_lines (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  bill_id uuid references zoho_bills(id) on delete cascade not null,
  zoho_bill_line_id text,
  zoho_item_id text,
  item_name text,
  quantity numeric default 0,
  rate numeric default 0,
  total numeric default 0,
  quotation_line_id uuid references project_baseline_lines(id) on delete set null,
  cost_category_id uuid references project_cost_categories(id) on delete set null,
  raw jsonb
);
create index if not exists idx_zbl_bill on zoho_bill_lines(bill_id);
create index if not exists idx_zbl_qline on zoho_bill_lines(quotation_line_id);

create table if not exists zoho_expenses (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade,
  zoho_expense_id text not null,
  expense_account text,
  vendor_name text,
  expense_date date,
  amount numeric default 0,
  description text,
  cost_category_id uuid references project_cost_categories(id) on delete set null,
  raw jsonb,
  synced_at timestamptz default now(),
  unique(company_id, zoho_expense_id)
);
create index if not exists idx_zex_project on zoho_expenses(project_id);

create table if not exists zoho_invoices (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade,
  zoho_invoice_id text not null,
  invoice_number text,
  invoice_date date,
  due_date date,
  status text,
  total numeric default 0,
  balance numeric default 0,
  raw jsonb,
  synced_at timestamptz default now(),
  unique(company_id, zoho_invoice_id)
);
create index if not exists idx_zinv_project on zoho_invoices(project_id);

create table if not exists zoho_payments (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade,
  zoho_payment_id text not null,
  zoho_invoice_id text,
  payment_date date,
  amount numeric default 0,
  raw jsonb,
  synced_at timestamptz default now(),
  unique(company_id, zoho_payment_id)
);
create index if not exists idx_zpay_project on zoho_payments(project_id);
create index if not exists idx_zpay_invoice on zoho_payments(zoho_invoice_id);

-- ─── Forecast remaining cost (user-maintained, versioned — never overwritten
-- without history) ──────────────────────────────────────────────────────────
create table if not exists project_forecasts (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade not null,
  cost_category_id uuid references project_cost_categories(id) on delete set null,
  amount numeric not null default 0,
  expected_date date,
  description text,
  status text default 'active' check (status in ('active','revised','removed')),
  removal_reason text,
  created_by uuid references users(id),
  created_at timestamptz default now()
);
create index if not exists idx_pforecast_project on project_forecasts(project_id, status);

-- ─── Completion percentage history ─────────────────────────────────────────
create table if not exists project_progress_history (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade not null,
  percentage numeric not null,
  updated_by uuid references users(id),
  comment text,
  created_at timestamptz default now()
);
create index if not exists idx_pprog_project on project_progress_history(project_id, created_at desc);

-- ─── Approved budget revisions (original budget stays untouched — see
-- project_commercial_baselines, which is never edited) ─────────────────────
create table if not exists project_budget_revisions (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade not null,
  revision_number int not null,
  revision_date date default current_date,
  reason text,
  previous_budget numeric,
  revised_budget numeric,
  created_by uuid references users(id),
  approved_by uuid references users(id),
  created_at timestamptz default now()
);
create index if not exists idx_pbr_project on project_budget_revisions(project_id);

-- ─── Health score detail — stored so a user can see why a project scored
-- what it scored, not just the number ───────────────────────────────────────
create table if not exists project_health_scores (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade not null,
  margin_health numeric,
  cost_control numeric,
  billing_health numeric,
  collection_health numeric,
  cash_health numeric,
  commitment_exposure numeric,
  weights jsonb,
  overall_score numeric,
  status text check (status in ('healthy','at_risk','critical')),
  calculated_at timestamptz default now()
);
create index if not exists idx_phs_project on project_health_scores(project_id, calculated_at desc);

-- ─── Per-company configurable thresholds — margin erosion bands, health
-- score weights/bands, billing-gap alert threshold. Requirement is explicit
-- that these must be configurable and never hardcoded in the frontend. ─────
create table if not exists project_performance_settings (
  company_id uuid references companies(id) on delete cascade primary key,
  margin_erosion_healthy_max numeric default 2,
  margin_erosion_at_risk_max numeric default 5,
  health_score_healthy_min numeric default 80,
  health_score_at_risk_min numeric default 60,
  health_score_weights jsonb default '{"margin":30,"cost_control":20,"billing":15,"collection":15,"cash":10,"commitment":10}',
  billing_gap_alert_threshold numeric default 15,
  updated_at timestamptz default now()
);

-- ─── Margin-trend / KPI history — one row per project per sync-day, powers
-- trend charts (e.g. "22.5% → 21.8% → 19.2% → 15.8%") ──────────────────────
create table if not exists project_performance_snapshots (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade not null,
  snapshot_date date not null default current_date,
  project_value numeric,
  original_gp_pct numeric,
  forecast_gp_pct numeric,
  margin_erosion_pct numeric,
  actual_cost numeric,
  committed_cost numeric,
  forecast_remaining_cost numeric,
  estimate_at_completion numeric,
  invoiced_value numeric,
  collected_value numeric,
  net_cash_position numeric,
  health_score numeric,
  created_at timestamptz default now(),
  unique(project_id, snapshot_date)
);
create index if not exists idx_pps_project on project_performance_snapshots(project_id, snapshot_date);

-- ─── Central alert log ──────────────────────────────────────────────────────
create table if not exists project_alerts (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade not null,
  alert_type text not null,
  severity text default 'medium' check (severity in ('low','medium','high','critical')),
  explanation text,
  financial_impact numeric,
  status text default 'open' check (status in ('open','acknowledged','under_review','resolved','ignored')),
  assigned_user_id uuid references users(id) on delete set null,
  resolution_comment text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_palert_project on project_alerts(project_id);
create index if not exists idx_palert_company on project_alerts(company_id);
create index if not exists idx_palert_status on project_alerts(status);

drop trigger if exists project_alerts_updated_at on project_alerts;
create trigger project_alerts_updated_at
before update on project_alerts
for each row execute function update_updated_at();

-- ─── Sync run history ───────────────────────────────────────────────────────
create table if not exists pp_sync_logs (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete set null,
  resource text not null,
  status text not null check (status in ('success','error')),
  records_synced int default 0,
  error_detail text,
  started_at timestamptz default now(),
  finished_at timestamptz
);
create index if not exists idx_ppsync_company on pp_sync_logs(company_id, started_at desc);

-- ─── Mapping exceptions — transactions that couldn't be confidently mapped;
-- never silently excluded from profitability ───────────────────────────────
create table if not exists pp_mapping_exceptions (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade,
  source_table text not null,
  source_id uuid not null,
  reason text,
  amount numeric,
  status text default 'open' check (status in ('open','resolved','ignored')),
  resolution_note text,
  created_at timestamptz default now()
);
create index if not exists idx_ppme_company on pp_mapping_exceptions(company_id, status);

-- ─── AI analysis output + chat log ──────────────────────────────────────────
create table if not exists project_ai_analyses (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  project_id uuid references projects(id) on delete cascade not null,
  kind text not null, -- 'analysis' | 'chat'
  question text,
  content jsonb not null,
  created_by uuid references users(id),
  created_at timestamptz default now()
);
create index if not exists idx_paia_project on project_ai_analyses(project_id, created_at desc);

-- ─── New user capability flags (mirrors the existing can_review /
-- can_view_all_quotes pattern — see users table in schema.sql) ─────────────
alter table users add column if not exists can_manage_project_performance boolean default false;
alter table users add column if not exists is_finance_user boolean default false;

-- ─── Row-Level Security — scoped to the new tables introduced here only.
-- The rest of the app has none yet (separate, larger initiative — see
-- sysconic-code-and-architecture-audit.md); every backend route still also
-- filters by company_id explicitly, so this is defense-in-depth, not the
-- only gate. Service-role key (used by every api/*.py file) bypasses RLS by
-- design, so normal app operation is unaffected. ───────────────────────────
do $$
declare
  t text;
begin
  foreach t in array array[
    'project_commercial_baselines','project_baseline_sections','project_baseline_lines',
    'project_cost_categories','project_cost_category_mappings',
    'zoho_purchase_orders','zoho_purchase_order_lines','zoho_bills','zoho_bill_lines',
    'zoho_expenses','zoho_invoices','zoho_payments',
    'project_forecasts','project_progress_history','project_budget_revisions',
    'project_health_scores','project_performance_settings','project_performance_snapshots',
    'project_alerts','pp_sync_logs','pp_mapping_exceptions','project_ai_analyses'
  ]
  loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists tenant_isolation on %I', t);
    execute format(
      'create policy tenant_isolation on %I using (company_id = (auth.jwt() ->> ''company_id'')::uuid)',
      t
    );
  end loop;
end $$;
-- Note: the app authenticates with its own JWT (api/auth.py), not Supabase
-- Auth, so auth.jwt() will be empty for the service-role connections every
-- api/*.py file uses — those bypass RLS anyway via the service role key.
-- This policy is the safety net for the day a query is ever made with a
-- lower-privilege key; it deliberately does not block the app's normal path.
