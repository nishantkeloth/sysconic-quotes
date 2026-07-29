-- Adds CC support to the "Email quote to customer" send log.
-- Run once in Supabase → SQL Editor. Additive / IF NOT EXISTS — safe to re-run.

alter table quote_emails add column if not exists sent_cc text;
