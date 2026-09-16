-- Adds Reg-Cost / Sup-Disc default fields to the Product Master, mirroring
-- the new Reg-Cost + Sup-Disc% -> Net Cost model added to the quote editor's
-- item rows. default_cost continues to represent the product's net cost (as
-- it always has -- still auto-filled onto new quote lines and used by the
-- catalog dedupe/backfill logic); these two new columns are additive defaults
-- that let a quote line pulled from (or pushed back to) the catalog carry
-- forward the same Reg-Cost/Sup-Disc% breakdown instead of just a flat cost.
-- Idempotent / safe to re-run.
alter table products add column if not exists default_reg_cost numeric default 0;
alter table products add column if not exists default_supplier_disc numeric default 0;
