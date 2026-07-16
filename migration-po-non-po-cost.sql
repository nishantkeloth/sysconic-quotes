-- Splits actual_cost into two visible components:
--   po_based_actual_cost     -- billed amount that traces back to a Purchase
--                                Order (zoho_bills.zoho_purchase_order_id is set)
--   non_po_based_actual_cost -- billed amount with no PO behind it, plus all
--                                direct project expenses (zoho_expenses)
-- po_based_actual_cost + non_po_based_actual_cost == actual_cost, always.

alter table projects add column if not exists po_based_actual_cost numeric default 0;
alter table projects add column if not exists non_po_based_actual_cost numeric default 0;
