-- ─── Companies ────────────────────────────────────────────────────────────────
create table if not exists companies (
  id uuid default gen_random_uuid() primary key,
  name text not null,
  slug text unique not null,
  plan text default 'free',
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
  created_at timestamptz default now()
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
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

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
