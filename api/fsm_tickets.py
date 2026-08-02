"""
api/fsm_tickets.py — FSM Phase 1: Tickets, Activity Timeline, Work Orders

Wired to match api/auth.py and the RBAC pattern in api/quotes.py.
Same gate as api/fsm_assets.py — single 'fsm' page_key.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import uuid
from datetime import datetime

from api.auth import verify_token, get_sb

app = Flask(__name__)

VALID_TICKET_TYPES = {
    "incident", "service_request", "preventive_maintenance", "corrective_maintenance",
    "inspection", "warranty", "installation_support", "change_request", "consultation",
}
VALID_PRIORITIES = {"low", "medium", "high", "critical"}
VALID_STATUSES = {
    "new", "acknowledged", "assigned", "accepted", "travelling", "on_site", "diagnosis",
    "waiting_customer", "waiting_parts", "waiting_approval", "resolved", "closed", "cancelled",
}


def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))


@app.before_request
def rbac_gate():
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
@app.route("/api/fsm_tickets", methods=["GET", "POST", "PATCH"])
def fsm_tickets_router():
    resource = request.args.get("resource")
    action = request.args.get("action")

    if resource == "ticket":
        if action == "create":
            return create_ticket()
        if action == "list":
            return list_tickets()
        if action == "get":
            return get_ticket()
        if action == "update_status":
            return update_ticket_status()
        if action == "assign":
            return assign_ticket()

    if resource == "activity":
        if action == "list":
            return list_activity()
        if action == "add_note":
            return add_activity_note()

    if resource == "work_order":
        if action == "create":
            return create_work_order()
        if action == "list":
            return list_work_orders()
        if action == "complete":
            return complete_work_order()

    return jsonify({"error": "unknown resource/action"}), 400


# ------------------------------------------------------------------
# Ticket numbering — atomic via fsm_next_ticket_number() Postgres function
# ------------------------------------------------------------------
def _next_ticket_number(sb, company_id):
    year = datetime.utcnow().year
    result = sb.rpc("fsm_next_ticket_number", {
        "p_company_id": company_id, "p_year": year
    }).execute()
    # NOTE: confirm the actual shape supabase-py returns for a scalar-returning
    # RPC in your version of the client — result.data may be an int directly,
    # or a list like [{"fsm_next_ticket_number": N}] depending on version.
    # Verify with a manual call before relying on this in production:
    #   print(result.data) after one test create_ticket() call.
    seq = result.data
    if isinstance(seq, list):
        seq = seq[0].get("fsm_next_ticket_number") if seq else 1
    return f"FSM-{year}-{seq:04d}"


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


# ------------------------------------------------------------------
# Tickets
# ------------------------------------------------------------------
def create_ticket():
    payload = request.get_json(force=True) or {}
    required = ["customer_id", "ticket_type", "subject"]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    if payload["ticket_type"] not in VALID_TICKET_TYPES:
        return jsonify({"error": f"invalid ticket_type: {payload['ticket_type']}"}), 400

    priority = payload.get("priority", "medium")
    if priority not in VALID_PRIORITIES:
        return jsonify({"error": f"invalid priority: {priority}"}), 400

    company_id = g.claims['company_id']
    actor_id = g.claims['user_id']
    sb = get_sb()

    ticket_id = str(uuid.uuid4())
    record = {
        "id": ticket_id,
        "company_id": company_id,
        "ticket_number": _next_ticket_number(sb, company_id),
        "site_id": payload.get("site_id"),
        "asset_id": payload.get("asset_id"),
        "customer_id": payload["customer_id"],
        "ticket_type": payload["ticket_type"],
        "priority": priority,
        "status": "new",
        "source": payload.get("source", "manual"),
        "subject": payload["subject"],
        "description": payload.get("description"),
        "created_at": datetime.utcnow().isoformat(),
    }

    result = sb.table("fsm_tickets").insert(record).execute()
    created = result.data[0]

    _log_activity(sb, ticket_id, company_id, "created", actor_id=actor_id, note="Ticket created")

    return jsonify(created), 201


def list_tickets():
    company_id = g.claims['company_id']
    status = request.args.get("status")
    engineer_id = request.args.get("engineer_id")
    site_id = request.args.get("site_id")

    sb = get_sb()
    query = sb.table("fsm_tickets").select("*").eq("company_id", company_id).eq("is_deleted", False)
    if status:
        query = query.eq("status", status)
    if engineer_id:
        query = query.eq("assigned_engineer_id", engineer_id)
    if site_id:
        query = query.eq("site_id", site_id)
    result = query.order("created_at", desc=True).execute()
    return jsonify(result.data)


def get_ticket():
    company_id = g.claims['company_id']
    ticket_id = request.args.get("id")
    if not ticket_id:
        return jsonify({"error": "id required"}), 400

    sb = get_sb()
    result = sb.table("fsm_tickets").select("*").eq("id", ticket_id).eq("company_id", company_id).single().execute()
    return jsonify(result.data)


def update_ticket_status():
    company_id = g.claims['company_id']
    actor_id = g.claims['user_id']
    ticket_id = request.args.get("id")
    if not ticket_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    new_status = payload.get("status")
    if new_status not in VALID_STATUSES:
        return jsonify({"error": f"invalid status: {new_status}"}), 400

    sb = get_sb()
    update = {"status": new_status, "updated_at": datetime.utcnow().isoformat()}
    result = sb.table("fsm_tickets").update(update).eq("id", ticket_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404

    _log_activity(
        sb, ticket_id, company_id, "status_change",
        actor_id=actor_id, note=f"Status changed to {new_status}",
    )

    return jsonify(result.data[0])


def assign_ticket():
    company_id = g.claims['company_id']
    actor_id = g.claims['user_id']
    ticket_id = request.args.get("id")
    payload = request.get_json(force=True) or {}
    engineer_id = payload.get("engineer_id")
    if not ticket_id or not engineer_id:
        return jsonify({"error": "id and engineer_id required"}), 400

    sb = get_sb()
    update = {
        "assigned_engineer_id": engineer_id,
        "status": "assigned",
        "updated_at": datetime.utcnow().isoformat(),
    }
    result = sb.table("fsm_tickets").update(update).eq("id", ticket_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404

    _log_activity(sb, ticket_id, company_id, "assigned", actor_id=actor_id, note=f"Assigned to engineer {engineer_id}")

    # TODO (Phase 1 notifications): enqueue into fsm_notifications here,
    # then wire to your existing Microsoft Graph email sender in auth.py
    # (see send_invite_email for the pattern to follow).

    return jsonify(result.data[0])


# ------------------------------------------------------------------
# Activity Timeline (read + manual notes only — audit trail is append-only,
# never update/delete existing entries)
# ------------------------------------------------------------------
def list_activity():
    company_id = g.claims['company_id']
    ticket_id = request.args.get("ticket_id")
    if not ticket_id:
        return jsonify({"error": "ticket_id required"}), 400

    sb = get_sb()
    result = (
        sb.table("fsm_ticket_activity")
        .select("*")
        .eq("ticket_id", ticket_id)
        .eq("company_id", company_id)
        .order("created_at")
        .execute()
    )
    return jsonify(result.data)


def add_activity_note():
    company_id = g.claims['company_id']
    actor_id = g.claims['user_id']
    ticket_id = request.args.get("ticket_id")
    payload = request.get_json(force=True) or {}
    note = payload.get("note")
    if not ticket_id or not note:
        return jsonify({"error": "ticket_id and note required"}), 400

    sb = get_sb()
    entry = _log_activity(sb, ticket_id, company_id, "note", actor_id=actor_id, note=note)
    return jsonify(entry), 201


# ------------------------------------------------------------------
# Work Orders
# ------------------------------------------------------------------
def create_work_order():
    company_id = g.claims['company_id']
    payload = request.get_json(force=True) or {}
    ticket_id = payload.get("ticket_id")
    if not ticket_id:
        return jsonify({"error": "ticket_id required"}), 400

    sb = get_sb()
    record = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "ticket_id": ticket_id,
        "engineer_id": payload.get("engineer_id"),
        "visit_date": payload.get("visit_date"),
        "expected_duration_mins": payload.get("expected_duration_mins"),
        "checklist": payload.get("checklist", []),
        "materials_required": payload.get("materials_required"),
        "tools_required": payload.get("tools_required"),
        "instructions": payload.get("instructions"),
        "attachments": payload.get("attachments", []),
        "created_at": datetime.utcnow().isoformat(),
    }

    result = sb.table("fsm_work_orders").insert(record).execute()
    return jsonify(result.data[0]), 201


def list_work_orders():
    company_id = g.claims['company_id']
    ticket_id = request.args.get("ticket_id")

    sb = get_sb()
    query = sb.table("fsm_work_orders").select("*").eq("company_id", company_id).eq("is_deleted", False)
    if ticket_id:
        query = query.eq("ticket_id", ticket_id)
    result = query.execute()
    return jsonify(result.data)


def complete_work_order():
    company_id = g.claims['company_id']
    wo_id = request.args.get("id")
    if not wo_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    sb = get_sb()

    update = {
        "completion_notes": payload.get("completion_notes"),
        "signature_url": payload.get("signature_url"),
        "updated_at": datetime.utcnow().isoformat(),
    }
    result = sb.table("fsm_work_orders").update(update).eq("id", wo_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404

    ticket_id = result.data[0]["ticket_id"]
    _log_activity(sb, ticket_id, company_id, "completed", note="Work order completed")

    return jsonify(result.data[0])
