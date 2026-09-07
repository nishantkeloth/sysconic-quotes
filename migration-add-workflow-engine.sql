-- ─────────────────────────────────────────────────────────────────────────────
-- Configurable Workflow Engine.
-- Generalizes the approval-chain pattern already used by Payment Vouchers
-- (vouchers/voucher_approvals) and Quotes (quote_versions/quote_version_reviewers)
-- into one reusable engine: per document type -> stages -> approvers -> actions.
-- Idempotent / safe to re-run, matching this repo's other migration-*.sql files.
-- ─────────────────────────────────────────────────────────────────────────────

-- Which document types have workflows available, per company. Admin-editable
-- list (not hardcoded) -- seeded below with the four known types, disabled by
-- default so nothing changes behavior until an admin turns one on.
create table if not exists workflow_document_types (
  id            uuid default gen_random_uuid() primary key,
  company_id    uuid references companies(id) on delete cascade not null,
  doc_key       text not null,              -- 'quotes' | 'delivery_challans' | 'fsm_tickets' | 'vouchers' | future custom keys
  label         text not null,
  enabled       boolean default false,
  created_at    timestamptz default now(),
  unique(company_id, doc_key)
);

-- One workflow definition per document type (room for multiple/versioned
-- definitions later, but v1 UI will manage exactly one active one per type).
create table if not exists workflow_definitions (
  id                  uuid default gen_random_uuid() primary key,
  company_id          uuid references companies(id) on delete cascade not null,
  document_type_id    uuid references workflow_document_types(id) on delete cascade not null,
  name                text not null,
  is_active           boolean default true,
  created_at          timestamptz default now(),
  updated_at          timestamptz default now()
);
create index if not exists idx_workflow_definitions_doctype on workflow_definitions(document_type_id);

-- Ordered stages within a definition. `key` is a stable machine name an
-- integration can map to an existing status column value (e.g. quotes.status
-- = 'sent'); `requires_approval` + `approval_mode` control whether the
-- document can advance on its own or needs sign-off first.
create table if not exists workflow_stages (
  id                uuid default gen_random_uuid() primary key,
  workflow_id       uuid references workflow_definitions(id) on delete cascade not null,
  seq               integer not null,
  key               text not null,
  label             text not null,
  requires_approval boolean default false,
  approval_mode     text default 'all' check (approval_mode in ('any','all','sequential')),
  created_at        timestamptz default now(),
  unique(workflow_id, seq),
  unique(workflow_id, key)
);
create index if not exists idx_workflow_stages_workflow on workflow_stages(workflow_id);

-- Who can approve a given stage -- either a specific user or a role (e.g.
-- 'admin'), so a stage doesn't have to be pinned to named individuals.
create table if not exists workflow_stage_approvers (
  id                uuid default gen_random_uuid() primary key,
  stage_id          uuid references workflow_stages(id) on delete cascade not null,
  approver_user_id  uuid references users(id) on delete cascade,
  approver_role     text,
  seq               integer default 0,       -- ordering when approval_mode = 'sequential'
  created_at        timestamptz default now(),
  check (approver_user_id is not null or approver_role is not null)
);
create index if not exists idx_workflow_stage_approvers_stage on workflow_stage_approvers(stage_id);

-- Automations attached to a stage: fire on entering the stage, on approval,
-- or on rejection. `config` is action-specific JSON (e.g. notify_email:
-- {"to": "role:admin"} or {"to": "user:<uuid>"}).
create table if not exists workflow_stage_actions (
  id            uuid default gen_random_uuid() primary key,
  stage_id      uuid references workflow_stages(id) on delete cascade not null,
  trigger       text default 'on_enter' check (trigger in ('on_enter','on_approve','on_reject')),
  action_type   text not null,              -- 'notify_email' | 'webhook' | 'update_field'
  config        jsonb default '{}'::jsonb,
  created_at    timestamptz default now()
);
create index if not exists idx_workflow_stage_actions_stage on workflow_stage_actions(stage_id);

-- One row per real document that has an active workflow attached.
-- `document_id` is a polymorphic reference (no FK) since it points into
-- whichever table `document_type_id` names (quotes.id, delivery_challans.id, etc).
create table if not exists workflow_instances (
  id                  uuid default gen_random_uuid() primary key,
  company_id          uuid references companies(id) on delete cascade not null,
  document_type_id    uuid references workflow_document_types(id) not null,
  document_id         uuid not null,
  workflow_id         uuid references workflow_definitions(id) not null,
  current_stage_id    uuid references workflow_stages(id),
  status              text default 'in_progress' check (status in ('in_progress','approved','rejected','completed')),
  created_at          timestamptz default now(),
  updated_at          timestamptz default now(),
  unique(document_type_id, document_id)
);
create index if not exists idx_workflow_instances_company on workflow_instances(company_id);
create index if not exists idx_workflow_instances_doc on workflow_instances(document_type_id, document_id);

-- Per-approver decision at a given stage for a given document instance --
-- same shape as voucher_approvals (magic-link token + in-app respond),
-- generalized to any stage of any document type.
create table if not exists workflow_stage_approvals (
  id                uuid default gen_random_uuid() primary key,
  instance_id       uuid references workflow_instances(id) on delete cascade not null,
  stage_id          uuid references workflow_stages(id) not null,
  approver_user_id  uuid references users(id) not null,
  status            text default 'pending' check (status in ('pending','approved','rejected')),
  token             text unique,
  responded_at      timestamptz,
  created_at        timestamptz default now(),
  unique(instance_id, stage_id, approver_user_id)
);
create index if not exists idx_workflow_stage_approvals_instance on workflow_stage_approvals(instance_id);

-- RLS, same shape/caveat as every other table in this repo: backend modules
-- use the service-role key (bypasses RLS), so this is defense-in-depth, not
-- the live gate -- the real tenant isolation is the .eq('company_id', ...)
-- filters in api/workflows.py.
alter table workflow_document_types enable row level security;
drop policy if exists tenant_isolation on workflow_document_types;
create policy tenant_isolation on workflow_document_types
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table workflow_definitions enable row level security;
drop policy if exists tenant_isolation on workflow_definitions;
create policy tenant_isolation on workflow_definitions
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table workflow_stages enable row level security;
drop policy if exists tenant_isolation on workflow_stages;
create policy tenant_isolation on workflow_stages
  using (workflow_id in (select id from workflow_definitions where company_id = (auth.jwt() ->> 'company_id')::uuid));

alter table workflow_stage_approvers enable row level security;
drop policy if exists tenant_isolation on workflow_stage_approvers;
create policy tenant_isolation on workflow_stage_approvers
  using (stage_id in (
    select s.id from workflow_stages s
    join workflow_definitions d on d.id = s.workflow_id
    where d.company_id = (auth.jwt() ->> 'company_id')::uuid
  ));

alter table workflow_stage_actions enable row level security;
drop policy if exists tenant_isolation on workflow_stage_actions;
create policy tenant_isolation on workflow_stage_actions
  using (stage_id in (
    select s.id from workflow_stages s
    join workflow_definitions d on d.id = s.workflow_id
    where d.company_id = (auth.jwt() ->> 'company_id')::uuid
  ));

alter table workflow_instances enable row level security;
drop policy if exists tenant_isolation on workflow_instances;
create policy tenant_isolation on workflow_instances
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

alter table workflow_stage_approvals enable row level security;
drop policy if exists tenant_isolation on workflow_stage_approvals;
create policy tenant_isolation on workflow_stage_approvals
  using (instance_id in (select id from workflow_instances where company_id = (auth.jwt() ->> 'company_id')::uuid));

-- Turn the module on for every real tenant company, and seed the four known
-- document types disabled -- admin turns each on explicitly from Settings.
update companies set features = features || '{"workflowEngine": true}'::jsonb where is_internal = false;

insert into workflow_document_types (company_id, doc_key, label, enabled)
select id, dt.doc_key, dt.label, false
from companies, (values
  ('quotes', 'Quotes'),
  ('delivery_challans', 'Delivery Challans'),
  ('fsm_tickets', 'FSM Tickets'),
  ('vouchers', 'Payment Vouchers')
) as dt(doc_key, label)
where companies.is_internal = false
on conflict (company_id, doc_key) do nothing;
