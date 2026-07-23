-- ─────────────────────────────────────────────────────────────────────────────
-- SUB-001 remediation: plan-limit plumbing only (per product decision — no
-- real limits are being turned on yet). Adds a place to define per-company
-- caps (e.g. {"max_quotes_per_month": 50, "max_users": 10}) that
-- api/quotes.py's create_quote() and api/auth.py's accept_invite() already
-- check for. Absent/empty means unlimited, which is every company today.
--
-- Safe to re-run (idempotent). Run once per environment (staging, production).
-- ─────────────────────────────────────────────────────────────────────────────
alter table companies add column if not exists plan_limits jsonb default '{}'::jsonb;

-- Verify.
select id, name, plan, plan_limits from companies order by created_at;
