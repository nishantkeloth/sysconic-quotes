-- ─────────────────────────────────────────────────────────────────────────────
-- FSM Module 13 — Billing
--
-- Deliberately scoped to billing CLASSIFICATION and TRACKING on the ticket
-- itself (what kind of billing applies, how much, has it been invoiced) —
-- NOT a parallel invoicing/PDF engine. The app already has a full
-- quote-to-PDF-to-Zoho pipeline for the sales side (api/quotes.py,
-- api/pdf.py, Zoho Books integration); a chargeable service ticket should
-- flow into THAT system via "Create Quote from Ticket" rather than
-- duplicating invoice generation here.
--
-- billing_type defaults automatically at ticket creation based on whichever
-- contract applies (see api/fsm_tickets.py _apply_sla, which already looks
-- up the contract for SLA — billing reuses the same lookup):
--   warranty contract        -> 'warranty'      (no charge)
--   amc / cmc / fully_comp   -> 'amc_included'  (no charge)
--   time_material/labour/parts -> 'time_material' (chargeable)
--   no contract at all       -> 'chargeable_visit' (chargeable)
--
-- Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

alter table fsm_tickets add column if not exists billing_type text
  check (billing_type in ('warranty','amc_included','chargeable_visit','time_material','fixed_price'));
alter table fsm_tickets add column if not exists billing_status text not null default 'not_billable'
  check (billing_status in ('not_billable','pending','quoted','invoiced','paid'));
alter table fsm_tickets add column if not exists billing_amount numeric;
alter table fsm_tickets add column if not exists billing_notes text;

-- Verify.
select column_name, data_type
from information_schema.columns
where table_name = 'fsm_tickets'
order by ordinal_position;
