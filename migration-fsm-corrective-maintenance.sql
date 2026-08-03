-- ─────────────────────────────────────────────────────────────────────────────
-- FSM Module 9 — Corrective Maintenance
--
-- Adds breakdown-tracking fields directly onto fsm_tickets rather than a
-- separate table, since a corrective-maintenance record IS a ticket (one
-- of ticket_type in ('incident','corrective_maintenance')) — these are
-- just extra fields captured as the engineer diagnoses/resolves it.
--
-- is_repeat_failure is computed automatically at ticket creation time by
-- api/fsm_tickets.py: true if the same asset already had another
-- incident/corrective_maintenance ticket resolved or closed in the prior
-- 90 days. Stored (not computed on every read) so it reflects what was
-- true at the time the ticket was raised.
--
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

alter table fsm_tickets add column if not exists failure_category text;
alter table fsm_tickets add column if not exists root_cause text;
alter table fsm_tickets add column if not exists corrective_action text;
alter table fsm_tickets add column if not exists temporary_fix text;
alter table fsm_tickets add column if not exists permanent_fix text;
alter table fsm_tickets add column if not exists downtime_minutes integer;
alter table fsm_tickets add column if not exists is_repeat_failure boolean not null default false;

create index if not exists idx_fsm_tickets_asset_type on fsm_tickets(company_id, asset_id, ticket_type);

-- Verify.
select column_name, data_type
from information_schema.columns
where table_name = 'fsm_tickets'
order by ordinal_position;
