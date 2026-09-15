-- Professional Proposal Builder -- schema additions
-- Adds the reusable, company-level boilerplate content (About Us / Why Us,
-- Scope of Work split, warranty write-up, default commercial terms) plus a
-- per-quote JSONB column for the small bits that vary quote-to-quote
-- (Understanding of Requirements rows, an executive-summary override, a
-- cover image). Per-section concept image/blurb need NO migration -- sections
-- already live inside quotes.quote_data (JSONB) and get new keys purely in
-- the frontend, same as every other per-item field in that structure.
--
-- Gated behind the `proposal_builder` company feature flag (toggle it per
-- company from Platform admin -> Features, no SQL needed for that part).
-- Idempotent / safe to re-run, matching this repo's other migration-*.sql files.

-- 1) Company Profile: reusable proposal boilerplate (write once, reused on
--    every quote). Long-form fields (bullets/paragraphs) are stored as plain
--    newline-separated text, not JSONB arrays -- same convention already used
--    for FSM PM checklists in this app -- so the Company Profile UI can just
--    use a plain <textarea> (one bullet per line) with no add/remove-row UI.
alter table companies
  add column if not exists about_us_text text,           -- "Sysconic Technologies is a UAE-based systems integrator..."
  add column if not exists why_us_bullets text,           -- one bullet per line, "Label: description" per line
  add column if not exists scope_ours_bullets text,       -- one bullet per line, "Label: description" per line
  add column if not exists scope_client_bullets text,     -- one bullet per line, "Label: description" per line
  add column if not exists warranty_text text,            -- free-form paragraph(s), blank-line separated
  add column if not exists default_validity_days text,    -- e.g. "30"
  add column if not exists default_delivery_weeks text,   -- e.g. "4 - 6 weeks"
  add column if not exists default_payment_terms text;    -- e.g. "50% advance on order confirmation, 50% on completion and handover"

-- 2) Quotes: small per-quote proposal metadata that doesn't fit the existing
--    quote_data (which is an array of pricing OPTIONS, not a place for
--    document-level fields). Defaults to an empty object so existing rows
--    and the create_quote() insert path need no backfill.
alter table quotes
  add column if not exists proposal_data jsonb not null default '{}'::jsonb;

-- 3) Private storage bucket for per-section concept images (Professional
--    Proposal template). Same private-bucket + signed-URL pattern as
--    product-images / voucher-receipts / company-assets.
insert into storage.buckets (id, name, public)
values ('proposal-images', 'proposal-images', false)
on conflict (id) do nothing;

-- Verify:
-- select legal_name, about_us_text, why_us_bullets, scope_ours_bullets, scope_client_bullets, warranty_text, default_validity_days, default_delivery_weeks, default_payment_terms from companies limit 5;
-- select id, title, proposal_data from quotes limit 5;
