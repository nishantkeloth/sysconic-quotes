-- ─── Companies ────────────────────────────────────────────────────────────────
create table if not exists companies (
  id uuid default gen_random_uuid() primary key,
  name text not null,
  slug text unique not null,
  plan text default 'free',
  status text default 'active',
  -- Marks the single dedicated, non-customer company row that houses
  -- platform-admin (is_platform_admin=true) users. Since users.company_id is
  -- NOT NULL, platform staff still need *a* company row — this keeps them out
  -- of any real tenant's company_id instead. See migrate-internal-company.sql.
  is_internal boolean default false,
  created_at timestamptz default now()
);

-- ─── Users ────────────────────────────────────────────────────────────────────
create table if not exists users (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  email text unique not null,
  name text,
  password_hash text not null default '',
  role text default 'user' check (role in ('admin','user')),
  invited_by uuid references users(id),
  is_platform_admin boolean default false,
  -- The Microsoft 365 mailbox this user sends quote/review emails from —
  -- separate from their app login `email` (which may not be a real M365
  -- mailbox, e.g. a Gmail address used just to sign into the app). Falls
  -- back to `email` if unset; the user sets this themselves the first time
  -- they send an email.
  ms365_email text,
  -- Add-on capability layered on top of role (admin/user), not a separate
  -- role: whether this person is eligible to be picked as a quote reviewer.
  can_review boolean default false,
  -- Add-on capability: a plain User only sees quotes they created by
  -- default (see quotes list/get scoping) unless this is set, or they're
  -- admin. Assigned reviewers can still open the specific quote they were
  -- asked to review regardless of this flag.
  can_view_all_quotes boolean default false,
  created_at timestamptz default now()
);

-- ─── Invites ──────────────────────────────────────────────────────────────────
create table if not exists invites (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  email text not null,
  password_hash text not null default '',
  role text default 'user',
  token text unique not null,
  invited_by uuid references users(id),
  accepted boolean default false,
  expires_at timestamptz default now() + interval '7 days',
  created_at timestamptz default now(),
  -- Set at invite time so a new team member can start with reviewer /
  -- view-all-quotes access already granted, instead of an admin having to
  -- remember to flip it on afterward in Team & Settings. Copied onto the
  -- new `users` row when the invite is accepted.
  can_review boolean default false,
  can_view_all_quotes boolean default false
);

-- ─── Quotes ───────────────────────────────────────────────────────────────────
create table if not exists quotes (
  id uuid default gen_random_uuid() primary key,
  company_id uuid references companies(id) on delete cascade not null,
  created_by uuid references users(id),
  title text not null,
  customer text,
  status text default 'draft' check (status in ('draft','sent','awarded','lost')),
  currency text default 'AED',
  exchange_rate numeric default 1,
  quote_data jsonb default '[]',
  vendor_data jsonb default '[]',
  terms_data jsonb default '[]',
  total_sell numeric default 0,
  total_gp numeric default 0,
  margin numeric default 0,
  -- Internal review workflow (separate from the customer-facing `status`
  -- lifecycle above): 'none' | 'pending' | 'approved' | 'changes_requested'.
  review_status text default 'none' check (review_status in ('none','pending','approved','changes_requested')),
  current_version int default 0,
  -- Separate from `current_version` above (which tracks internal review
  -- submissions). This counts how many times the quote has actually been
  -- emailed to the customer — first successful send is "V1", shown next to
  -- the sent date in the activity log.
  customer_version int default 0,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- ─── Quote versions (snapshots taken each time a quote is submitted for
-- internal review, so past submissions stay viewable even after the live
-- quote_data keeps changing) ────────────────────────────────────────────────
create table if not exists quote_versions (
  id uuid default gen_random_uuid() primary key,
  quote_id uuid references quotes(id) on delete cascade not null,
  version_number int not null,
  title text,
  customer text,
  quote_data jsonb,
  terms_data jsonb,
  vendor_data jsonb,
  status text default 'pending' check (status in ('pending','approved','changes_requested')),
  submitted_by uuid references users(id),
  submitted_at timestamptz default now(),
  reviewed_by uuid references users(id),
  reviewed_at timestamptz,
  review_comment text,
  -- True when the final decision on this version was made by a Company
  -- Admin overriding/short-circuiting the assigned reviewers (force-approve,
  -- or cancelling the submission), rather than the reviewers themselves.
  admin_override boolean default false
);
create index if not exists idx_quote_versions_quote on quote_versions(quote_id);
create index if not exists idx_quote_versions_status on quote_versions(status);

-- ─── Quote version reviewers (the specific users the quote creator picked to
-- review a given submission — review is assigned, not open to the whole
-- company). quote_versions.status is the aggregate: 'changes_requested' if
-- any assigned reviewer requested changes, 'approved' once all of them have
-- approved, otherwise 'pending' ────────────────────────────────────────────
create table if not exists quote_version_reviewers (
  id uuid default gen_random_uuid() primary key,
  version_id uuid references quote_versions(id) on delete cascade not null,
  user_id uuid references users(id) not null,
  status text default 'pending' check (status in ('pending','approved','changes_requested')),
  comment text,
  decided_at timestamptz,
  created_at timestamptz default now(),
  unique(version_id, user_id)
);
create index if not exists idx_qvr_version on quote_version_reviewers(version_id);
create index if not exists idx_qvr_user on quote_version_reviewers(user_id);

-- ─── Quote emails (one row per customer send — who emailed this quote to
-- whom, when, and a full content snapshot at that moment, mirroring what
-- quote_versions does for review submissions. version_number matches
-- quotes.customer_version at the time of that send, so past sends stay
-- viewable/comparable even after the live quote_data keeps changing) ───────
create table if not exists quote_emails (
  id uuid default gen_random_uuid() primary key,
  quote_id uuid references quotes(id) on delete cascade not null,
  sent_by uuid references users(id),
  sent_to text not null,
  subject text,
  sent_at timestamptz default now(),
  version_number int,
  title text,
  customer text,
  currency text,
  exchange_rate numeric,
  quote_data jsonb,
  terms_data jsonb,
  vendor_data jsonb
);
create index if not exists idx_quote_emails_quote on quote_emails(quote_id);
create index if not exists idx_quote_emails_version on quote_emails(quote_id, version_number);

-- ─── Quote activity log (one row per notable action on a quote — created,
-- status changed, submitted/approved/rejected for review, admin overrides,
-- emailed to customer — shown as a timeline in the quote's Log tab) ────────
create table if not exists quote_activity (
  id uuid default gen_random_uuid() primary key,
  quote_id uuid references quotes(id) on delete cascade not null,
  action text not null,
  detail text,
  actor_id uuid references users(id),
  created_at timestamptz default now()
);
create index if not exists idx_quote_activity_quote on quote_activity(quote_id);

-- ─── Indexes ──────────────────────────────────────────────────────────────────
create index if not exists idx_quotes_company on quotes(company_id);
create index if not exists idx_users_company on users(company_id);
create index if not exists idx_users_email on users(email);

-- ─── Auto update updated_at ───────────────────────────────────────────────────
create or replace function update_updated_at()
returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

drop trigger if exists quotes_updated_at on quotes;
create trigger quotes_updated_at
before update on quotes
for each row execute function update_updated_at();
