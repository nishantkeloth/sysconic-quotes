"""
api/fsm_engineers.py — FSM Module 5: Engineer Management

Wired to match api/auth.py and the RBAC pattern in api/fsm_tickets.py /
api/fsm_assets.py — same gate, single 'fsm' page_key.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import uuid
import traceback
from datetime import datetime

from api.auth import verify_token, get_sb

app = Flask(__name__)

VALID_AVAILABILITY = {"available", "busy", "on_leave", "off_duty"}


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
@app.route("/api/fsm_engineers", methods=["GET", "POST", "PATCH"])
def fsm_engineers_router():
    resource = request.args.get("resource")
    action = request.args.get("action")

    if resource == "engineer":
        if action == "create":
            return create_engineer()
        if action == "list":
            return list_engineers()
        if action == "get":
            return get_engineer()
        if action == "update":
            return update_engineer()
        if action == "workload":
            return engineer_workload()

    return jsonify({"error": "unknown resource/action"}), 400


# ------------------------------------------------------------------
# Engineers
# ------------------------------------------------------------------
def create_engineer():
    payload = request.get_json(force=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    availability = payload.get("availability", "available")
    if availability not in VALID_AVAILABILITY:
        return jsonify({"error": f"invalid availability: {availability}"}), 400

    company_id = g.claims['company_id']
    record = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "user_id": payload.get("user_id"),
        "name": name,
        "email": payload.get("email"),
        "phone": payload.get("phone"),
        "skills": payload.get("skills", []),
        "certifications": payload.get("certifications", []),
        "territory": payload.get("territory"),
        "availability": availability,
        "is_active": True,
        "notes": payload.get("notes"),
        "created_at": datetime.utcnow().isoformat(),
    }

    sb = get_sb()
    try:
        result = sb.table("fsm_engineers").insert(record).execute()
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    return jsonify(result.data[0]), 201


def list_engineers():
    company_id = g.claims['company_id']
    active_only = request.args.get("active_only", "true").lower() != "false"
    availability = request.args.get("availability")

    sb = get_sb()
    query = sb.table("fsm_engineers").select("*").eq("company_id", company_id)
    if active_only:
        query = query.eq("is_active", True)
    if availability:
        query = query.eq("availability", availability)
    result = query.order("name").execute()
    return jsonify(result.data)


def get_engineer():
    company_id = g.claims['company_id']
    engineer_id = request.args.get("id")
    if not engineer_id:
        return jsonify({"error": "id required"}), 400

    sb = get_sb()
    result = sb.table("fsm_engineers").select("*").eq("id", engineer_id).eq("company_id", company_id).single().execute()
    return jsonify(result.data)


def update_engineer():
    company_id = g.claims['company_id']
    engineer_id = request.args.get("id")
    if not engineer_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    if "availability" in payload and payload["availability"] not in VALID_AVAILABILITY:
        return jsonify({"error": f"invalid availability: {payload['availability']}"}), 400

    allowed = {"name", "email", "phone", "skills", "certifications", "territory",
               "availability", "is_active", "notes", "user_id"}
    update = {k: v for k, v in payload.items() if k in allowed}
    update["updated_at"] = datetime.utcnow().isoformat()

    sb = get_sb()
    try:
        result = sb.table("fsm_engineers").update(update).eq("id", engineer_id).eq("company_id", company_id).execute()
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    if not result.data:
        return jsonify({"error": "not found"}), 404
    return jsonify(result.data[0])


def engineer_workload():
    """Open-ticket count per active engineer — used by the Engineers list to
    show current load at a glance (feeds the Module 6 scheduler later)."""
    company_id = g.claims['company_id']
    sb = get_sb()

    engineers = sb.table("fsm_engineers").select("id,name").eq("company_id", company_id).eq("is_active", True).execute().data or []
    open_statuses = ["new", "acknowledged", "assigned", "accepted", "travelling",
                      "on_site", "diagnosis", "waiting_customer", "waiting_parts", "waiting_approval"]
    tickets = (
        sb.table("fsm_tickets")
        .select("assigned_engineer_id")
        .eq("company_id", company_id)
        .eq("is_deleted", False)
        .in_("status", open_statuses)
        .execute()
        .data or []
    )
    counts = {}
    for t in tickets:
        eid = t.get("assigned_engineer_id")
        if eid:
            counts[eid] = counts.get(eid, 0) + 1

    return jsonify([{"id": e["id"], "name": e["name"], "open_tickets": counts.get(e["id"], 0)} for e in engineers])
