-- CFO summary (mobile Insights): one JSON snapshot per company, rebuilt by
-- POST /api/integrations/sync-cfo from Zoho Books' Profit & Loss report,
-- bank balances and the already-synced invoices/bills.
create table if not exists cfo_snapshots (
  company_id uuid primary key references companies(id) on delete cascade,
  payload jsonb not null,
  synced_at timestamptz default now()
);
alter table cfo_snapshots enable row level security;
drop policy if exists tenant_isolation on cfo_snapshots;
create policy tenant_isolation on cfo_snapshots using (company_id = (auth.jwt() ->> 'company_id')::uuid);
