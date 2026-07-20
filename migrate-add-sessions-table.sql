-- ─────────────────────────────────────────────────────────────────────────────
-- AUTH-002 remediation: server-side, revocable sessions backing a short-lived
-- access token + long-lived refresh token pair, replacing the previous
-- 30-day JWT with no server-side revocation.
--
-- Only the refresh token touches this table. The access token (short-lived
-- JWT, unchanged mechanism otherwise) is still verified purely by signature
-- in every api/*.py file -- no DB lookup on every request, so there's no
-- performance regression on the hot path.
--
-- refresh_token_hash stores a SHA-256 hash of the actual refresh token, never
-- the raw value -- same reasoning as password_hash: a DB read/leak should
-- not hand out valid, usable session tokens.
--
-- Safe to re-run (idempotent). Run once per environment (staging, production).
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists sessions (
  id uuid default gen_random_uuid() primary key,
  user_id uuid references users(id) on delete cascade not null,
  refresh_token_hash text not null unique,
  created_at timestamptz default now(),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  user_agent text
);
create index if not exists idx_sessions_user on sessions(user_id);
create index if not exists idx_sessions_hash on sessions(refresh_token_hash);

alter table sessions enable row level security;
drop policy if exists tenant_isolation on sessions;
create policy tenant_isolation on sessions
  using (user_id in (select id from users where company_id = (auth.jwt() ->> 'company_id')::uuid));

-- Verify.
select column_name, data_type
from information_schema.columns
where table_name = 'sessions'
order by ordinal_position;
