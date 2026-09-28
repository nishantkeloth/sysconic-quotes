-- ============================================================
-- Production catch-up migration -- 2026-09-28
-- Combines every migration merged into staging since the last
-- production sync (migration-production-2026-09-27.sql):
--   1. migration-add-receipt-vouchers.sql
--   2. migration-add-voucher-seal.sql
--   3. migration-add-payable-reminders.sql
-- All idempotent (safe to re-run / run individually too).
-- ============================================================

-- ── 1. Receipt Vouchers ─────────────────────────────────────
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

-- ── 2. Receipt Voucher seal/stamp column ────────────────────
-- Company seal/stamp image, printed at the bottom of a Receipt Voucher PDF.
--
-- Deliberately a separate column from companies.logo_url: the letterhead
-- logo used on Quotation PDFs and the round rubber-stamp seal used on a
-- Receipt Voucher (see the user's own sample voucher) are different assets
-- in practice. Set via POST /api/auth/company-profile/voucher-seal
-- (api/auth.py upload_voucher_seal()), same signed-URL storage pattern as
-- the existing logo upload.
--
-- Safe to re-run (idempotent).

ALTER TABLE companies ADD COLUMN IF NOT EXISTS voucher_seal_url TEXT;

-- ── 3. Payable reminders dedupe table ───────────────────────
-- Daily payables reminder (Insights "Payables" section)
--
-- The Vendor payment tracker is manually entered per quote (Quote editor ->
-- Vendors tab) -- easy to lose track of once there are several awarded
-- quotes each with their own vendor payment dates. api/quotes.py's
-- run_payable_reminders() (a daily cron, see vercel.json) scans every
-- company's awarded quotes for payables that are overdue or due within a
-- few days and emails the company's admins a digest.
--
-- This table exists purely to stop that cron from sending more than one
-- digest per company per day if it's ever triggered twice (or retried) --
-- it deliberately does NOT dedupe per individual payable, since the whole
-- point is to keep reminding once a day for as long as something stays
-- unpaid, not to notify once and go silent.
create table if not exists payable_reminder_state (
  company_id uuid references companies(id) on delete cascade primary key,
  last_sent_date date,
  updated_at timestamptz not null default now()
);

alter table payable_reminder_state enable row level security;
drop policy if exists tenant_isolation on payable_reminder_state;
create policy tenant_isolation on payable_reminder_state
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);
-- Same caveat as every other migration in this app: api/*.py routes use the
-- Supabase service-role key and bypass RLS -- this is defense-in-depth, not
-- a live gate on today's routes.
