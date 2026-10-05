-- Adds 'cancelled' as a valid quote status (draft/sent/awarded/lost/cancelled).
--
-- The original schema.sql declared the status check inline, so Postgres
-- auto-named it (normally quotes_status_check) -- but rather than assume the
-- name, this drops whichever CHECK constraint on quotes mentions both
-- 'status' and 'awarded' (review_status's constraint never mentions
-- 'awarded', so it is left alone), then re-adds it with the new value.
-- Idempotent / safe to re-run.
do $$
declare c text;
begin
  for c in
    select conname from pg_constraint
    where conrelid = 'quotes'::regclass and contype = 'c'
      and pg_get_constraintdef(oid) ilike '%status%'
      and pg_get_constraintdef(oid) ilike '%awarded%'
  loop
    execute format('alter table quotes drop constraint %I', c);
  end loop;
end $$;

alter table quotes drop constraint if exists quotes_status_check;
alter table quotes add constraint quotes_status_check
  check (status in ('draft','sent','awarded','lost','cancelled'));
