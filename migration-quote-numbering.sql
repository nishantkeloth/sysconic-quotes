-- Auto-generated quotation reference numbers (QT-2026-001 style).
--
-- Run this once in Supabase → SQL Editor (production, and staging once it
-- has its own database). Everything is IF NOT EXISTS / additive — safe to
-- re-run.
--
-- Design:
--   * quote_numbering_settings — one row per company: prefix + zero-padding
--     width. Admin-editable via the new Company Profile "Quotation
--     numbering" panel. No row needed for a company to work — defaults
--     ('QT', 3) are applied in application code when the row is missing.
--   * quote_number_sequences — one row per (company, year). next_number is
--     the number that will be handed out *next*. Rows are created lazily
--     (first quote of the year, or an admin pre-setting a starting number
--     for a future year) via the upsert inside get_next_quote_number().
--   * get_next_quote_number() is a single atomic UPSERT — safe under
--     concurrent quote creation without any explicit locking from
--     api/quotes.py, since Postgres serializes the row-level lock itself.
--
-- Existing quotes are untouched — quote_ref stays null/manual for anything
-- created before this ships; only new quotes get auto-generated refs.

create table if not exists quote_numbering_settings (
  company_id uuid references companies(id) on delete cascade primary key,
  prefix text not null default 'QT',
  padding int not null default 3,
  updated_at timestamptz default now(),
  updated_by uuid references users(id) on delete set null
);

create table if not exists quote_number_sequences (
  company_id uuid references companies(id) on delete cascade not null,
  year int not null,
  next_number int not null default 1,
  updated_at timestamptz default now(),
  primary key (company_id, year)
);

create or replace function get_next_quote_number(p_company_id uuid, p_year int)
returns int as $$
declare
  v_number int;
begin
  insert into quote_number_sequences (company_id, year, next_number)
  values (p_company_id, p_year, 2)
  on conflict (company_id, year)
  do update set next_number = quote_number_sequences.next_number + 1, updated_at = now()
  returning next_number - 1 into v_number;
  return v_number;
end;
$$ language plpgsql;

-- ─── Row-Level Security — same defense-in-depth pattern as the rest of the
-- app (see migration-project-performance.sql). Service-role key used by
-- every api/*.py bypasses this by design; every route still also filters by
-- company_id explicitly. ────────────────────────────────────────────────────
do $$
declare
  t text;
begin
  foreach t in array array['quote_numbering_settings','quote_number_sequences']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists tenant_isolation on %I', t);
    execute format(
      'create policy tenant_isolation on %I using (company_id = (auth.jwt() ->> ''company_id'')::uuid)',
      t
    );
  end loop;
end $$;

-- Verify:
-- select * from quote_numbering_settings;
-- select * from quote_number_sequences order by company_id, year;
-- select get_next_quote_number(id, 2026) from companies limit 1; -- test call using
--   a real company_id (company_id has an FK to companies, so a made-up uuid will
--   fail with a foreign-key violation -- that's expected, not a bug)
