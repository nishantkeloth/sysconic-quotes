-- Contract renewal history + coverage-gap guard.
--  * fsm_contracts.renewed_from_id / renewed_to_id / renewed_at link a contract to
--    the one that replaced it, so the history is visible.
--  * fsm_tickets.coverage_gap flags tickets raised while no AMC-type contract was
--    active (lapsed and not renewed), which would otherwise silently become billable.
-- Safe to re-run.

alter table fsm_contracts add column if not exists renewed_from_id uuid references fsm_contracts(id);
alter table fsm_contracts add column if not exists renewed_to_id   uuid references fsm_contracts(id);
alter table fsm_contracts add column if not exists renewed_at      timestamptz;

alter table fsm_tickets add column if not exists coverage_gap boolean not null default false;

create index if not exists idx_fsm_tickets_coverage_gap on fsm_tickets(company_id) where coverage_gap;
