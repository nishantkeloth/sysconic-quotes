-- ─────────────────────────────────────────────────────────────────────────────
-- OBS-001 remediation: audit trail for platform-admin actions. Platform
-- admins act across every tenant, so unlike a normal company admin there's
-- no per-company owner who would otherwise notice a suspicious action (e.g.
-- an unexpected company suspension, or an invite into the wrong company).
-- This is an after-the-fact record, not a live gate.
--
-- No RLS policy is defined (deny-all-except-service-role by design), same
-- reasoning as auth_attempts: only api/platform.py's service-role client
-- ever needs to touch this table.
--
-- Safe to re-run (idempotent). Run once per environment (staging, production).
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists platform_audit_log (
  id uuid default gen_random_uuid() primary key,
  actor_user_id uuid references users(id),
  action text not null,
  target_company_id uuid references companies(id),
  detail jsonb,
  created_at timestamptz default now()
);
create index if not exists idx_platform_audit_log_actor on platform_audit_log(actor_user_id);
create index if not exists idx_platform_audit_log_target on platform_audit_log(target_company_id);

alter table platform_audit_log enable row level security;

-- Verify.
select column_name, data_type
from information_schema.columns
where table_name = 'platform_audit_log'
order by ordinal_position;
