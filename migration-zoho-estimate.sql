-- Zoho Books Estimate (Quotation) replication, alongside the existing Zoho
-- Project creation. Run once in Supabase → SQL Editor. Additive / IF NOT
-- EXISTS -- safe to re-run.
--
-- Mirrors the existing zoho_project_id/zoho_project_no pattern on both
-- tables so a second click on "Create Project & Quotation in Zoho" can
-- detect an estimate was already created and skip re-creating it (same
-- idempotency guarantee the project link already has).

alter table projects add column if not exists zoho_estimate_id text;
alter table quotes add column if not exists zoho_estimate_id text;
