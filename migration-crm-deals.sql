-- ─────────────────────────────────────────────────────────────────────────────
-- CRM Deals pipeline: a lightweight, configurable sales pipeline that sits
-- BEFORE a formal Quote exists. A deal starts as just a title + customer +
-- rough value; once it's real, one click converts it into a Quote (created
-- via the existing /api/quotes endpoint, then linked back via
-- deals.converted_quote_id — no duplicated quote-creation logic here).
--
-- Stages are per-company and fully configurable (add/rename/reorder/delete)
-- rather than hardcoded, since different sales processes want different
-- stage names. Each company gets a sensible default set auto-seeded the
-- first time it calls GET /api/deals/stages with none yet defined (see
-- api/quotes.py: _ensure_default_stages).
--
-- RLS follows the exact pattern in migrate-enable-rls-core-tables.sql
-- (defense-in-depth only — the app's real tenant isolation is enforced in
-- application code via claims['company_id'], same as every other table).
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists deal_stages (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  name text not null,
  sort_order int not null default 0,
  -- Marks a stage as a closing stage for reporting/next-best-action purposes.
  -- A stage can be both is_won=false/is_lost=false (an open pipeline stage),
  -- or exactly one of is_won/is_lost true (a terminal stage).
  is_won boolean not null default false,
  is_lost boolean not null default false,
  created_at timestamptz default now()
);
create index if not exists idx_deal_stages_company on deal_stages(company_id, sort_order);

create table if not exists deals (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  created_by uuid references users(id),
  owner_id uuid references users(id),
  title text not null,
  -- Free-typed like quotes.customer (not forced to an existing Customer row),
  -- but optionally linked to one when the user picks from the Customer Master.
  customer_id uuid references customers(id),
  customer_name text,
  value numeric default 0,
  currency text default 'AED',
  stage_id uuid references deal_stages(id),
  expected_close_date date,
  notes text,
  -- 'open' | 'won' | 'lost' — kept separate from stage_id so a deal's closed
  -- state survives even if its terminal stage is later renamed/reordered.
  status text not null default 'open' check (status in ('open','won','lost')),
  lost_reason text,
  converted_quote_id uuid references quotes(id),
  last_activity_at timestamptz default now(),
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists idx_deals_company on deals(company_id);
create index if not exists idx_deals_stage on deals(stage_id);
create index if not exists idx_deals_owner on deals(company_id, owner_id);
create index if not exists idx_deals_status on deals(company_id, status);

-- Activity/notes timeline per deal — same idea as quote_activity, shown as a
-- feed in the deal detail modal, and used to compute "days since last touch"
-- for the next-best-action suggestion.
create table if not exists deal_activity (
  id uuid default gen_random_uuid() primary key,
  deal_id uuid references deals(id) on delete cascade not null,
  company_id uuid references companies(id) on delete cascade not null,
  type text not null default 'note' check (type in ('note','stage_change','created','won','lost','converted')),
  body text,
  actor_id uuid references users(id),
  created_at timestamptz default now()
);
create index if not exists idx_deal_activity_deal on deal_activity(deal_id, created_at desc);

alter table deal_stages enable row level security;
drop policy if exists tenant_isolation on deal_stages;
create policy tenant_isolation on deal_stages
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table deals enable row level security;
drop policy if exists tenant_isolation on deals;
create policy tenant_isolation on deals
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table deal_activity enable row level security;
drop policy if exists tenant_isolation on deal_activity;
create policy tenant_isolation on deal_activity
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Verify.
select table_name, column_name, data_type
from information_schema.columns
where table_name in ('deal_stages','deals','deal_activity')
order by table_name, ordinal_position;
