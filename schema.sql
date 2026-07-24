-- ═════════════════════════════════════════════════════════════════════════════
-- REL-001: this file, reconciled 2026-07-20 against a live information_schema
-- dump, is the base schema -- but it is NOT the complete schema on its own.
-- The full picture, in order, is:
--   1. This file (schema.sql)                     -- base tables
--   2. migration-project-performance.sql          -- 21 Project Performance tables + RLS
--   3. migration-add-pricing-type.sql, migration-dedupe-products.sql,
--      migration-projects-source.sql, migration-quotes-zoho-project-id.sql,
--      migration-zoho-project-no.sql, migration-po-non-po-cost.sql,
--      migration-po-split-projects.sql, migration-bill-split-projects.sql
--                                                  -- incremental column/constraint additions
--   4. migrate-internal-company.sql                -- companies.is_internal + the internal company row
--   5. migrate-enable-rls-core-tables.sql           -- RLS on every table this file defines
--   6. migrate-add-sessions-table.sql, migrate-add-auth-rate-limiting.sql,
--      migrate-add-platform-audit-log.sql, migrate-add-plan-limits.sql
--                                                  -- newest additions (AUTH-002/003, OBS-001, SUB-001)
-- Run ALL of the above, in this order, against a fresh database to reach the
-- current production schema. Every statement in every one of these files is
-- idempotent (IF NOT EXISTS / safe to re-run).
-- ═════════════════════════════════════════════════════════════════════════════

-- ─── Companies ────────────────────────────────────────────────────────────────
-- REL-001: expanded with company-profile fields (legal/billing identity shown
-- on quote PDFs and the company profile screen) and feature/integration flags
-- that were previously only added via untracked ad-hoc migrations.
create table if not exists companies (
  id uuid default gen_random_uuid() primary key,
  name text not null,
  slug text unique not null,
  plan text default 'free',
  status text default 'active',
  -- Marks the single dedicated, non-customer company row that houses
  -- platform-admin (is_platform_admin=true) users. Since users.company_id is
  -- NOT NULL, platform staff still need *a* company row — this keeps them out
  -- of any real tenant's company_id instead. See migrate-internal-company.sql.
  is_internal boolean default false,
  legal_name text,
  address text,
  trn text,
  phone text,
  website text,
  logo_url text,
  bank_name text,
  bank_account_name text,
  bank_account_no text,
  bank_iban text,
  bank_swift text,
  bank_branch text,
  -- Per-company feature toggles (e.g. which modules are enabled). Embedded
  -- into the JWT at login (see api/auth.py make_token) so route handlers can
  -- gate on it without a DB round-trip per request.
  features jsonb not null default '{"quotes": true}'::jsonb,
  customer_sync_provider text,
  vendor_sync_provider text,
  auto_sync_enabled boolean default false,
  -- SUB-001: plan-limit plumbing (e.g. {"max_quotes_per_month": 50,
  -- "max_users": 10}). Absent/empty means unlimited -- no plan currently sets
  -- this, so it's a no-op for every real company today.
  plan_limits jsonb default '{}'::jsonb,
  created_at timestamptz default now()
);

-- ─── Users ────────────────────────────────────────────────────────────────────
create table if not exists users (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  email text unique not null,
  name text,
  password_hash text not null default '',
  role text default 'user' check (role in ('admin','user')),
  invited_by uuid references users(id),
  is_platform_admin boolean default false,
  -- The Microsoft 365 mailbox this user sends quote/review emails from —
  -- separate from their app login `email` (which may not be a real M365
  -- mailbox, e.g. a Gmail address used just to sign into the app). Falls
  -- back to `email` if unset; the user sets this themselves the first time
  -- they send an email.
  ms365_email text,
  -- Add-on capability layered on top of role (admin/user), not a separate
  -- role: whether this person is eligible to be picked as a quote reviewer.
  can_review boolean default false,
  -- Add-on capability: a plain User only sees quotes they created by
  -- default (see quotes list/get scoping) unless this is set, or they're
  -- admin. Assigned reviewers can still open the specific quote they were
  -- asked to review regardless of this flag.
  can_view_all_quotes boolean default false,
  created_at timestamptz default now()
);

-- ─── Invites ──────────────────────────────────────────────────────────────────
create table if not exists invites (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  email text not null,
  password_hash text not null default '',
  role text default 'user',
  token text unique not null,
  invited_by uuid references users(id),
  accepted boolean default false,
  expires_at timestamptz default now() + interval '7 days',
  created_at timestamptz default now(),
  -- Set at invite time so a new team member can start with reviewer /
  -- view-all-quotes access already granted, instead of an admin having to
  -- remember to flip it on afterward in Team & Settings. Copied onto the
  -- new `users` row when the invite is accepted.
  can_review boolean default false,
  can_view_all_quotes boolean default false
);

-- ─── Quotes ───────────────────────────────────────────────────────────────────
create table if not exists quotes (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  created_by uuid references users(id),
  title text not null,
  customer text,
  status text default 'draft' check (status in ('draft','sent','awarded','lost')),
  currency text default 'AED',
  exchange_rate numeric default 1,
  quote_data jsonb default '[]',
  vendor_data jsonb default '[]',
  terms_data jsonb default '[]',
  total_sell numeric default 0,
  total_gp numeric default 0,
  margin numeric default 0,
  -- Internal review workflow (separate from the customer-facing `status`
  -- lifecycle above): 'none' | 'pending' | 'approved' | 'changes_requested'.
  review_status text default 'none' check (review_status in ('none','pending','approved','changes_requested')),
  current_version int default 0,
  -- Separate from `current_version` above (which tracks internal review
  -- submissions). This counts how many times the quote has actually been
  -- emailed to the customer — first successful send is "V1", shown next to
  -- the sent date in the activity log.
  customer_version int default 0,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- ─── Quote versions (snapshots taken each time a quote is submitted for
-- internal review, so past submissions stay viewable even after the live
-- quote_data keeps changing) ────────────────────────────────────────────────
create table if not exists quote_versions (
  id uuid default gen_random_uuid() primary key,
  quote_id uuid references quotes(id) on delete cascade not null,
  version_number int not null,
  title text,
  customer text,
  quote_data jsonb,
  terms_data jsonb,
  vendor_data jsonb,
  status text default 'pending' check (status in ('pending','approved','changes_requested')),
  submitted_by uuid references users(id),
  submitted_at timestamptz default now(),
  reviewed_by uuid references users(id),
  reviewed_at timestamptz,
  review_comment text,
  -- True when the final decision on this version was made by a Company
  -- Admin overriding/short-circuiting the assigned reviewers (force-approve,
  -- or cancelling the submission), rather than the reviewers themselves.
  admin_override boolean default false
);
create index if not exists idx_quote_versions_quote on quote_versions(quote_id);
create index if not exists idx_quote_versions_status on quote_versions(status);

-- ─── Quote version reviewers (the specific users the quote creator picked to
-- review a given submission — review is assigned, not open to the whole
-- company). quote_versions.status is the aggregate: 'changes_requested' if
-- any assigned reviewer requested changes, 'approved' once all of them have
-- approved, otherwise 'pending' ────────────────────────────────────────────
create table if not exists quote_version_reviewers (
  id uuid default gen_random_uuid() primary key,
  version_id uuid references quote_versions(id) on delete cascade not null,
  user_id uuid references users(id) not null,
  status text default 'pending' check (status in ('pending','approved','changes_requested')),
  comment text,
  decided_at timestamptz,
  created_at timestamptz default now(),
  unique(version_id, user_id)
);
create index if not exists idx_qvr_version on quote_version_reviewers(version_id);
create index if not exists idx_qvr_user on quote_version_reviewers(user_id);

-- ─── Quote emails (one row per customer send — who emailed this quote to
-- whom, when, and a full content snapshot at that moment, mirroring what
-- quote_versions does for review submissions. version_number matches
-- quotes.customer_version at the time of that send, so past sends stay
-- viewable/comparable even after the live quote_data keeps changing) ───────
create table if not exists quote_emails (
  id uuid default gen_random_uuid() primary key,
  quote_id uuid references quotes(id) on delete cascade not null,
  sent_by uuid references users(id),
  sent_to text not null,
  subject text,
  sent_at timestamptz default now(),
  version_number int,
  title text,
  customer text,
  currency text,
  exchange_rate numeric,
  quote_data jsonb,
  terms_data jsonb,
  vendor_data jsonb
);
create index if not exists idx_quote_emails_quote on quote_emails(quote_id);
create index if not exists idx_quote_emails_version on quote_emails(quote_id, version_number);

-- ─── Quote activity log (one row per notable action on a quote — created,
-- status changed, submitted/approved/rejected for review, admin overrides,
-- emailed to customer — shown as a timeline in the quote's Log tab) ────────
create table if not exists quote_activity (
  id uuid default gen_random_uuid() primary key,
  quote_id uuid references quotes(id) on delete cascade not null,
  action text not null,
  detail text,
  actor_id uuid references users(id),
  created_at timestamptz default now()
);
create index if not exists idx_quote_activity_quote on quote_activity(quote_id);

-- ─── Vendors (vendor master — was previously only created via an earlier,
-- untracked migration; reconstructed here so this file stays a complete
-- reference. source/external_contact_id mirror customers, populated by the
-- Zoho/etc. sync in api/integrations.py) ────────────────────────────────────
create table if not exists vendors (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  created_by uuid references users(id),
  name text not null,
  contact_person text,
  email text,
  phone text,
  address text,
  category text,
  notes text,
  source text,
  external_contact_id text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_vendors_company on vendors(company_id);

-- ─── Products (catalog master used for BOM/Auto-BOM search, quote-line
-- autofill, and the Products page — was previously only created via an
-- earlier, untracked migration; reconstructed here with the fuller product
-- master fields added on top. Deliberately no inventory/stock fields — this
-- app is a quoting tool, not a stock system) ────────────────────────────────
create table if not exists products (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  created_by uuid references users(id),
  brand text,
  model text,
  description text,
  default_cost numeric default 0,
  default_margin numeric default 0.2,
  image_url text,
  sku text,
  category text,
  cost_currency text default 'AED',
  -- Set (server-side) whenever default_cost changes, so stale pricing is
  -- visible before it goes into a quote.
  cost_updated_at timestamptz,
  specs text,
  vendor_id uuid references vendors(id) on delete set null,
  vendor_part_number text,
  lead_time text,
  -- Lets an old/discontinued model stop showing up in search without
  -- deleting it outright (deleting would orphan quotes that reference it).
  is_active boolean default true,
  datasheet_url text,
  -- Normalized (lowercased/trimmed) brand+model, used for dedupe matching so
  -- "Infiled" / "infiled " / "INFILED" don't create separate catalog rows.
  -- See normalize_key() in api/products.py.
  model_key text,
  brand_key text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_products_company on products(company_id);
create index if not exists idx_products_vendor on products(vendor_id);
create index if not exists idx_products_category on products(category);
-- One catalog row per brand+model per company. Partial (excludes blank
-- model_key) so legacy rows without a normalized key don't block this.
create unique index if not exists products_company_brandkey_modelkey_uidx
  on products (company_id, brand_key, model_key) where model_key <> '';

-- ─── Customers (customer master — mirrors the existing Vendors master;
-- populated manually or via the Zoho/etc. sync in api/integrations.py) ──────
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

-- ─── Company integrations (per-company third-party sync connections, e.g.
-- Zoho Books/CRM — credentials stored per-provider so a company can connect
-- more than one) ─────────────────────────────────────────────────────────────
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

-- ─── Projects (Project Performance module — a project is created from an
-- awarded quote and tracked against actual cost/revenue over its life;
-- columns below cover both the original manual-tracking fields and the
-- later Zoho-synced Project Performance fields added in
-- migration-project-performance.sql and friends) ────────────────────────────
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

-- ─── Password resets (self-service "Forgot password" flow) ─────────────────
create table if not exists password_resets (
  id uuid default gen_random_uuid() primary key,
  user_id uuid references users(id) on delete cascade not null,
  token text unique not null,
  created_at timestamptz default now(),
  expires_at timestamptz not null,
  used boolean default false
);
create index if not exists idx_password_resets_token on password_resets(token);

-- ─── Indexes ──────────────────────────────────────────────────────────────────
create index if not exists idx_quotes_company on quotes(company_id);
create index if not exists idx_users_company on users(company_id);
create index if not exists idx_users_email on users(email);

-- ─── Auto update updated_at ───────────────────────────────────────────────────
create or replace function update_updated_at()
returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

drop trigger if exists quotes_updated_at on quotes;
create trigger quotes_updated_at
before update on quotes
for each row execute function update_updated_at();
