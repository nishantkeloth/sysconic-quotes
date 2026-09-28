-- ─────────────────────────────────────────────────────────────────────────────
-- Receipt Vouchers — money RECEIVED from a customer (cash, cheque, bank
-- transfer, card), the counterpart to the existing Payment Vouchers module
-- (api/vouchers.py), which tracks money PAID OUT. Deliberately simpler than
-- Payment Vouchers: no approval workflow -- an accountant records a receipt
-- and it's immediately final, same as writing one out of a physical receipt
-- book. Gated behind the same 'vouchers' feature flag as Payment Vouchers
-- (Company Profile → Finance), with its own RBAC page key ('receiptVouchers')
-- so access can be granted independently.
--
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Numbering — atomic per-company-per-year sequence, same pattern as
--    delivery_challan_counters / dc_next_challan_number() ──────────────────
create table if not exists receipt_voucher_counters (
  company_id uuid not null references companies(id) on delete cascade,
  year int not null,
  seq int not null default 0,
  primary key (company_id, year)
);

create or replace function rv_next_receipt_number(p_company_id uuid, p_year int)
returns int
language plpgsql
as $$
declare
  v_seq int;
begin
  insert into receipt_voucher_counters (company_id, year, seq)
  values (p_company_id, p_year, 1)
  on conflict (company_id, year)
  do update set seq = receipt_voucher_counters.seq + 1
  returning seq into v_seq;
  return v_seq;
end;
$$;

-- ── Number format settings — one row per company: prefix + zero-padding
--    width. Admin-editable via Company Profile "Receipt Voucher numbering".
--    No row needed to work -- defaults ('RV', 4) apply in application code
--    when missing, giving RV-2026-0001 style numbers out of the box.
create table if not exists rv_numbering_settings (
  company_id uuid references companies(id) on delete cascade primary key,
  prefix text not null default 'RV',
  padding int not null default 4,
  updated_at timestamptz default now(),
  updated_by uuid references users(id) on delete set null
);

-- ── Receipt Vouchers ─────────────────────────────────────────────────────
create table if not exists receipt_vouchers (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  receipt_number text not null,
  receipt_date date not null default current_date,

  -- Free-typed like a paper receipt book ("Received from Mr./M/s. ...");
  -- optionally pre-filled from project_id/customer_id below, but never
  -- forced to match them -- the printed voucher is the source of truth for
  -- what was actually written, same spirit as delivery_challans.customer_name.
  received_from text not null,
  amount numeric not null check (amount > 0),
  currency text not null default 'AED',

  payment_method text not null default 'cash'
    check (payment_method in ('cash','cheque','bank_transfer','card')),
  cheque_number text,
  bank_name text,

  -- Optional links back into the app's own data, for reporting only -- see
  -- migration note above. Both nullable; a receipt need not be tied to
  -- either.
  project_id uuid references projects(id) on delete set null,
  customer_id uuid references customers(id) on delete set null,

  notes text,

  created_by uuid references users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_rv_company on receipt_vouchers(company_id, receipt_date desc);
create index if not exists idx_rv_project on receipt_vouchers(project_id);
create index if not exists idx_rv_customer on receipt_vouchers(customer_id);

alter table receipt_vouchers enable row level security;
drop policy if exists tenant_isolation on receipt_vouchers;
create policy tenant_isolation on receipt_vouchers
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table rv_numbering_settings enable row level security;
drop policy if exists tenant_isolation on rv_numbering_settings;
create policy tenant_isolation on rv_numbering_settings
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);
-- Same caveat as every other migration in this app: api/*.py routes use the
-- Supabase service-role key and bypass RLS -- this is defense-in-depth, not
-- a live gate on today's routes.

-- Verify:
-- select table_name, column_name, data_type from information_schema.columns
-- where table_name in ('receipt_vouchers','receipt_voucher_counters','rv_numbering_settings')
-- order by table_name, ordinal_position;
