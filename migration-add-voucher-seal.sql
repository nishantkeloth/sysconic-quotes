-- Company seal/stamp image, printed at the bottom of a Receipt Voucher PDF.
--
-- Deliberately a separate column from companies.logo_url: the letterhead
-- logo used on Quotation PDFs and the round rubber-stamp seal used on a
-- Receipt Voucher (see the user's own sample voucher) are different assets
-- in practice. Set via POST /api/auth/company-profile/voucher-seal
-- (api/auth.py upload_voucher_seal()), same signed-URL storage pattern as
-- the existing logo upload.
--
-- Safe to re-run (idempotent).

ALTER TABLE companies ADD COLUMN IF NOT EXISTS voucher_seal_url TEXT;
