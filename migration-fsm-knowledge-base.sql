-- FSM Module 15: Knowledge Base
-- Idempotent. New table only — no changes to existing FSM tables.

create table if not exists fsm_kb_articles (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id),
    title text not null,
    category text not null default 'article',
    content text not null default '',
    tags text[] not null default '{}',
    asset_category text,
    attachment_url text,
    is_published boolean not null default true,
    created_by uuid,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'fsm_kb_articles_category_check'
    ) then
        alter table fsm_kb_articles
            add constraint fsm_kb_articles_category_check
            check (category in ('article','video','manual','troubleshooting_guide','faq','known_error'));
    end if;
end $$;

create index if not exists idx_fsm_kb_articles_company on fsm_kb_articles(company_id);
create index if not exists idx_fsm_kb_articles_category on fsm_kb_articles(company_id, category);
create index if not exists idx_fsm_kb_articles_tags on fsm_kb_articles using gin(tags);

alter table fsm_kb_articles enable row level security;

drop policy if exists fsm_kb_articles_tenant_isolation on fsm_kb_articles;
create policy fsm_kb_articles_tenant_isolation on fsm_kb_articles
    using (company_id = (auth.jwt() ->> 'company_id')::uuid)
    with check (company_id = (auth.jwt() ->> 'company_id')::uuid);
