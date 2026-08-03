-- ─────────────────────────────────────────────────────────────────────────────
-- FSM Module 16 — Notifications
--
-- fsm_notifications already existed from the original Phase 1 session
-- (id, company_id, ticket_id, recipient_email, event_type, status,
-- created_at, sent_at) but was never used — this adds contract_id so the
-- AMC-expiry check (api/fsm_notify.py) can tell "already notified about
-- THIS contract" apart from "already notified about some other contract",
-- and avoid re-emailing admins every day a contract sits inside the
-- expiry window.
--
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

alter table fsm_notifications add column if not exists contract_id uuid references fsm_contracts(id);
create index if not exists idx_fsm_notifications_contract on fsm_notifications(contract_id, event_type);
create index if not exists idx_fsm_notifications_ticket on fsm_notifications(ticket_id, event_type);

-- Verify.
select column_name, data_type
from information_schema.columns
where table_name = 'fsm_notifications'
order by ordinal_position;
