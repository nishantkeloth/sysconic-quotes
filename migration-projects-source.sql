-- Tags where a `projects` row came from:
--   'quote'       -- created via the "Create Project" button on an awarded
--                     quote (has a commercial baseline frozen from that quote)
--   'zoho_import' -- pulled in directly from Zoho Books' project list (daily
--                     batch job or manual "Sync Now"), with no originating
--                     quote in this app, so it has no commercial baseline --
--                     purely actuals-tracked until one is added manually.
-- Existing rows all came from the quote flow, so they default/backfill to 'quote'.

alter table projects add column if not exists source text default 'quote';
update projects set source = 'quote' where source is null;
