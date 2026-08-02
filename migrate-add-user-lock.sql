-- ─────────────────────────────────────────────────────────────────────────────
-- Company-Admin can lock a team member's account, blocking sign-in without
-- deleting the user or losing their quote history/attribution (created_by,
-- "Prepared by", activity log entries, etc. all stay intact -- locking is
-- reversible, deleting is not).
--
-- Locking immediately revokes that user's active sessions (see
-- api/auth.py: lock_team_member) so their refresh-token cookie stops
-- working right away. Their current short-lived access token (JWT, verified
-- purely by signature -- see migrate-add-sessions-table.sql) is not
-- server-revocable and will simply expire on its own shortly after, same
-- tradeoff already accepted for a suspended company.
--
-- Safe to re-run (idempotent). Run once per environment (staging, production).
-- ─────────────────────────────────────────────────────────────────────────────
alter table users add column if not exists is_locked boolean not null default false;
alter table users add column if not exists locked_at timestamptz;
alter table users add column if not exists locked_by uuid references users(id);

-- Verify.
select column_name, data_type
from information_schema.columns
where table_name = 'users' and column_name in ('is_locked','locked_at','locked_by')
order by ordinal_position;
