-- Daily payables reminder (Insights "Payables" section)
--
-- The Vendor payment tracker is manually entered per quote (Quote editor ->
-- Vendors tab) -- easy to lose track of once there are several awarded
-- quotes each with their own vendor payment dates. api/quotes.py's
-- run_payable_reminders() (a daily cron, see vercel.json) scans every
-- company's awarded quotes for payables that are overdue or due within a
-- few days and emails the company's admins a digest.
--
-- This table exists purely to stop that cron from sending more than one
-- digest per company per day if it's ever triggered twice (or retried) --
-- it deliberately does NOT dedupe per individual payable, since the whole
-- point is to keep reminding once a day for as long as something stays
-- unpaid, not to notify once and go silent.
create table if not exists payable_reminder_state (
  company_id uuid references companies(id) on delete cascade primary key,
  last_sent_date date,
  updated_at timestamptz not null default now()
);

alter table payable_reminder_state enable row level security;
drop policy if exists tenant_isolation on payable_reminder_state;
create policy tenant_isolation on payable_reminder_state
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);
-- Same caveat as every other migration in this app: api/*.py routes use the
-- Supabase service-role key and bypass RLS -- this is defense-in-depth, not
-- a live gate on today's routes.
