-- Lets an admin approve/reject a pending workflow_stage_approvals row on
-- behalf of whichever user it's actually assigned to (e.g. the admin is
-- covering for someone who's out, or just wants to unblock a document
-- without waiting). `responded_by_user_id` records who *actually* clicked
-- Approve/Reject when that's different from the assigned approver, so the
-- approval log can show "Approved by ADMIN" while still knowing whose slot
-- it was. NULL means the assigned approver responded themselves (the normal
-- case) -- nothing changes for existing rows.
-- Idempotent / safe to re-run, matching this repo's other migration-*.sql files.

alter table workflow_stage_approvals
  add column if not exists responded_by_user_id uuid references users(id);
