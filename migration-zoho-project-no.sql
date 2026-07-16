-- What we've been calling "Zoho Project ID"/"Zoho #" everywhere in the UI was
-- actually Zoho's internal numeric project_id -- not the human-readable
-- project number staff actually use. That number lives in Zoho Books as a
-- custom field, cf_project_no, on the project record. This adds a separate
-- column for it: zoho_project_id stays as-is (still required for every Zoho
-- API call -- POs/bills/expenses/invoices are all fetched by internal
-- project_id, that can't change), zoho_project_no is purely the
-- human-readable number for display and for the "Sync Selected" testing
-- lookup.

alter table projects add column if not exists zoho_project_no text;
alter table quotes add column if not exists zoho_project_no text;
