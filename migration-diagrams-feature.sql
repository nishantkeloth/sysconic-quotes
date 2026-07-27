-- AI System Diagram feature flag
-- Enables the 🗺 AI Diagram button (quote editor) and the "System Schematic"
-- proposal section, gated via companies.features → JWT claims → hasFeature('diagrams').
-- Trial rollout: Sysconic only. Enable for other tenants the same way when ready.
--
-- Run in the Supabase SQL editor. Users must log out/in (or refresh their
-- token) to pick up the new flag.

UPDATE companies
SET features = COALESCE(features, '{}'::jsonb) || '{"diagrams": true}'::jsonb
WHERE name ILIKE 'sysconic%'
  AND is_internal IS NOT TRUE;

-- Verify:
-- SELECT name, slug, features FROM companies ORDER BY name;
