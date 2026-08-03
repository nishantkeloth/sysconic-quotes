-- FSM Module 14: Customer Portal
-- Idempotent. Two new tables — no changes to existing FSM tables.
--
-- fsm_portal_users: one login per customer contact. password_hash is
-- nullable until the invite is accepted (row exists in a "pending" state
-- via fsm_portal_invites first).
-- fsm_portal_invites: doubles as both "first activation" and "reset my
-- password" — re-sending an invite to an already-active user just lets
-- them set a new password via the same accept flow, so there's no need
-- for a separate password-reset table.

create table if not exists fsm_portal_users (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id),
    customer_id uuid not null,
    email text not null,
    name text not null default '',
    phone text,
    password_hash text,
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    last_login_at timestamptz
);

create unique index if not exists idx_fsm_portal_users_email_per_company on fsm_portal_users(company_id, lower(email));
create index if not exists idx_fsm_portal_users_customer on fsm_portal_users(customer_id);

create table if not exists fsm_portal_invites (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id),
    customer_id uuid not null,
    email text not null,
    name text,
    token text not null unique,
    invited_by uuid,
    accepted boolean not null default false,
    expires_at timestamptz not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_fsm_portal_invites_token on fsm_portal_invites(token);
create index if not exists idx_fsm_portal_invites_customer on fsm_portal_invites(customer_id);

alter table fsm_portal_users enable row level security;
alter table fsm_portal_invites enable row level security;

drop policy if exists fsm_portal_users_tenant_isolation on fsm_portal_users;
create policy fsm_portal_users_tenant_isolation on fsm_portal_users
    using (company_id = (auth.jwt() ->> 'company_id')::uuid)
    with check (company_id = (auth.jwt() ->> 'company_id')::uuid);

drop policy if exists fsm_portal_invites_tenant_isolation on fsm_portal_invites;
create policy fsm_portal_invites_tenant_isolation on fsm_portal_invites
    using (company_id = (auth.jwt() ->> 'company_id')::uuid)
    with check (company_id = (auth.jwt() ->> 'company_id')::uuid);
