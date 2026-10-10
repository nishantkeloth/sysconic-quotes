-- Proposals module, milestone M1 (foundation): register, numbering, versioned
-- dedicated Proposal AI settings. Preview/document/job tables arrive with
-- later milestones. Additive only.

create table if not exists proposal_counters (
  company_id uuid references companies(id) on delete cascade not null,
  year int not null,
  last_seq int not null default 0,
  primary key (company_id, year)
);

create or replace function proposal_next_number(p_company_id uuid, p_year int)
returns int language plpgsql as $$
declare v int;
begin
  insert into proposal_counters (company_id, year, last_seq) values (p_company_id, p_year, 1)
  on conflict (company_id, year) do update set last_seq = proposal_counters.last_seq + 1
  returning last_seq into v;
  return v;
end $$;

create table if not exists proposals (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  reference text not null,
  title text,
  customer_name text,
  source_kind text not null default 'quotation' check (source_kind in ('quotation','bom')),
  quote_id uuid,
  quote_ref text,
  instruction text,
  owner_id uuid,
  lifecycle text not null default 'active' check (lifecycle in ('active','archived')),
  stage text not null default 'draft',
  lock_version int not null default 1,
  archived_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique (company_id, reference)
);
create index if not exists idx_proposals_company on proposals(company_id, lifecycle, updated_at desc, id);
create index if not exists idx_proposals_quote on proposals(company_id, quote_id);

create table if not exists proposal_source_snapshots (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  proposal_id uuid references proposals(id) on delete cascade not null,
  safe_source jsonb not null,           -- customer-safe projection: no cost/margin/supplier discount
  source_hash text not null,
  visibility text not null default 'commercial' check (visibility in ('technical','commercial')),
  created_by uuid,
  created_at timestamptz default now()
);
create index if not exists idx_pss_proposal on proposal_source_snapshots(proposal_id, created_at desc);

create table if not exists company_proposal_ai_settings_versions (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  version int not null,
  text_profile jsonb not null default '{}'::jsonb,
  image_profile jsonb not null default '{}'::jsonb,
  text_fallback jsonb,
  image_fallback jsonb,
  limits jsonb not null default '{}'::jsonb,
  validation jsonb,
  is_active boolean not null default false,
  created_by uuid,
  created_at timestamptz default now(),
  unique (company_id, version)
);
create unique index if not exists uq_cpai_one_active on company_proposal_ai_settings_versions(company_id) where is_active;

alter table proposals enable row level security;
alter table proposal_source_snapshots enable row level security;
alter table company_proposal_ai_settings_versions enable row level security;
alter table proposal_counters enable row level security;
drop policy if exists tenant_isolation on proposals;
create policy tenant_isolation on proposals using (company_id = (auth.jwt() ->> 'company_id')::uuid);
drop policy if exists tenant_isolation on proposal_source_snapshots;
create policy tenant_isolation on proposal_source_snapshots using (company_id = (auth.jwt() ->> 'company_id')::uuid);
drop policy if exists tenant_isolation on company_proposal_ai_settings_versions;
create policy tenant_isolation on company_proposal_ai_settings_versions using (company_id = (auth.jwt() ->> 'company_id')::uuid);
