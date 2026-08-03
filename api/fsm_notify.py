"""
api/fsm_notify.py — FSM Module 16: Notifications (scheduled checks)

Two of the PRS notification triggers ('SLA Warning', 'AMC Expiry') aren't
tied to a user action — they need to be discovered by scanning. This file
is the cron entry point for that, run daily by Vercel (see vercel.json
'crons'), same CRON_SECRET pattern as api/integrations.py's run-auto-sync
and api/fsm_pm.py's run-auto-generate.

Ticket-created / engineer-assigned / work-completed notifications are
triggered directly from the relevant action in api/fsm_tickets.py instead —
those already know the right moment to fire, no scanning needed.

Email only (see api/fsm_tickets.py's _notify for why — no SMS/WhatsApp/push
provider wired up yet).
"""

from flask import Flask, request, jsonify
import os
import uuid
import requests
from datetime import datetime, timedelta

from api.auth import verify_token, get_sb, get_ms_token, SENDER_EMAIL

app = Flask(__name__)

CRON_SECRET = os.environ.get('CRON_SECRET')
SLA_WARNING_WINDOW_HOURS = 2
CONTRACT_EXPIRY_WINDOW_DAYS = 30


def _notify(sb, company_id, ticket_id, event_type, to_email, subject, html_body, contract_id=None):
    if not to_email:
        return
    entry = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "ticket_id": ticket_id,
        "contract_id": contract_id,
        "recipient_email": to_email,
        "event_type": event_type,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    }
    try:
        token = get_ms_token()
        if not token:
            entry["status"] = "failed"
        else:
            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": html_body},
                    "toRecipients": [{"emailAddress": {"address": to_email}}],
                    "from": {"emailAddress": {"address": SENDER_EMAIL}},
                }
            }
            r = requests.post(
                f"https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail",
                json=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            entry["status"] = "sent" if r.status_code == 202 else "failed"
    except Exception as e:
        print(f"FSM notification email error: {e}")
        entry["status"] = "failed"

    if entry["status"] == "sent":
        entry["sent_at"] = datetime.utcnow().isoformat()
    sb.table("fsm_notifications").insert(entry).execute()

    if ticket_id:
        sb.table("fsm_ticket_activity").insert({
            "id": str(uuid.uuid4()),
            "ticket_id": ticket_id,
            "company_id": company_id,
            "event_type": "notification_sent",
            "note": f"{event_type.replace('_',' ').title()} email to {to_email} — {entry['status']}",
            "metadata": {},
            "created_at": datetime.utcnow().isoformat(),
        }).execute()


def _already_notified(sb, company_id, ticket_id, event_type):
    existing = (
        sb.table("fsm_notifications")
        .select("id")
        .eq("company_id", company_id)
        .eq("ticket_id", ticket_id)
        .eq("event_type", event_type)
        .limit(1)
        .execute()
        .data
    )
    return bool(existing)


def _check_sla_warnings(sb, company_id):
    now = datetime.utcnow()
    window_end = (now + timedelta(hours=SLA_WARNING_WINDOW_HOURS)).isoformat()
    open_statuses = ["new", "acknowledged", "assigned", "accepted", "travelling",
                      "on_site", "diagnosis", "waiting_customer", "waiting_parts", "waiting_approval"]
    tickets = (
        sb.table("fsm_tickets")
        .select("id,ticket_number,subject,assigned_engineer_id,sla_resolution_due_at")
        .eq("company_id", company_id)
        .in_("status", open_statuses)
        .not_.is_("sla_resolution_due_at", "null")
        .lte("sla_resolution_due_at", window_end)
        .execute()
        .data or []
    )
    sent = 0
    for t in tickets:
        if _already_notified(sb, company_id, t["id"], "sla_warning"):
            continue
        if not t.get("assigned_engineer_id"):
            continue
        eng = sb.table("fsm_engineers").select("email").eq("id", t["assigned_engineer_id"]).single().execute().data
        if eng and eng.get("email"):
            _notify(sb, company_id, t["id"], "sla_warning", eng["email"],
                    f"SLA warning: {t['ticket_number']}",
                    f"<p>Ticket <strong>{t['ticket_number']}</strong> — {t['subject']} — is due for resolution by {t['sla_resolution_due_at']}.</p>")
            sent += 1
    return sent


def _check_contract_expiry(sb, company_id):
    today = datetime.utcnow().date()
    window_end = (today + timedelta(days=CONTRACT_EXPIRY_WINDOW_DAYS)).isoformat()
    contracts = (
        sb.table("fsm_contracts")
        .select("id,contract_number,contract_type,customer_id,end_date")
        .eq("company_id", company_id)
        .eq("is_active", True)
        .not_.is_("end_date", "null")
        .lte("end_date", window_end)
        .gte("end_date", today.isoformat())
        .execute()
        .data or []
    )
    admins = sb.table("users").select("email").eq("company_id", company_id).eq("role", "admin").execute().data or []
    admin_emails = [a["email"] for a in admins if a.get("email")]
    sent = 0
    for c in contracts:
        already = (
            sb.table("fsm_notifications")
            .select("id")
            .eq("company_id", company_id)
            .eq("event_type", "amc_expiry")
            .eq("contract_id", c["id"])
            .limit(1)
            .execute()
            .data
        )
        if already:
            continue
        for email in admin_emails:
            _notify(sb, company_id, None, "amc_expiry", email,
                    f"Contract expiring: {c.get('contract_number') or c['id'][:8]}",
                    f"<p>Contract <strong>{c.get('contract_number') or c['id']}</strong> ({c['contract_type']}) expires on {c['end_date']}.</p>",
                    contract_id=c["id"])
            sent += 1
    return sent


@app.route("/api/fsm_notify/run-checks", methods=["GET", "POST"])
def run_checks():
    auth = request.headers.get('Authorization', '')
    provided = auth[7:] if auth.startswith('Bearer ') else ''
    provided = provided or request.args.get('secret') or request.headers.get('X-Cron-Secret') or ''
    if not CRON_SECRET or provided != CRON_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    sb = get_sb()
    companies = sb.table('companies').select('id').execute().data or []
    results = []
    for co in companies:
        cid = co['id']
        sla_sent = _check_sla_warnings(sb, cid)
        expiry_sent = _check_contract_expiry(sb, cid)
        if sla_sent or expiry_sent:
            results.append({'company_id': cid, 'sla_warnings_sent': sla_sent, 'contract_expiry_sent': expiry_sent})
    return jsonify({'companies_processed': len(companies), 'results': results})
