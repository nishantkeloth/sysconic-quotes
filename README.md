# Sysconic Quote Manager — SaaS

## Architecture
- **Frontend**: Single HTML file, Vanilla JS
- **Backend**: Python/Flask on Vercel serverless
- **Database**: Supabase PostgreSQL
- **Auth**: JWT (bcrypt passwords)
- **Deploy**: Vercel

## Setup

### 1. Run the SQL schema in Supabase
Go to Supabase → SQL Editor → paste contents of `schema.sql` → Run

### 2. Set environment variables in Vercel
```
SUPABASE_URL=https://idcvzoqcmgwgqcpnpcuz.supabase.co
SUPABASE_SERVICE_KEY=your_service_key
JWT_SECRET=your_long_random_secret
```

### 3. Deploy
```bash
vercel --prod
```

## Multi-tenant SaaS model
- Each company gets their own isolated data via `company_id`
- Row-level isolation enforced in all API queries
- Roles: `admin` (full access, invite) and `user` (create/edit own quotes)
- Ready to add billing/plans via `companies.plan` column

## API endpoints
| Method | Path | Description |
|--------|------|-------------|
| POST | /api/auth/register | Create company + admin |
| POST | /api/auth/login | Login |
| GET | /api/auth/me | Current user |
| POST | /api/auth/invite | Invite team member (admin) |
| POST | /api/auth/accept-invite | Accept invite |
| GET | /api/auth/team | List team members |
| GET | /api/quotes | List all quotes |
| POST | /api/quotes | Create quote |
| GET | /api/quotes/:id | Get quote |
| PUT | /api/quotes/:id | Update quote |
| DELETE | /api/quotes/:id | Delete quote |
| POST | /api/quotes/:id/duplicate | Duplicate quote |
