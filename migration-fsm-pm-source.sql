-- Allow tickets auto-generated from PM schedules to carry source = 'pm_schedule'.
-- Without this, fsm_tickets_source_check (portal/email/phone/whatsapp/api/manual)
-- rejects them (error 23514) and the generator falls back to source = 'manual'.
-- Safe to re-run.

alter table fsm_tickets drop constraint if exists fsm_tickets_source_check;
alter table fsm_tickets add constraint fsm_tickets_source_check
  check (source is null or source in
    ('portal','email','phone','whatsapp','api','manual','pm_schedule'));
