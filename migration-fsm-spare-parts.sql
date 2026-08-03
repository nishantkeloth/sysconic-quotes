-- ─────────────────────────────────────────────────────────────────────────────
-- FSM Module 12 — Spare Parts
--
-- Deliberately a separate inventory from the sales product catalog
-- (products/api/products.py) — that catalog is for quoting new equipment,
-- this is field-service stock (on-hand quantities, stock location,
-- consumption against tickets). Different lifecycle, different fields.
--
-- fsm_spare_parts.quantity_on_hand and quantity_reserved are maintained by
-- application code (api/fsm_parts.py) each time a fsm_spare_part_transactions
-- row is inserted — not computed on read, so a part's current stock is a
-- single fast lookup rather than summing the whole transaction history
-- every time.
--
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists fsm_spare_parts (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  part_code text not null,
  name text not null,
  category text,
  manufacturer text,
  compatible_models text,
  supplier text,
  stock_location text,
  quantity_on_hand integer not null default 0,
  quantity_reserved integer not null default 0,
  reorder_level integer,
  unit_cost numeric,
  is_active boolean not null default true,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique (company_id, part_code)
);
create index if not exists idx_fsm_spare_parts_company on fsm_spare_parts(company_id);

create table if not exists fsm_spare_part_transactions (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  part_id uuid references fsm_spare_parts(id) on delete cascade not null,
  ticket_id uuid references fsm_tickets(id),
  work_order_id uuid references fsm_work_orders(id),
  type text not null check (type in
    ('stock_in','reservation','release_reservation','consumption','return','warranty_replacement','adjustment')),
  quantity integer not null,
  notes text,
  actor_id uuid references users(id),
  created_at timestamptz default now()
);
create index if not exists idx_fsm_spare_part_txn_part on fsm_spare_part_transactions(part_id, created_at desc);
create index if not exists idx_fsm_spare_part_txn_company on fsm_spare_part_transactions(company_id);

alter table fsm_spare_parts enable row level security;
drop policy if exists tenant_isolation on fsm_spare_parts;
create policy tenant_isolation on fsm_spare_parts
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table fsm_spare_part_transactions enable row level security;
drop policy if exists tenant_isolation on fsm_spare_part_transactions;
create policy tenant_isolation on fsm_spare_part_transactions
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Verify.
select table_name, column_name, data_type
from information_schema.columns
where table_name in ('fsm_spare_parts','fsm_spare_part_transactions')
order by table_name, ordinal_position;
