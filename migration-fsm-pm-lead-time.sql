-- PM visit tickets are created ahead of the due date so they show on the
-- Schedule board in advance. lead_days = how many days before the due date the
-- ticket + work order are generated (default 14, per schedule).
-- Safe to re-run.

alter table fsm_pm_schedules add column if not exists lead_days integer not null default 14;
