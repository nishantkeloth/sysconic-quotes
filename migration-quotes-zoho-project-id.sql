-- Adds a direct zoho_project_id reference on the quotes table itself, so the
-- Zoho project link is visible right on the quote (not only on the linked
-- projects row). Populated by api/integrations.py's zoho_create_project()
-- the moment a project gets auto-linked to Zoho from the "Create Project"
-- button flow.

alter table quotes add column if not exists zoho_project_id text;

-- Backfill: any quote whose linked project already has a zoho_project_id
-- (e.g. linked earlier via the old Projects-page "+ Zoho" button) should
-- reflect that on the quote too.
update quotes q
set zoho_project_id = p.zoho_project_id
from projects p
where q.project_id = p.id
  and p.zoho_project_id is not null
  and q.zoho_project_id is null;
