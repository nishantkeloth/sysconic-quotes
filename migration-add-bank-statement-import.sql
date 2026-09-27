-- Bank statement upload + column mapping (Zoho → Report, Phase 2)
--
-- This is the independent "bank side" data source agreed in
-- QTcal_Zoho_Menu_and_Reconciliation_Report_Requirements.docx Section 5.2,
-- Option B: since none of this company's accounts have live Zoho bank feeds
-- connected, the admin uploads the same statement they already download from
-- their bank each period, and this app parses it independently of Zoho.
--
-- Three tables:
--   bank_statement_column_maps -- one saved mapping per account (which CSV/
--     Excel column is the date, description, amount, etc.) so re-uploading
--     next month's statement from the same account doesn't ask again.
--   bank_statement_imports -- one row per upload: which account, file name,
--     who uploaded it, and the date range actually found in the parsed rows
--     (not a forced "period" -- a statement can span a month boundary).
--   bank_statement_transactions -- the parsed rows themselves. match_status/
--     matched_zoho_transaction_id/matched_at are unused placeholders until
--     Phase 3's matching engine is built; every row starts 'unmatched'.

create table if not exists bank_statement_column_maps (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  zoho_account_id text not null,
  date_col text not null,
  desc_col text not null,
  -- 'single': one signed Amount column (negative = money out, unless flipped).
  -- 'split': separate Debit/Withdrawal and Credit/Deposit columns.
  amount_mode text not null default 'single' check (amount_mode in ('single','split')),
  amount_col text,
  amount_sign_flipped boolean not null default false,
  debit_col text,
  credit_col text,
  reference_col text,
  -- One of a small fixed set the upload UI offers (DMY / MDY / YMD / auto)
  -- rather than a free-text strptime pattern, so a mistyped format can't
  -- silently misparse every date in a statement.
  date_format text not null default 'auto',
  updated_at timestamptz default now(),
  unique(company_id, zoho_account_id)
);

create table if not exists bank_statement_imports (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  zoho_account_id text not null,
  account_name text,
  file_name text,
  uploaded_by uuid references public.users(id),
  uploaded_at timestamptz default now(),
  row_count integer not null default 0,
  date_min date,
  date_max date
);
create index if not exists idx_bsi_company_account on bank_statement_imports(company_id, zoho_account_id);

create table if not exists bank_statement_transactions (
  id uuid default gen_random_uuid() primary key,
  import_id uuid references bank_statement_imports(id) on delete cascade not null,
  company_id uuid references companies(id) on delete cascade not null,
  zoho_account_id text not null,
  txn_date date not null,
  description text,
  debit_amount numeric,
  credit_amount numeric,
  reference text,
  match_status text not null default 'unmatched' check (match_status in ('unmatched','matched','discrepancy')),
  matched_zoho_transaction_id text,
  matched_at timestamptz,
  created_at timestamptz default now()
);
create index if not exists idx_bst_company_account_date on bank_statement_transactions(company_id, zoho_account_id, txn_date);
create index if not exists idx_bst_import on bank_statement_transactions(import_id);

alter table bank_statement_column_maps enable row level security;
drop policy if exists tenant_isolation on bank_statement_column_maps;
create policy tenant_isolation on bank_statement_column_maps
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table bank_statement_imports enable row level security;
drop policy if exists tenant_isolation on bank_statement_imports;
create policy tenant_isolation on bank_statement_imports
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table bank_statement_transactions enable row level security;
drop policy if exists tenant_isolation on bank_statement_transactions;
create policy tenant_isolation on bank_statement_transactions
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);
-- Same caveat as every other migration in this app: api/*.py routes use the
-- Supabase service-role key and bypass RLS -- this is defense-in-depth, not
-- a live gate on today's routes.
