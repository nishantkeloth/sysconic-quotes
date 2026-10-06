-- Salaries (Insights): admin picks which Zoho Chart-of-Accounts EXPENSE
-- accounts hold payroll (e.g. "Salaries & Employee Wages"); sync pulls every
-- ledger entry on them into salary_payments, grouped by month + employee
-- (employee = the entry's payee, else its description).
create table if not exists salary_accounts (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  zoho_account_id text not null,
  account_name text,
  is_tracked boolean not null default true,
  last_synced_at timestamptz,
  created_at timestamptz default now(),
  unique(company_id, zoho_account_id)
);
create table if not exists salary_payments (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  zoho_account_id text not null,
  txn_key text not null,
  pay_date date,
  month text,            -- 'YYYY-MM'
  employee text,
  amount numeric default 0,
  description text,
  unique(company_id, txn_key)
);
create index if not exists idx_salary_pay_month on salary_payments(company_id, month);
alter table salary_accounts enable row level security;
alter table salary_payments enable row level security;
drop policy if exists tenant_isolation on salary_accounts;
create policy tenant_isolation on salary_accounts using (company_id = (auth.jwt() ->> 'company_id')::uuid);
drop policy if exists tenant_isolation on salary_payments;
create policy tenant_isolation on salary_payments using (company_id = (auth.jwt() ->> 'company_id')::uuid);
