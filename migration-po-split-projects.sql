-- Zoho lets each Purchase Order *line item* be assigned to a different
-- project (line item -> More -> Project), confirmed in Zoho's own API docs
-- (line_items[].project_id / .project_name). One PO can therefore span
-- multiple projects in this app.
--
-- The old unique(company_id, zoho_purchase_order_id) constraint assumed a
-- PO belongs to exactly one project: syncing project A would write a row
-- for the PO, and syncing project B (if it shared that PO) would upsert
-- straight over it on the same conflict key, silently discarding project
-- A's share. Widening the constraint to include project_id lets each
-- project keep its own scoped row for a shared PO.

alter table zoho_purchase_orders drop constraint if exists zoho_purchase_orders_company_id_zoho_purchase_order_id_key;
alter table zoho_purchase_orders add constraint zoho_purchase_orders_company_project_po_key
  unique (company_id, zoho_purchase_order_id, project_id);
