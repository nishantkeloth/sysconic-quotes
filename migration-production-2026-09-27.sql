-- ============================================================================
-- QTcal production catch-up migration -- 2026-09-27
--
-- Combines every migration that shipped to staging but was never run against
-- production, now that staging is being merged to main. Each statement below
-- is copied verbatim from its own migration-*.sql file in the repo (listed
-- per section) and is idempotent (IF NOT EXISTS / CREATE OR REPLACE-style
-- guards throughout), so it's safe to run even if a piece of it was somehow
-- already applied. Run this once, in full, against the production Supabase
-- database before (or immediately after) deploying the merged `main` branch.
--
-- NOT included: migration-add-bank-statement-import.sql -- that feature
-- (CSV/Excel statement upload) was built, then removed at your request in
-- favor of pulling bank data live from Zoho instead. Nothing in the current
-- code reads those tables, so there is nothing to run for it.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1) migration-add-quote-invoice-matches.sql
--    Quote-to-invoice reconciliation (Business Performance Dashboard)
-- ----------------------------------------------------------------------------
create table if not exists quote_invoice_matches (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  quote_id uuid references quotes(id) on delete cascade not null,
  zoho_invoice_id uuid references zoho_invoices(id) on delete cascade not null,
  match_score numeric,
  match_basis jsonb default '{}'::jsonb,
  status text not null default 'suggested' check (status in ('suggested','confirmed','rejected')),
  created_at timestamptz default now(),
  confirmed_by uuid references users(id),
  confirmed_at timestamptz,
  unique(quote_id, zoho_invoice_id)
);

create index if not exists idx_qim_company_status on quote_invoice_matches(company_id, status);
create index if not exists idx_qim_quote on quote_invoice_matches(quote_id);
create index if not exists idx_qim_invoice on quote_invoice_matches(zoho_invoice_id);

alter table quote_invoice_matches enable row level security;
drop policy if exists tenant_isolation on quote_invoice_matches;
create policy tenant_isolation on quote_invoice_matches
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);


-- ----------------------------------------------------------------------------
-- 2) migration-add-loan-accounts.sql
--    Loans section (Insights dashboard)
-- ----------------------------------------------------------------------------
create table if not exists loan_accounts (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  zoho_account_id text not null,
  account_name text,
  account_type text,
  is_tracked boolean not null default true,
  current_balance numeric default 0,
  total_repaid numeric default 0,
  total_borrowed numeric default 0,
  last_synced_at timestamptz,
  created_at timestamptz default now(),
  unique(company_id, zoho_account_id)
);

create index if not exists idx_loan_accounts_company on loan_accounts(company_id, is_tracked);

alter table loan_accounts enable row level security;
drop policy if exists tenant_isolation on loan_accounts;
create policy tenant_isolation on loan_accounts
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);


-- ----------------------------------------------------------------------------
-- 3) migration-add-bank-reconciliation.sql
--    Bank Reconciliation tracker (Zoho menu -> Reconciliation / Insights)
-- ----------------------------------------------------------------------------
create table if not exists bank_account_snapshot (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  zoho_account_id text not null,
  account_name text,
  account_type text,
  bank_name text,
  balance numeric,
  bank_balance numeric,
  uncategorized_transactions integer default 0,
  last_synced_at timestamptz,
  created_at timestamptz default now(),
  unique(company_id, zoho_account_id)
);
create index if not exists idx_bank_snapshot_company on bank_account_snapshot(company_id);

create table if not exists bank_reconciliation_months (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  zoho_account_id text not null,
  account_name text,
  month date not null, -- always the 1st of the month
  is_reconciled boolean not null default false,
  closing_balance numeric,
  reconciled_at timestamptz,
  last_synced_at timestamptz,
  created_at timestamptz default now(),
  unique(company_id, zoho_account_id, month)
);
create index if not exists idx_bank_recon_months_company on bank_reconciliation_months(company_id, month);

alter table bank_account_snapshot enable row level security;
drop policy if exists tenant_isolation on bank_account_snapshot;
create policy tenant_isolation on bank_account_snapshot
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table bank_reconciliation_months enable row level security;
drop policy if exists tenant_isolation on bank_reconciliation_months;
create policy tenant_isolation on bank_reconciliation_months
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);


-- ----------------------------------------------------------------------------
-- 4) migration-add-insights-share-link.sql
--    Passwordless public Insights share link
-- ----------------------------------------------------------------------------
ALTER TABLE companies ADD COLUMN IF NOT EXISTS insights_share_token_hash TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS insights_share_enabled BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_insights_share_token_hash
  ON companies(insights_share_token_hash)
  WHERE insights_share_token_hash IS NOT NULL;

-- ============================================================================
-- Same caveat as every migration in this app: api/*.py routes use the
-- Supabase service-role key and bypass RLS -- these policies are
-- defense-in-depth, not a live gate on today's routes.
-- ============================================================================
