-- Adds a real payment_type column to vouchers so a submission can be tagged
-- as tied to a specific Project, a General/Admin expense, or Other. The New
-- Payment Voucher form's "Payment Type" selector was previously visual-only
-- (nothing was saved); this makes it a real, persisted field the form and
-- API both read/write. Idempotent / safe to re-run.
alter table vouchers add column if not exists payment_type text default 'project';
alter table vouchers drop constraint if exists vouchers_payment_type_check;
alter table vouchers add constraint vouchers_payment_type_check
  check (payment_type in ('project','general','other'));
