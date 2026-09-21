-- Adds org-wide "Zoho Sales" support to Insights: total invoiced/collected
-- plus a per-customer breakdown, sourced from every invoice in Zoho Books --
-- not just the ones already synced per tracked Project (Project Performance).
--
-- Reuses the existing zoho_invoices table instead of creating a parallel one:
-- project_id on that table is already nullable, and the sync in
-- api/integrations.py's fetch_all_invoices()/sync-sales endpoint never sets
-- project_id on the rows it upserts, so an invoice already linked to a
-- Project by the Project Performance sync keeps that link untouched, while a
-- plain sale with no project just gets project_id = NULL. Every existing
-- query against this table (api/projects.py, api/project_performance.py,
-- api/pp_sync.py) filters with .eq('project_id', project_id), so the new
-- NULL-project rows are invisible to those and can't affect them.
--
-- Only new columns needed: customer_name/customer_id, so the Insights
-- breakdown can group by customer without parsing the raw jsonb each time.
-- Idempotent / safe to re-run.
alter table zoho_invoices add column if not exists customer_name text;
alter table zoho_invoices add column if not exists customer_id text;
create index if not exists idx_zinv_customer on zoho_invoices(company_id, customer_name);
