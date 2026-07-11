-- ─────────────────────────────────────────────────────────────────────────────
-- Move platform-admin accounts off real tenant company_ids and onto a
-- dedicated, non-customer "internal" company. Run once per environment
-- (dev, staging, production) that has a platform-admin ("Global Admin") user.
-- Safe to re-run — every step is idempotent.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Add the flag that marks a company row as internal/non-customer.
alter table companies add column if not exists is_internal boolean default false;

-- 2. Create the one dedicated internal company, if it doesn't already exist.
insert into companies (name, slug, plan, status, is_internal)
select 'Sysconic Platform (Internal)', 'sysconic-platform-internal', 'internal', 'active', true
where not exists (select 1 from companies where is_internal = true);

-- 3. Move every existing platform-admin user onto that internal company.
update users
set company_id = (select id from companies where is_internal = true limit 1)
where is_platform_admin = true;

-- 4. Verify: should show the Global Admin (and any other platform admins)
--    now pointing at the internal company, not a real tenant's company_id.
select u.id, u.name, u.email, u.is_platform_admin, u.company_id, c.name as company_name, c.is_internal
from users u
join companies c on c.id = u.company_id
where u.is_platform_admin = true;
