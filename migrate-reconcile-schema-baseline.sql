-- ─────────────────────────────────────────────────────────────────────────────
-- REL-001 remediation: reconciles a live database (which accumulated several
-- untracked/ad-hoc changes over time) up to the documented baseline in
-- schema.sql. schema.sql itself uses `create table if not exists`, which is a
-- no-op against tables that already exist -- so the column-level additions
-- below are needed as explicit ALTER TABLEs to actually reach that baseline
-- on an existing database. New tables schema.sql defines that don't exist
-- yet are created here too.
--
-- Every statement is idempotent (IF NOT EXISTS / safe to re-run). Run once
-- per environment (staging, production).
-- ─────────────────────────────────────────────────────────────────────────────

-- ── companies: company-profile + feature/integration columns ───────────────
alter table companies add column if not exists legal_name text;
alter table companies add column if not exists address text;
alter table companies add column if not exists trn text;
alter table companies add column if not exists phone text;
alter table companies add column if not exists website text;
alter table companies add column if not exists logo_url text;
alter table companies add column if not exists bank_name text;
alter table companies add column if not exists bank_account_name text;
alter table companies add column if not exists bank_account_no text;
alter table companies add column if not exists bank_iban text;
alter table companies add column if not exists bank_swift text;
alter table companies add column if not exists bank_branch text;
alter table companies add column if not exists features jsonb not null default '{"quotes": true}'::jsonb;
alter table companies add column if not exists customer_sync_provider text;
alter table companies add column if not exists vendor_sync_provider text;
alter table companies add column if not exists auto_sync_enabled boolean default false;

-- ── vendors: updated_at (for parity with products/customers) ───────────────
alter table vendors add column if not exists updated_at timestamptz default now();

-- ── products: normalized dedupe keys + the unique index that depends on them.
-- NOTE: this unique index previously failed on staging/production due to a
-- pre-existing duplicate row (same company_id/brand_key/model_key, created
-- ~0.3s apart by a race condition in index.html's captureProduct() blur
-- handler). That duplicate has since been manually resolved (the newer,
-- redundant row was deleted), so this can now run cleanly.
alter table products add column if not exists model_key text;
alter table products add column if not exists brand_key text;
create unique index if not exists products_company_brandkey_modelkey_uidx
  on products (company_id, brand_key, model_key) where model_key <> '';

-- ── customers (new table if not already present) ───────────────────────────
create table if not exists customers (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  name text not null,
  company_name text,
  email text,
  phone text,
  address text,
  trn text,
  notes text,
  source text default 'manual',
  external_contact_id text,
  created_by uuid references users(id),
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_customers_company on customers(company_id);

-- ── company_integrations (new table if not already present) ────────────────
create table if not exists company_integrations (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  provider text not null,
  credentials jsonb not null default '{}',
  status text default 'disconnected',
  last_synced_at timestamptz,
  created_by uuid references users(id),
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique(company_id, provider)
);

-- ── projects (new table if not already present -- on databases where the
-- Project Performance migration already created this table, all these
-- statements are no-ops) ────────────────────────────────────────────────────
create table if not exists projects (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  name text not null,
  customer text,
  site_location text,
  status text default 'active',
  po_number text,
  start_date date,
  end_date date,
  notes text,
  created_by uuid references users(id),
  created_at timestamptz default now(),
  zoho_project_id text,
  zoho_project_no text,
  quotation_id uuid references quotes(id) on delete set null,
  quote_ref text,
  won_option_index int,
  project_manager_id uuid references users(id),
  salesperson_id uuid references users(id),
  project_type text,
  original_selling_price numeric default 0,
  original_estimated_cost numeric default 0,
  original_gp numeric default 0,
  original_gp_pct numeric default 0,
  revenue_forecast numeric default 0,
  actual_cost numeric default 0,
  committed_cost numeric default 0,
  forecast_remaining_cost numeric default 0,
  estimate_at_completion numeric default 0,
  forecast_gp numeric default 0,
  forecast_gp_pct numeric default 0,
  margin_erosion_pct numeric default 0,
  invoiced_value numeric default 0,
  collected_value numeric default 0,
  net_cash_position numeric default 0,
  completion_pct numeric default 0,
  health_score numeric,
  health_status text check (health_status in ('on_track','at_risk','critical')),
  last_synced_at timestamptz,
  source text default 'quote',
  po_based_actual_cost numeric default 0,
  non_po_based_actual_cost numeric default 0
);
create index if not exists idx_projects_company on projects(company_id);

-- ── password_resets (new table if not already present) ─────────────────────
create table if not exists password_resets (
  id uuid default gen_random_uuid() primary key,
  user_id uuid references users(id) on delete cascade not null,
  token text unique not null,
  created_at timestamptz default now(),
  expires_at timestamptz not null,
  used boolean default false
);
create index if not exists idx_password_resets_token on password_resets(token);

-- Verify.
select table_name, column_name, data_type
from information_schema.columns
where table_name in ('companies','vendors','products','customers','company_integrations','projects','password_resets')
order by table_name, ordinal_position;
