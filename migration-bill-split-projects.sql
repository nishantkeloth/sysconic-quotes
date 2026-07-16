-- Same rationale as migration-po-split-projects.sql: Zoho lets each *bill*
-- line item be assigned to its own project too (same mechanism as POs), so
-- one bill can span multiple projects. Widening the unique constraint the
-- same way so each project keeps its own scoped row for a shared bill
-- instead of the second project's sync silently overwriting the first's.

alter table zoho_bills drop constraint if exists zoho_bills_company_id_zoho_bill_id_key;
alter table zoho_bills add constraint zoho_bills_company_project_bill_key
  unique (company_id, zoho_bill_id, project_id);
