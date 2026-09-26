-- Quote-to-invoice reconciliation (Business Performance Dashboard, Phase 1)
--
-- QTCal quotes/projects and Zoho invoices have never had any linkage besides
-- zoho_invoices.project_id (set only by the per-project Project Performance
-- sync, never by the org-wide Zoho Sales sync -- see api/integrations.py's
-- fetch_all_invoices()/sync_sales()). This table records best-effort
-- suggested matches (by customer-name overlap + amount closeness + date
-- proximity -- see _suggest_quote_invoice_matches in api/quotes.py) for an
-- admin to confirm or reject, so "awarded-but-uninvoiced" never guesses: an
-- invoice only counts against a quote once a human has confirmed the link.
--
-- Many-to-many by design -- a quote can be covered by multiple partial
-- invoices, and (before confirmation) one invoice may be *suggested* against
-- more than one quote. To prevent double-counting revenue, application code
-- (see confirm_quote_invoice_match()) auto-rejects any other pending
-- suggestion for the same invoice once one is confirmed; an invoice is never
-- allowed more than one CONFIRMED link.
create table if not exists quote_invoice_matches (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  quote_id uuid references quotes(id) on delete cascade not null,
  zoho_invoice_id uuid references zoho_invoices(id) on delete cascade not null,
  match_score numeric,
  -- What drove the score -- e.g. {"customer_overlap": 0.8, "amount_ratio": 0.95,
  -- "days_apart": 12} -- shown to the admin reviewing a suggestion so "why
  -- was this suggested" is never a mystery.
  match_basis jsonb default '{}'::jsonb,
  status text not null default 'suggested' check (status in ('suggested','confirmed','rejected')),
  created_at timestamptz default now(),
  confirmed_by uuid references users(id),
  confirmed_at timestamptz,
  unique(quote_id, zoho_invoice_id)
);

create index if not exists idx_qim_company_status on quote_invoice_matches(company_id, status);
create index if not exists idx_qim_quote on quote_invoice_matches(quote_id);
create index if not exists idx_qim_invoice on quote_invoice_matches(zoho_invoice_id);

alter table quote_invoice_matches enable row level security;
drop policy if exists tenant_isolation on quote_invoice_matches;
create policy tenant_isolation on quote_invoice_matches
  using (company_id = (auth.jwt() ->> 'company_id')::uuid);
-- Same caveat as migration-project-performance.sql: every api/*.py route
-- uses the Supabase service-role key (this app's own JWT, not Supabase
-- Auth), which bypasses RLS unconditionally. This policy is defense-in-depth
-- for a future lower-privilege code path, not a live gate on today's routes.
