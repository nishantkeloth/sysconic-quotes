-- ─────────────────────────────────────────────────────────────────────────────
-- FSM Module 11 — SLA Management
--
-- SLA deadlines are computed once, at ticket creation, from whichever
-- contract applies (site-specific contract first, else a customer-wide
-- contract with no site_id) — see _apply_sla() in api/fsm_tickets.py.
-- Deliberately simple calendar-hour math (due_at = created_at + N hours),
-- NOT working-hours/holiday-calendar aware yet — the contract has those
-- fields captured for reference, but a full business-calendar SLA engine
-- is a larger feature than Phase 1 needs. Response/arrival/resolution/
-- closure are stamped automatically as the ticket's status changes.
--
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

alter table fsm_tickets add column if not exists contract_id uuid references fsm_contracts(id);
alter table fsm_tickets add column if not exists sla_response_due_at timestamptz;
alter table fsm_tickets add column if not exists sla_resolution_due_at timestamptz;
alter table fsm_tickets add column if not exists first_response_at timestamptz;
alter table fsm_tickets add column if not exists arrived_at timestamptz;
alter table fsm_tickets add column if not exists resolved_at timestamptz;
alter table fsm_tickets add column if not exists closed_at timestamptz;

-- Verify.
select column_name, data_type
from information_schema.columns
where table_name = 'fsm_tickets'
order by ordinal_position;
