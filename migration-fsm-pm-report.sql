-- ─────────────────────────────────────────────────────────────────────────────
-- FSM — Preventive Maintenance Report generation
--
-- Adds what's needed to generate a branded PM report PDF (like a vendor's
-- service documentation report) from a completed work order:
--   1. fsm_work_orders.report_meta — health rating, key observations,
--      customer sign-off name/date, revision, functional test results.
--   2. fsm_work_order_assets — per-asset (per-room) pass/fail + remarks for
--      that specific visit, so the report can show a device/room table like
--      "System Inventory & Preventive Checklist" in the sample report.
--
-- fsm_work_orders.checklist (already exists, jsonb array) is unchanged in
-- shape at the DB level — the frontend now writes richer items
-- {text, done, category, remarks} into the same column instead of the old
-- {text, done}, which is backward compatible (old items still render fine).
--
-- Depends on: migration-fsm-000-foundation.sql (fsm_work_orders, fsm_assets).
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

alter table fsm_work_orders add column if not exists report_meta jsonb not null default '{}'::jsonb;

create table if not exists fsm_work_order_assets (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  work_order_id uuid not null references fsm_work_orders(id) on delete cascade,
  asset_id uuid not null references fsm_assets(id) on delete cascade,
  status text not null default 'completed',   -- 'completed' | 'issue_found' | 'not_applicable'
  remarks text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists uq_fsm_wo_assets_per_wo on fsm_work_order_assets(work_order_id, asset_id);
create index if not exists idx_fsm_wo_assets_company on fsm_work_order_assets(company_id);
create index if not exists idx_fsm_wo_assets_wo on fsm_work_order_assets(work_order_id);

alter table fsm_work_order_assets enable row level security;
drop policy if exists tenant_isolation on fsm_work_order_assets;
create policy tenant_isolation on fsm_work_order_assets
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Verify
select column_name from information_schema.columns
where table_name = 'fsm_work_orders' and column_name = 'report_meta';

select table_name from information_schema.tables
where table_schema = 'public' and table_name = 'fsm_work_order_assets';
