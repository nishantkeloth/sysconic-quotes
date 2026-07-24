-- ─────────────────────────────────────────────────────────────────────────────
-- AUTH-003 remediation: DB-backed rate limiting for auth endpoints (login,
-- register, forgot-password, accept-invite). DB-backed rather than in-memory
-- because Vercel Python functions are stateless per invocation -- an
-- in-memory counter would reset on almost every request instead of actually
-- limiting anything.
--
-- No RLS policy is defined (deny-all-except-service-role by design): every
-- write/read to this table goes through the service-role key from
-- api/auth.py, and there is no legitimate reason for a tenant-scoped client
-- to read it directly.
--
-- Safe to re-run (idempotent). Run once per environment (staging, production).
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists auth_attempts (
  id uuid default gen_random_uuid() primary key,
  action text not null,
  identifier text not null,
  created_at timestamptz default now()
);
create index if not exists idx_auth_attempts_lookup on auth_attempts(action, identifier, created_at desc);

alter table auth_attempts enable row level security;

-- Verify.
select column_name, data_type
from information_schema.columns
where table_name = 'auth_attempts'
order by ordinal_position;
