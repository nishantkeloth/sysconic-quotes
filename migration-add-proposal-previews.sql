-- Proposals module, milestone M3: AI preview packages (image + write-up).
-- One row = one preview package AND its generation job (queued/running/ready/failed).
-- Additive only. Run on STAGING first.

create table if not exists proposal_previews (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  proposal_id uuid references proposals(id) on delete cascade not null,
  seq int not null,                                  -- 1,2,3... per proposal
  status text not null default 'queued' check (status in ('queued','running','ready','failed')),
  instruction text,                                  -- extra guidance for this attempt
  snapshot_id uuid,                                  -- source snapshot used
  writeup jsonb,                                     -- {headline, summary, sections[], highlights[]}
  image_prompt text,
  image_path text,                                   -- storage path in company-assets
  image_provider text,
  image_model text,
  text_provider text,
  text_model text,
  settings_version int,
  error text,
  stage_detail text,                                 -- 'writing' / 'rendering' for the poller
  created_by uuid,
  created_at timestamptz default now(),
  started_at timestamptz,
  finished_at timestamptz,
  unique (proposal_id, seq)
);
create index if not exists idx_pp_proposal on proposal_previews(proposal_id, seq desc);
create index if not exists idx_pp_company_status on proposal_previews(company_id, status);

alter table proposals add column if not exists selected_preview_id uuid;
alter table proposals add column if not exists selected_at timestamptz;

alter table proposal_previews enable row level security;
drop policy if exists tenant_isolation on proposal_previews;
create policy tenant_isolation on proposal_previews using (company_id = (auth.jwt() ->> 'company_id')::uuid);
