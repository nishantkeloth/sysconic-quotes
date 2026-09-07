"""
One-time data migration: copies existing rows from the standalone sysconic-pv
app's `vouchers` table into this app's new `vouchers` / `voucher_approvals`
tables (see migration-add-vouchers.sql -- run that migration first).

Unlike scripts/resign_storage_urls.py, this reads from a SECOND database (the
old PV app's Postgres, not this repo's Supabase project), so it needs that
connection string on top of the usual SUPABASE_URL / SUPABASE_SERVICE_KEY.
PV's DATABASE_URL isn't committed anywhere (it's a Vercel dashboard env var
on the sysconic-pv project, not in its local .env) -- pull it yourself with:

    cd path/to/sysconic-pv
    vercel env pull .env.vercel --environment=production
    # then read the DATABASE_URL line out of .env.vercel

and pass it to this script as PV_DATABASE_URL, either exported or via a
.env.pv file in this repo root (gitignored -- do not commit it).

What this does, per PV voucher row:
  - Resolves PV's free-text `project` field against this app's `projects.name`
    (case-insensitive exact match). No match -> stored in
    project_name_freeform instead of forcing a bad link.
  - Resolves `submitted_by` (a free-text name in PV) against this app's
    `users.name` the same way. No match -> left null.
  - Inserts one `vouchers` row.
  - Explodes PV's rajesh_status/ajith_status/nishant_status columns into up
    to three `voucher_approvals` rows, matched to real user accounts by
    email via --approver-emails (see below) in that fixed order.

Usage:
    pip install psycopg2-binary supabase python-dotenv --break-system-packages   # if not already installed

    # 1. Dry run first -- prints exactly what would be inserted, writes nothing.
    PV_DATABASE_URL="postgres://..." python scripts/migrate_pv_vouchers.py \
        --approver-emails rajesh@sysconic.com,ajith@sysconic.com,nishant.keloth@gmail.com

    # 2. Once the dry run output looks right, actually write:
    PV_DATABASE_URL="postgres://..." python scripts/migrate_pv_vouchers.py \
        --approver-emails rajesh@sysconic.com,ajith@sysconic.com,nishant.keloth@gmail.com --apply

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment or a
.env.local file in the repo root (same as every other script here).

Take a Supabase backup/point-in-time snapshot of this project before running
with --apply -- this is a one-shot insert, not designed to be re-run safely
against a target that already has the migrated rows (it does not de-dupe).
"""
import os
import sys
import argparse
from pathlib import Path

from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
from supabase import create_client

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env.local")
load_dotenv(REPO_ROOT / ".env.pv")  # optional, gitignored -- for PV_DATABASE_URL

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
PV_DATABASE_URL = os.environ.get("PV_DATABASE_URL")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually write. Without this flag, only prints what would happen.")
    ap.add_argument("--company-slug", default=None, help="Slug of the companies row to insert everything under. If omitted, auto-picks the only non-internal company (fails if there's more than one).")
    ap.add_argument("--approver-emails", required=True, help="Comma-separated emails in this exact order: rajesh,ajith,nishant -- matched positionally to PV's rajesh_status/ajith_status/nishant_status columns.")
    args = ap.parse_args()

    if not (SUPABASE_URL and SUPABASE_KEY):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY (check .env.local)")
    if not PV_DATABASE_URL:
        sys.exit("Missing PV_DATABASE_URL -- see the docstring for how to pull it from Vercel")

    approver_emails = [e.strip() for e in args.approver_emails.split(",") if e.strip()]
    if len(approver_emails) != 3:
        sys.exit("--approver-emails must list exactly 3 emails, in rajesh,ajith,nishant order")

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    company_id = args.company_slug
    if not args.company_slug:
        companies = sb.table("companies").select("id,name,slug").eq("is_internal", False).execute().data
        if len(companies) != 1:
            names = ", ".join(f"{c['name']} ({c['slug']})" for c in companies)
            sys.exit(f"Found {len(companies)} non-internal companies ({names or 'none'}) -- pass --company-slug explicitly.")
        company_id = companies[0]["id"]
        print(f"Using company: {companies[0]['name']} ({companies[0]['slug']}) -> {company_id}")
    else:
        row = sb.table("companies").select("id,name").eq("slug", args.company_slug).execute().data
        if not row:
            sys.exit(f"No company with slug '{args.company_slug}'")
        company_id = row[0]["id"]

    users = sb.table("users").select("id,email,name").eq("company_id", company_id).execute().data
    users_by_email = {u["email"].lower(): u for u in users}
    approver_users = []
    for e in approver_emails:
        u = users_by_email.get(e.lower())
        if not u:
            sys.exit(f"No user with email {e} found in this company -- create their account first.")
        approver_users.append(u)
    print("Approvers (in order):", ", ".join(f"{u['name']} <{u['email']}>" for u in approver_users))

    projects = sb.table("projects").select("id,name").eq("company_id", company_id).execute().data
    projects_by_name = {p["name"].strip().lower(): p["id"] for p in projects}

    pv_conn = psycopg2.connect(PV_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    with pv_conn, pv_conn.cursor() as cur:
        cur.execute("SELECT * FROM vouchers ORDER BY id")
        pv_vouchers = cur.fetchall()
    print(f"\nFound {len(pv_vouchers)} vouchers in PV.\n")

    inserted, project_matches, project_misses = 0, 0, 0
    for v in pv_vouchers:
        project_name = (v.get("project") or "").strip()
        project_id = projects_by_name.get(project_name.lower())
        if project_id:
            project_matches += 1
        elif project_name:
            project_misses += 1

        new_row = {
            "company_id": company_id,
            "payee": v.get("payee"),
            "amount": v.get("amount"),
            "currency": v.get("currency") or "AED",
            "payment_method": v.get("payment_method") or "Bank Transfer",
            "category": v.get("category"),
            "project_id": project_id,
            "project_name_freeform": None if project_id else (project_name or None),
            "invoice_no": v.get("invoice_no"),
            "due_date": v.get("due_date") or None,  # PV stored this as free text; verify format before --apply
            "remarks": v.get("remarks"),
            "submitted_by": None,  # PV only stored a free-text name (v.get('submitted_by')) -- no reliable user match; left null
            "status": v.get("status") or "pending",
        }
        approval_statuses = [v.get("rajesh_status"), v.get("ajith_status"), v.get("nishant_status")]

        print(f"PV #{v.get('id')}: {new_row['payee']} — {new_row['currency']} {new_row['amount']} "
              f"[project: {'matched -> ' + project_name if project_id else ('freeform: ' + project_name if project_name else '(none)')}] "
              f"approvals: {approval_statuses}")

        if args.apply:
            inserted_row = sb.table("vouchers").insert(new_row).execute().data[0]
            for u, status in zip(approver_users, approval_statuses):
                sb.table("voucher_approvals").insert({
                    "voucher_id": inserted_row["id"],
                    "approver_user_id": u["id"],
                    "status": status or "pending",
                }).execute()
            inserted += 1

    print(f"\nProjects: {project_matches} matched, {project_misses} left as free text.")
    if args.apply:
        print(f"Inserted {inserted} vouchers with their approvals.")
    else:
        print("\nDry run only -- nothing written. Re-run with --apply once this looks right.")


if __name__ == "__main__":
    main()
