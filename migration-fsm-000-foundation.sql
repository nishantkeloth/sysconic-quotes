-- ─────────────────────────────────────────────────────────────────────────────
-- FSM Phase 1 — Foundation schema (Sites, Assets, Engineers, Tickets,
-- Activity Timeline, Work Orders, Notifications, ticket numbering)
--
-- This is the base schema every later FSM migration (Modules 5, 8-18) ALTERs
-- with `add column if not exists`. It was originally created directly
-- against the staging database in an earlier session and never saved as a
-- migration file — reconstructed here field-for-field from what
-- api/fsm_assets.py, api/fsm_tickets.py and api/fsm_engineers.py actually
-- insert/select, since no other source of truth for it exists in this repo.
--
-- MUST run first, before every other migration-fsm-*.sql file — everything
-- else ALTERs these tables or references them via foreign key.
--
-- Safe to re-run (idempotent) — every statement is `if not exists`.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Sites ────────────────────────────────────────────────────────────────
create table if not exists fsm_sites (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  customer_id uuid not null,
  name text not null,
  address text,
  building text,
  floor text,
  room text,
  department text,
  contact_name text,
  contact_phone text,
  contact_email text,
  notes text,
  is_deleted boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_fsm_sites_company on fsm_sites(company_id);
create index if not exists idx_fsm_sites_customer on fsm_sites(customer_id);

alter table fsm_sites enable row level security;
drop policy if exists tenant_isolation on fsm_sites;
create policy tenant_isolation on fsm_sites
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- ── Assets ───────────────────────────────────────────────────────────────
create table if not exists fsm_assets (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  site_id uuid not null references fsm_sites(id) on delete cascade,
  parent_asset_id uuid references fsm_assets(id),
  asset_code text not null,
  category text,
  manufacturer text,
  model text,
  serial_number text,
  barcode text,
  installation_date date,
  warranty_expiry date,
  supplier text,
  purchase_order text,
  firmware_version text,
  software_version text,
  status text not null default 'active',
  photo_urls jsonb not null default '[]',
  document_urls jsonb not null default '[]',
  is_deleted boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists uq_fsm_assets_code_per_company on fsm_assets(company_id, asset_code) where not is_deleted;
create index if not exists idx_fsm_assets_company on fsm_assets(company_id);
create index if not exists idx_fsm_assets_site on fsm_assets(site_id);

alter table fsm_assets enable row level security;
drop policy if exists tenant_isolation on fsm_assets;
create policy tenant_isolation on fsm_assets
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- ── Engineers (base 10 columns only — skills/certifications/availability/
-- notes are added later by migration-fsm-engineers.sql) ────────────────────
create table if not exists fsm_engineers (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  user_id uuid,
  name text not null,
  phone text,
  email text,
  territory text,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_fsm_engineers_company on fsm_engineers(company_id);

alter table fsm_engineers enable row level security;
drop policy if exists tenant_isolation on fsm_engineers;
create policy tenant_isolation on fsm_engineers
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- ── Ticket numbering — atomic per-company-per-year sequence ────────────────
create table if not exists fsm_ticket_counters (
  company_id uuid not null references companies(id) on delete cascade,
  year int not null,
  seq int not null default 0,
  primary key (company_id, year)
);

create or replace function fsm_next_ticket_number(p_company_id uuid, p_year int)
returns int
language plpgsql
as $$
declare
  v_seq int;
begin
  insert into fsm_ticket_counters (company_id, year, seq)
  values (p_company_id, p_year, 1)
  on conflict (company_id, year)
  do update set seq = fsm_ticket_counters.seq + 1
  returning seq into v_seq;
  return v_seq;
end;
$$;

-- ── Tickets (base columns only — SLA/billing/corrective-maintenance columns
-- are added later by their own migration-fsm-*.sql files) ──────────────────
create table if not exists fsm_tickets (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  ticket_number text not null,
  site_id uuid references fsm_sites(id),
  asset_id uuid references fsm_assets(id),
  customer_id uuid not null,
  ticket_type text not null,
  priority text not null default 'medium',
  status text not null default 'new',
  source text not null default 'manual',
  subject text not null,
  description text,
  assigned_engineer_id uuid references fsm_engineers(id),
  is_deleted boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists uq_fsm_tickets_number_per_company on fsm_tickets(company_id, ticket_number);
create index if not exists idx_fsm_tickets_company on fsm_tickets(company_id);
create index if not exists idx_fsm_tickets_customer on fsm_tickets(customer_id);
create index if not exists idx_fsm_tickets_site on fsm_tickets(site_id);
create index if not exists idx_fsm_tickets_status on fsm_tickets(company_id, status);
create index if not exists idx_fsm_tickets_engineer on fsm_tickets(assigned_engineer_id);

alter table fsm_tickets enable row level security;
drop policy if exists tenant_isolation on fsm_tickets;
create policy tenant_isolation on fsm_tickets
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- ── Activity Timeline (append-only audit trail — no updated_at/is_deleted) ──
create table if not exists fsm_ticket_activity (
  id uuid primary key default gen_random_uuid(),
  ticket_id uuid not null references fsm_tickets(id) on delete cascade,
  company_id uuid not null references companies(id) on delete cascade,
  event_type text not null,
  actor_id uuid,
  note text,
  metadata jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index if not exists idx_fsm_ticket_activity_ticket on fsm_ticket_activity(ticket_id, created_at);
create index if not exists idx_fsm_ticket_activity_company on fsm_ticket_activity(company_id);

alter table fsm_ticket_activity enable row level security;
drop policy if exists tenant_isolation on fsm_ticket_activity;
create policy tenant_isolation on fsm_ticket_activity
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- ── Work Orders ──────────────────────────────────────────────────────────
create table if not exists fsm_work_orders (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  ticket_id uuid not null references fsm_tickets(id) on delete cascade,
  engineer_id uuid references fsm_engineers(id),
  visit_date date,
  expected_duration_mins int,
  checklist jsonb not null default '[]',
  materials_required text,
  tools_required text,
  instructions text,
  attachments jsonb not null default '[]',
  completion_notes text,
  signature_url text,
  is_deleted boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_fsm_work_orders_company on fsm_work_orders(company_id);
create index if not exists idx_fsm_work_orders_ticket on fsm_work_orders(ticket_id);
create index if not exists idx_fsm_work_orders_engineer on fsm_work_orders(engineer_id, visit_date);

alter table fsm_work_orders enable row level security;
drop policy if exists tenant_isolation on fsm_work_orders;
create policy tenant_isolation on fsm_work_orders
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- ── Notifications (base columns — contract_id added later by
-- migration-fsm-notifications.sql) ──────────────────────────────────────────
create table if not exists fsm_notifications (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  ticket_id uuid references fsm_tickets(id),
  recipient_email text,
  event_type text not null,
  status text not null default 'pending',
  created_at timestamptz not null default now(),
  sent_at timestamptz
);
create index if not exists idx_fsm_notifications_company on fsm_notifications(company_id);
create index if not exists idx_fsm_notifications_ticket on fsm_notifications(ticket_id);

alter table fsm_notifications enable row level security;
drop policy if exists tenant_isolation on fsm_notifications;
create policy tenant_isolation on fsm_notifications
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Verify — should list all 8 tables + the counters helper table.
select table_name
from information_schema.tables
where table_schema = 'public' and table_name like 'fsm_%'
order by table_name;
