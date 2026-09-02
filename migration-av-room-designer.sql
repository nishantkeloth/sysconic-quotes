-- ─────────────────────────────────────────────────────────────────────────────
-- AV Room Designer — Phase 1 data model only (schema + backend CRUD; no
-- canvas/UI yet). Three tables, matching the "one common object model" and
-- "structured JSON/DB records, not screenshots" principles from the spec:
--
--   av_design_projects  — a customer engagement that can contain multiple rooms
--   av_rooms             — one room's dimensions/status within a project
--   av_room_objects       — one placed device/furniture item within a room,
--                          with x/y/z position, rotation, and a metadata_json
--                          escape hatch for device-type-specific engineering
--                          fields (camera FOV, mic pickup radius, etc.) so
--                          those can evolve without further migrations.
--
-- Deliberately NOT included yet (later phases, per the spec's own phasing):
-- av_design_rules, av_room_connections (cable estimation), av_design_versions
-- (version snapshots) — Phase 1 here is just "create a room, place devices,
-- persist positions, map to real products, generate a BOM into an existing
-- quote". Version history and the rules engine come once that's proven out.
--
-- Gated behind the `av_room_designer` company feature flag (companies.features
-- jsonb, same mechanism as delivery_challans/fsm_module) + the `avRoomDesigner`
-- RBAC page key -- inert for every company until both are explicitly enabled.
--
-- Tenant isolation follows the established dual pattern (see
-- migrate-enable-rls-core-tables.sql): every table carries its own
-- company_id column, filtered explicitly in application code (api/av_rooms.py
-- uses the service-role key like every other route), PLUS an RLS policy here
-- as a defense-in-depth backstop matching auth.jwt() ->> 'company_id'.
--
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Design projects ──────────────────────────────────────────────────────────
-- A design engagement, optionally linked to an existing quote/customer. Named
-- av_design_projects (not "projects") to avoid any confusion with the
-- existing `projects` table, which tracks Zoho-synced financial actuals for
-- Project Performance -- a completely different concept. A design project
-- MAY later be linked to a Project Performance `projects` row via
-- pp_project_id once that handoff is designed, but that's out of scope here.
create table if not exists av_design_projects (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,

  project_name text not null,
  customer_id uuid references customers(id) on delete set null,
  quote_id uuid references quotes(id) on delete set null,
  deal_id uuid references deals(id) on delete set null,

  status text not null default 'draft'
    check (status in ('draft','concept','under_review','approved','quotation_generated','locked')),

  created_by uuid references users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_avdp_company on av_design_projects(company_id);
create index if not exists idx_avdp_quote on av_design_projects(quote_id);
create index if not exists idx_avdp_customer on av_design_projects(customer_id);

-- ── Rooms ────────────────────────────────────────────────────────────────────
-- One room's dimensions/status within a project. `quantity` is the "Room
-- Multiplier" from the spec (section 30) -- e.g. one "Small Meeting Room"
-- design representing 20 physical rooms, so BOM generation can multiply
-- this room's device counts by `quantity` for the consolidated project BOM
-- while still preserving the single per-room design as the source of truth.
create table if not exists av_rooms (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  project_id uuid not null references av_design_projects(id) on delete cascade,

  room_name text not null,
  room_type text,

  -- Stored in `units` as entered; conversion to a canonical unit for
  -- calculations (e.g. always meters internally) is an application-layer
  -- concern, not enforced here, since the spec wants the *displayed* unit
  -- to be whatever the user picked (mm/cm/m/ft/in).
  length numeric,
  width numeric,
  height numeric,
  ceiling_height numeric,
  units text not null default 'm' check (units in ('mm','cm','m','ft','in')),

  capacity int,
  seating_capacity int,
  quantity int not null default 1,

  version int not null default 1,
  status text not null default 'draft'
    check (status in ('draft','concept','under_review','approved','quotation_generated','locked')),

  created_by uuid references users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_avr_company on av_rooms(company_id);
create index if not exists idx_avr_project on av_rooms(project_id);

-- ── Room objects (placed devices/furniture) ─────────────────────────────────
-- One row per placed object. `product_id` is null for a "generic device"
-- (e.g. just "Camera") and set once the user maps it to a real QTcal
-- product (spec section 9) -- object_name/category still carry enough
-- info to render and BOM a generic device even before that mapping happens.
-- metadata_json is the deliberate escape hatch for device-type-specific
-- engineering fields (camera horizontal/vertical FOV, mic pickup radius,
-- display recommended viewing distance, mounting_type, etc.) so those can
-- be added/changed per device category without a schema migration each
-- time -- structured validation of what's expected per category belongs in
-- the application layer (TypeScript interfaces per the spec's own section
-- 59/60), not the DB.
create table if not exists av_room_objects (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  room_id uuid not null references av_rooms(id) on delete cascade,

  object_type text not null,       -- e.g. 'device' | 'furniture'
  category text not null,          -- e.g. 'camera', 'display', 'ceiling_microphone', 'table'
  object_name text not null,       -- display label, e.g. "Camera 1" or the product's name once mapped
  product_id uuid references products(id) on delete set null,

  position_x numeric not null default 0,
  position_y numeric not null default 0,
  position_z numeric not null default 0,
  rotation_x numeric not null default 0,
  rotation_y numeric not null default 0,
  rotation_z numeric not null default 0,
  width numeric,
  height numeric,
  depth numeric,

  mounting_height numeric,
  mounting_type text,
  quantity int not null default 1,
  notes text,

  metadata_json jsonb not null default '{}',

  created_by uuid references users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_avro_company on av_room_objects(company_id);
create index if not exists idx_avro_room on av_room_objects(room_id);
create index if not exists idx_avro_product on av_room_objects(product_id);

-- ── RLS (defense-in-depth backstop; app code remains the primary gate) ──────
alter table av_design_projects enable row level security;
drop policy if exists tenant_isolation on av_design_projects;
create policy tenant_isolation on av_design_projects
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table av_rooms enable row level security;
drop policy if exists tenant_isolation on av_rooms;
create policy tenant_isolation on av_rooms
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table av_room_objects enable row level security;
drop policy if exists tenant_isolation on av_room_objects;
create policy tenant_isolation on av_room_objects
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Verify.
select table_name, column_name, data_type
from information_schema.columns
where table_name in ('av_design_projects','av_rooms','av_room_objects')
order by table_name, ordinal_position;
