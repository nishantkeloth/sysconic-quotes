-- Proposals: client site photos (an uploaded photo can be re-rendered with the proposed equipment in it).
-- Additive and safe to re-run.
alter table proposal_images add column if not exists kind text not null default 'generated';
alter table proposal_images add column if not exists source_path text;
