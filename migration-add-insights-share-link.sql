-- Insights public share link
--
-- Lets a company admin generate a single unguessable URL that opens the
-- Insights dashboard (desktop or the mobile insights-app.html PWA) with no
-- login at all -- for a phone home-screen shortcut or an office TV display
-- where typing a password every time isn't practical.
--
-- Deliberately NOT tied to any user's password/session: it's a long random
-- token, hashed at rest (SHA-256, same approach as the existing refresh-token
-- sessions table) so a database read alone can't recover a working link.
-- Regenerating or revoking immediately invalidates any previously shared
-- link. Anyone holding the raw link sees the full company-wide Insights
-- dashboard with no further gate -- this is an explicit, admin-only opt-in
-- that trades a real security boundary for convenience, never a default.

ALTER TABLE companies ADD COLUMN IF NOT EXISTS insights_share_token_hash TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS insights_share_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- Lets the public endpoint look a company up by hashed token in one indexed
-- query. Partial index (WHERE ... IS NOT NULL) since most companies will
-- never generate a link, and Postgres enforces uniqueness only among the
-- non-null values anyway.
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_insights_share_token_hash
  ON companies(insights_share_token_hash)
  WHERE insights_share_token_hash IS NOT NULL;
