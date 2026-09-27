-- Bank Reconciliation tracker (Company Profile + Insights)
--
-- The goal: confirm every bank transaction has a corresponding, reconciled
-- entry in Zoho Books, tracked month by month. Unlike Loans, no manual
-- account picker is needed here -- Zoho's own /bankaccounts endpoint already
-- tells us unambiguously which accounts are bank/credit-card accounts
-- (account_type), so every one of them is included automatically.
--
-- Two tables, separating "right now" from "history", same split used for
-- loan_accounts (current_balance) vs its transaction history:
--
-- bank_account_snapshot: one row per bank/credit-card account, refreshed on
-- every sync. balance = Zoho Books' own ledger balance for the account;
-- bank_balance = the live balance per the connected bank feed (only
-- meaningful if the account has bank feeds enabled -- NULL otherwise, never
-- assumed to be 0); uncategorized_transactions = Zoho's own live count of
-- imported bank-feed lines not yet categorized/matched to a Books entry --
-- i.e. exactly "transactions from the bank with no corresponding Zoho
-- transaction yet".
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

-- bank_reconciliation_months: one row per (account, calendar month), derived
-- from Zoho's own /bankaccounts/{id}/reconciliations records (the same
-- reconciliation workflow the user already runs in Zoho each time a bank
-- statement comes in). is_reconciled=true means a completed (non-draft)
-- reconciliation from Zoho covers that month; a month with no row, or
-- is_reconciled=false, means Zoho has no completed reconciliation for it --
-- surfaced as a gap rather than silently omitted.
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
-- Same caveat as every other migration in this app: api/*.py routes use the
-- Supabase service-role key and bypass RLS -- this is defense-in-depth, not
-- a live gate on today's routes.
