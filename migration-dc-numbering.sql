-- Configurable Delivery Challan reference numbers (DC-2026-0001 style),
-- same admin-editable pattern as migration-quote-numbering.sql.
--
-- Run this once in Supabase → SQL Editor. Additive / idempotent — safe to
-- re-run.
--
-- Design:
--   * dc_numbering_settings — one row per company: prefix + zero-padding
--     width. Admin-editable via the Company Profile "Delivery Challan
--     numbering" panel. No row needed for a company to work — defaults
--     ('DC', 4) are applied in application code when the row is missing,
--     matching the format already in use (DC-2026-0001).
--   * The running number itself keeps using the existing
--     delivery_challan_counters table / dc_next_challan_number() function
--     from migration-delivery-challans.sql — nothing changes there, this
--     migration only adds the format settings on top. An admin overriding
--     the "next number" for a year writes directly to
--     delivery_challan_counters.seq (= next_number - 1, since the function
--     increments-then-returns).
--
-- Existing challans are untouched — only the number FORMAT for new challans
-- changes if an admin edits prefix/padding; already-issued challan_numbers
-- are never rewritten.

create table if not exists dc_numbering_settings (
  company_id uuid references companies(id) on delete cascade primary key,
  prefix text not null default 'DC',
  padding int not null default 4,
  updated_at timestamptz default now(),
  updated_by uuid references users(id) on delete set null
);

alter table dc_numbering_settings enable row level security;
drop policy if exists tenant_isolation on dc_numbering_settings;
create policy tenant_isolation on dc_numbering_settings
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- Verify:
-- select * from dc_numbering_settings;
-- select * from delivery_challan_counters order by company_id, year;
