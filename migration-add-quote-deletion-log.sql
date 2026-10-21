-- Quote deletion (administrators only). Every deletion is recorded here with a full
-- copy of the quote row so it can be restored by hand if needed. Additive and safe to re-run.
create table if not exists quote_deletion_log (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  quote_id uuid not null,
  quote_ref text,
  title text,
  customer text,
  status text,
  total_sell numeric,
  deleted_by uuid,
  deleted_by_name text,
  deleted_at timestamptz default now(),
  snapshot jsonb              -- the complete quotes row at the time of deletion
);
create index if not exists idx_qdl_company on quote_deletion_log(company_id, deleted_at desc);
alter table quote_deletion_log enable row level security;
drop policy if exists tenant_isolation on quote_deletion_log;
create policy tenant_isolation on quote_deletion_log using (company_id = (auth.jwt() ->> 'company_id')::uuid);
