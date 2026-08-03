"""
api/fsm_pm.py — FSM Module 8: Preventive Maintenance

Wired to match api/auth.py and the RBAC pattern in the rest of the FSM API
files — same gate, single 'fsm' page_key.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import os
import uuid
import calendar
from datetime import datetime, date

from api.auth import verify_token, get_sb

app = Flask(__name__)

VALID_FREQUENCIES = {"monthly", "quarterly", "half_yearly", "yearly"}
FREQUENCY_MONTHS = {"monthly": 1, "quarterly": 3, "half_yearly": 6, "yearly": 12}

CRON_SECRET = os.environ.get('CRON_SECRET')


def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))


@app.before_request
def rbac_gate():
    # The cron endpoint has no logged-in user — it authenticates via
    # CRON_SECRET instead (same pattern as api/integrations.py's
    # run-auto-sync), so it's exempted from the JWT gate here.
    if request.path == '/api/fsm_pm/run-auto-generate':
        return None

    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'unauthorized'}), 401

    if not (claims.get('features') or {}).get('fsm_module'):
        return jsonify({'error': 'FSM module not enabled for this company'}), 403

    if not has_page_access(claims, 'fsm'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403

    g.claims = claims
    return None


# ------------------------------------------------------------------
# Router
# ------------------------------------------------------------------
@app.route("/api/fsm_pm", methods=["GET", "POST", "PATCH"])
def fsm_pm_router():
    resource = request.args.get("resource")
    action = request.args.get("action")

    if resource == "schedule":
        if action == "create":
            return create_schedule()
        if action == "list":
            return list_schedules()
        if action == "update":
            return update_schedule()
        if action == "generate_now":
            return generate_now()

    return jsonify({"error": "unknown resource/action"}), 400


# ------------------------------------------------------------------
# Shared helpers (duplicated per-file — Vercel's Python builder doesn't
# bundle sibling modules, same convention as api/quotes.py / api/integrations.py)
# ------------------------------------------------------------------
def _log_activity(sb, ticket_id, company_id, event_type, actor_id=None, note=None, metadata=None):
    entry = {
        "id": str(uuid.uuid4()),
        "ticket_id": ticket_id,
        "company_id": company_id,
        "event_type": event_type,
        "actor_id": actor_id,
        "note": note,
        "metadata": metadata or {},
        "created_at": datetime.utcnow().isoformat(),
    }
    sb.table("fsm_ticket_activity").insert(entry).execute()
    return entry


def _next_ticket_number(sb, company_id):
    year = datetime.utcnow().year
    result = sb.rpc("fsm_next_ticket_number", {"p_company_id": company_id, "p_year": year}).execute()
    seq = result.data
    if isinstance(seq, list):
        seq = seq[0].get("fsm_next_ticket_number") if seq else 1
    return f"FSM-{year}-{seq:04d}"


def _advance_date(d, frequency):
    """Add N months to a date, clamping the day to the target month's length
    (e.g. Jan 31 + 1 month -> Feb 28/29, not an invalid Feb 31)."""
    months = FREQUENCY_MONTHS[frequency]
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# ------------------------------------------------------------------
# PM Schedules
# ------------------------------------------------------------------
def create_schedule():
    payload = request.get_json(force=True) or {}
    required = ["site_id", "name", "frequency", "next_due_date"]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    if payload["frequency"] not in VALID_FREQUENCIES:
        return jsonify({"error": f"invalid frequency: {payload['frequency']}"}), 400

    company_id = g.claims['company_id']
    record = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "site_id": payload["site_id"],
        "asset_id": payload.get("asset_id"),
        "name": payload["name"],
        "frequency": payload["frequency"],
        "checklist": payload.get("checklist", []),
        "assigned_engineer_id": payload.get("assigned_engineer_id"),
        "next_due_date": payload["next_due_date"],
        "is_active": True,
        "created_at": datetime.utcnow().isoformat(),
    }

    sb = get_sb()
    result = sb.table("fsm_pm_schedules").insert(record).execute()
    return jsonify(result.data[0]), 201


def list_schedules():
    company_id = g.claims['company_id']
    site_id = request.args.get("site_id")

    sb = get_sb()
    query = sb.table("fsm_pm_schedules").select("*").eq("company_id", company_id)
    if site_id:
        query = query.eq("site_id", site_id)
    result = query.order("next_due_date").execute()
    return jsonify(result.data)


def update_schedule():
    company_id = g.claims['company_id']
    schedule_id = request.args.get("id")
    if not schedule_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    if "frequency" in payload and payload["frequency"] not in VALID_FREQUENCIES:
        return jsonify({"error": f"invalid frequency: {payload['frequency']}"}), 400

    allowed = {"name", "frequency", "checklist", "assigned_engineer_id",
               "next_due_date", "asset_id", "is_active"}
    update = {k: v for k, v in payload.items() if k in allowed}
    update["updated_at"] = datetime.utcnow().isoformat()

    sb = get_sb()
    result = sb.table("fsm_pm_schedules").update(update).eq("id", schedule_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404
    return jsonify(result.data[0])


def _generate_for_company(sb, company_id, today):
    """Create tickets + work orders for every active, due schedule in one
    company. Shared by the manual 'Generate Now' button (JWT-gated, current
    company only) and the cron endpoint (CRON_SECRET-gated, all companies)."""
    due = (
        sb.table("fsm_pm_schedules")
        .select("*")
        .eq("company_id", company_id)
        .eq("is_active", True)
        .lte("next_due_date", today.isoformat())
        .execute()
        .data or []
    )
    generated = []
    for sched in due:
        site = sb.table("fsm_sites").select("customer_id").eq("id", sched["site_id"]).single().execute().data
        if not site:
            continue

        ticket_id = str(uuid.uuid4())
        ticket = {
            "id": ticket_id,
            "company_id": company_id,
            "ticket_number": _next_ticket_number(sb, company_id),
            "site_id": sched["site_id"],
            "asset_id": sched.get("asset_id"),
            "customer_id": site["customer_id"],
            "ticket_type": "preventive_maintenance",
            "priority": "medium",
            "status": "new",
            "source": "pm_schedule",
            "subject": f"Preventive Maintenance — {sched['name']}",
            "description": f"Auto-generated from PM schedule ({sched['frequency'].replace('_',' ')}).",
            "assigned_engineer_id": sched.get("assigned_engineer_id"),
            "created_at": datetime.utcnow().isoformat(),
        }
        sb.table("fsm_tickets").insert(ticket).execute()
        _log_activity(sb, ticket_id, company_id, "created", note="Ticket auto-created from PM schedule")

        sb.table("fsm_work_orders").insert({
            "id": str(uuid.uuid4()),
            "company_id": company_id,
            "ticket_id": ticket_id,
            "engineer_id": sched.get("assigned_engineer_id"),
            "visit_date": sched["next_due_date"],
            "checklist": sched.get("checklist", []),
            "instructions": f"Preventive maintenance visit — {sched['name']}",
            "created_at": datetime.utcnow().isoformat(),
        }).execute()

        next_due = _advance_date(date.fromisoformat(sched["next_due_date"]), sched["frequency"])
        sb.table("fsm_pm_schedules").update({
            "next_due_date": next_due.isoformat(),
            "last_generated_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", sched["id"]).execute()

        generated.append({"schedule_id": sched["id"], "ticket_id": ticket_id, "ticket_number": ticket["ticket_number"]})

    return generated


def generate_now():
    company_id = g.claims['company_id']
    sb = get_sb()
    generated = _generate_for_company(sb, company_id, date.today())
    return jsonify({"generated": generated, "count": len(generated)})


@app.route("/api/fsm_pm/run-auto-generate", methods=["GET", "POST"])
def run_auto_generate():
    """Cron entry point — see vercel.json 'crons'. Not JWT-gated (no logged-in
    user for a cron trigger); authenticates via CRON_SECRET the same way
    api/integrations.py's run-auto-sync does."""
    auth = request.headers.get('Authorization', '')
    provided = auth[7:] if auth.startswith('Bearer ') else ''
    provided = provided or request.args.get('secret') or request.headers.get('X-Cron-Secret') or ''
    if not CRON_SECRET or provided != CRON_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    sb = get_sb()
    companies = sb.table('companies').select('id').execute().data or []
    today = date.today()
    results = []
    for co in companies:
        generated = _generate_for_company(sb, co['id'], today)
        if generated:
            results.append({'company_id': co['id'], 'generated': len(generated)})
    return jsonify({'companies_processed': len(companies), 'results': results})
