-- ─────────────────────────────────────────────────────────────────────────────
-- Delivery Challans — goods-movement documents for AV equipment leaving the
-- warehouse (installation delivery, loan/demo units, RMA returns to vendor,
-- warehouse transfers, returns from site). Deliberately has NO rate/tax/amount
-- columns -- this is a proof-of-delivery + traceability document, not a
-- commercial one (that's what Quotes/Invoices are for).
--
-- Serial numbers captured per line feed straight into the existing Field
-- Service Assets master (fsm_assets) when the company has fsm_module enabled
-- and the challan is linked to a real fsm_site -- see api/delivery_challans.py
-- auto_create_assets(). Every unit delivered is already tracked, tied to the
-- customer/site, before a single service ticket is ever raised.
--
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Numbering — atomic per-company-per-year sequence, same pattern as
--    fsm_ticket_counters / fsm_next_ticket_number ───────────────────────────
create table if not exists delivery_challan_counters (
  company_id uuid not null references companies(id) on delete cascade,
  year int not null,
  seq int not null default 0,
  primary key (company_id, year)
);

create or replace function dc_next_challan_number(p_company_id uuid, p_year int)
returns int
language plpgsql
as $$
declare
  v_seq int;
begin
  insert into delivery_challan_counters (company_id, year, seq)
  values (p_company_id, p_year, 1)
  on conflict (company_id, year)
  do update set seq = delivery_challan_counters.seq + 1
  returning seq into v_seq;
  return v_seq;
end;
$$;

-- ── Challans ─────────────────────────────────────────────────────────────
create table if not exists delivery_challans (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  challan_number text not null,
  challan_type text not null default 'installation'
    check (challan_type in ('installation','loan_demo','rma_return','warehouse_transfer','site_return')),
  status text not null default 'draft'
    check (status in ('draft','dispatched','delivered','returned','cancelled')),
  challan_date date not null default current_date,

  -- Links back into the app's own data instead of free-typed references.
  quote_id uuid references quotes(id) on delete set null,
  project_id uuid references projects(id) on delete set null,
  site_id uuid references fsm_sites(id) on delete set null,

  customer_name text,
  delivery_address text,
  warehouse text,

  -- Only meaningful for challan_type = 'loan_demo'; drives the pending-
  -- returns view rather than being enforced at the DB level.
  expected_return_date date,

  vehicle_number text,
  driver_name text,
  packages_count text,
  total_weight_kg numeric,
  dispatch_time timestamptz,

  -- Site acknowledgment. Typed attestation for now (name + designation +
  -- timestamp), matching the existing customer_signature_name pattern used
  -- on FSM work order completion -- a drawn signature pad / photo upload
  -- would need real file storage, which nothing in this app has yet.
  received_by_name text,
  received_by_designation text,
  received_at timestamptz,

  customer_notes text,
  terms_conditions text,

  created_by uuid references users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_dc_company on delivery_challans(company_id);
create index if not exists idx_dc_status on delivery_challans(company_id, status);
create index if not exists idx_dc_project on delivery_challans(project_id);
create index if not exists idx_dc_quote on delivery_challans(quote_id);

-- ── Line items ───────────────────────────────────────────────────────────
create table if not exists delivery_challan_items (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  challan_id uuid not null references delivery_challans(id) on delete cascade,
  product_id uuid references products(id) on delete set null,

  item_name text not null,
  brand text,
  model_no text,
  quantity numeric not null default 1,
  unit text not null default 'pcs',
  condition text not null default 'new' check (condition in ('new','refurbished','demo')),
  serial_numbers text[] not null default '{}',
  remarks text,

  -- fsm_assets.id rows created from this line's serial numbers (empty until
  -- auto_create_assets() runs). Kept here rather than only on fsm_assets so
  -- "already traced" can be shown on the challan without a join back.
  fsm_asset_ids uuid[] not null default '{}',

  sort_order int not null default 0,
  created_at timestamptz not null default now()
);
create index if not exists idx_dc_items_challan on delivery_challan_items(challan_id);
create index if not exists idx_dc_items_company on delivery_challan_items(company_id);

alter table delivery_challans enable row level security;
drop policy if exists tenant_isolation on delivery_challans;
create policy tenant_isolation on delivery_challans
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table delivery_challan_items enable row level security;
drop policy if exists tenant_isolation on delivery_challan_items;
create policy tenant_isolation on delivery_challan_items
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Verify.
select table_name, column_name, data_type
from information_schema.columns
where table_name in ('delivery_challans','delivery_challan_items','delivery_challan_counters')
order by table_name, ordinal_position;
