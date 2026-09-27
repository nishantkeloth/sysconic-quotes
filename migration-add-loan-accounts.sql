-- Loans section (Insights dashboard)
--
-- Zoho Books has no dedicated "loan" object -- a loan is just an entry in
-- the Chart of Accounts, usually typed 'other_current_liability' (short-term)
-- or 'long_term_liability', named whatever the bookkeeper called it ("Bank
-- Loan - ADCB", "Car Loan", etc). Other liabilities (VAT payable, accrued
-- expenses, payroll liabilities...) share those same account_types, so
-- there's no reliable way to auto-detect "this account is a loan" from the
-- Zoho API alone -- exactly the same problem migration-add-quote-invoice-
-- matches.sql solves for invoices, solved the same way here: an admin picks
-- which Chart-of-Accounts entries are loans (see /api/integrations/loan-
-- accounts), and only picked accounts ever show up in the dashboard.
--
-- current_balance is read straight from Zoho's ledger (authoritative --
-- Zoho's own running balance for the account, via chartofaccounts?showbalance).
-- total_repaid is the sum of every balance-reducing transaction Zoho has
-- ever recorded against the account (via chartofaccounts/accounttransactions,
-- fully paginated). total_borrowed is DERIVED as current_balance + total_repaid
-- -- exact for a simple single-disbursement term loan, since nothing has
-- reduced the balance except logged repayments. If an account was drawn down
-- more than once (a revolving facility, a second tranche), this understates
-- the true lifetime amount borrowed -- flagged in the UI, not hidden.
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
-- Same caveat as every other migration in this app: api/*.py routes use the
-- Supabase service-role key and bypass RLS -- this is defense-in-depth, not
-- a live gate on today's routes.
