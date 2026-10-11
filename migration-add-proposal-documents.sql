-- Proposals module, M4-M6: immutable document versions, approval, source refresh.
-- Additive only. Run on STAGING first (after migration-add-proposal-previews.sql).

create table if not exists proposal_documents (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  proposal_id uuid references proposals(id) on delete cascade not null,
  version int not null,
  preview_id uuid not null,
  snapshot_id uuid,
  source_hash text,
  options jsonb not null default '{}'::jsonb,       -- {show_prices, include_terms, cover_note}
  content jsonb not null,                           -- frozen: writeup, image_path, lines, commercials, titles
  status text not null default 'draft' check (status in ('draft','pending_approval','approved','rejected')),
  submitted_by uuid, submitted_at timestamptz,
  decided_by uuid, decided_at timestamptz, decision_note text,
  created_by uuid,
  created_at timestamptz default now(),
  unique (proposal_id, version)
);
create index if not exists idx_pd_proposal on proposal_documents(proposal_id, version desc);

alter table proposals add column if not exists current_document_id uuid;

alter table proposal_documents enable row level security;
drop policy if exists tenant_isolation on proposal_documents;
create policy tenant_isolation on proposal_documents using (company_id = (auth.jwt() ->> 'company_id')::uuid);
