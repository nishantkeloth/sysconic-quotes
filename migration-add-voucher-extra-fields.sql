-- Adds the extra fields requested for the New Voucher form: payee bank
-- details (for actually processing the transfer), a cost center/department,
-- a separate VAT amount, and a receipt/invoice attachment. Idempotent / safe
-- to re-run, matching this repo's other migration-*.sql files.

alter table vouchers
  add column if not exists bank_name text,
  add column if not exists bank_account text,       -- account number or IBAN
  add column if not exists cost_center text,
  add column if not exists vat_amount numeric,
  add column if not exists receipt_url text;         -- signed Supabase Storage URL, same pattern as products.image_url

-- Private storage bucket for uploaded receipts/invoices (images or PDF).
-- Same private-bucket + signed-URL pattern as product-images and
-- company-assets (see api/products.py / api/auth.py) -- not world-readable,
-- api/vouchers.py mints a long-lived signed URL at upload time instead.
insert into storage.buckets (id, name, public)
values ('voucher-receipts', 'voucher-receipts', false)
on conflict (id) do nothing;
