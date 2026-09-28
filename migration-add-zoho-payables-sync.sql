-- Adds org-wide "Zoho Payables" support to Insights: total unpaid bills
-- split into current/overdue (mirroring Zoho Books' own Accounts Payable
-- dashboard widget), plus a "due within 10 days" cash-out view -- sourced
-- from every vendor bill in Zoho Books, not the manual per-quote Vendor
-- payment tracker.
--
-- Reuses the existing zoho_bills table (see migration-project-performance.sql)
-- instead of creating a parallel one: it already has project_id (nullable),
-- vendor_name, total, status, bill_date, synced_at. The org-wide sync in
-- api/integrations.py's fetch_all_bills()/sync-payables endpoint never sets
-- project_id on the rows it upserts, so a bill already linked to a tracked
-- Project by the per-project Financial Health sync (fetch_bills()) keeps
-- that link untouched, while a plain bill with no project just gets
-- project_id = NULL -- identical pattern to migration-add-zoho-sales-sync.sql
-- for zoho_invoices. Every existing query against zoho_bills
-- (api/projects.py, api/project_performance.py, api/pp_sync.py) filters
-- with .eq('project_id', project_id), so the new NULL-project rows are
-- invisible to those and can't affect them.
--
-- New columns: due_date (not needed by the per-project sync, but essential
-- here for the current/overdue/due-soon split) and balance (the per-project
-- sync only ever needed the header total, not what's still outstanding on
-- it). vendor_id is captured for a future "drill into this vendor" view.
-- Idempotent / safe to re-run.
alter table zoho_bills add column if not exists due_date date;
alter table zoho_bills add column if not exists balance numeric default 0;
alter table zoho_bills add column if not exists vendor_id text;
create index if not exists idx_zb_vendor on zoho_bills(company_id, vendor_name);
create index if not exists idx_zb_due on zoho_bills(company_id, due_date);
