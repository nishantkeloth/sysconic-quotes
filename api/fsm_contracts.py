"""
api/fsm_contracts.py — FSM Module 10: Contract Management

Wired to match api/auth.py and the RBAC pattern in the rest of the FSM API
files — same gate, single 'fsm' page_key.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import uuid
from datetime import datetime

from api.auth import verify_token, get_sb

app = Flask(__name__)

VALID_CONTRACT_TYPES = {
    "warranty", "amc", "cmc", "time_material", "labour_only", "parts_only", "fully_comprehensive",
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
@app.route("/api/fsm_contracts", methods=["GET", "POST", "PATCH"])
def fsm_contracts_router():
    resource = request.args.get("resource")
    action = request.args.get("action")

    if resource == "contract":
        if action == "create":
            return create_contract()
        if action == "list":
            return list_contracts()
        if action == "get":
            return get_contract()
        if action == "update":
            return update_contract()

    return jsonify({"error": "unknown resource/action"}), 400


# ------------------------------------------------------------------
# Contracts
# ------------------------------------------------------------------
def create_contract():
    payload = request.get_json(force=True) or {}
    required = ["customer_id", "contract_type"]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    if payload["contract_type"] not in VALID_CONTRACT_TYPES:
        return jsonify({"error": f"invalid contract_type: {payload['contract_type']}"}), 400

    company_id = g.claims['company_id']
    record = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "customer_id": payload["customer_id"],
        "site_id": payload.get("site_id"),
        "contract_number": payload.get("contract_number"),
        "contract_type": payload["contract_type"],
        "coverage_notes": payload.get("coverage_notes"),
        "excluded_items": payload.get("excluded_items"),
        "sla_response_hours": payload.get("sla_response_hours"),
        "sla_resolution_hours": payload.get("sla_resolution_hours"),
        "billing_rate": payload.get("billing_rate"),
        "billing_notes": payload.get("billing_notes"),
        "working_hours": payload.get("working_hours"),
        "holiday_calendar_notes": payload.get("holiday_calendar_notes"),
        "escalation_matrix": payload.get("escalation_matrix"),
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "is_active": True,
        "created_at": datetime.utcnow().isoformat(),
    }

    sb = get_sb()
    result = sb.table("fsm_contracts").insert(record).execute()
    created = result.data[0]

    if record["site_id"]:
        sb.table("fsm_sites").update({"contract_id": created["id"]}).eq("id", record["site_id"]).eq("company_id", company_id).execute()

    return jsonify(created), 201


def list_contracts():
    company_id = g.claims['company_id']
    customer_id = request.args.get("customer_id")

    sb = get_sb()
    query = sb.table("fsm_contracts").select("*").eq("company_id", company_id)
    if customer_id:
        query = query.eq("customer_id", customer_id)
    result = query.order("end_date").execute()
    return jsonify(result.data)


def get_contract():
    company_id = g.claims['company_id']
    contract_id = request.args.get("id")
    if not contract_id:
        return jsonify({"error": "id required"}), 400

    sb = get_sb()
    result = sb.table("fsm_contracts").select("*").eq("id", contract_id).eq("company_id", company_id).single().execute()
    return jsonify(result.data)


def update_contract():
    company_id = g.claims['company_id']
    contract_id = request.args.get("id")
    if not contract_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    if "contract_type" in payload and payload["contract_type"] not in VALID_CONTRACT_TYPES:
        return jsonify({"error": f"invalid contract_type: {payload['contract_type']}"}), 400

    allowed = {
        "site_id", "contract_number", "contract_type", "coverage_notes", "excluded_items",
        "sla_response_hours", "sla_resolution_hours", "billing_rate", "billing_notes",
        "working_hours", "holiday_calendar_notes", "escalation_matrix",
        "start_date", "end_date", "is_active",
    }
    update = {k: v for k, v in payload.items() if k in allowed}
    update["updated_at"] = datetime.utcnow().isoformat()

    sb = get_sb()
    result = sb.table("fsm_contracts").update(update).eq("id", contract_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404

    if "site_id" in payload:
        if payload["site_id"]:
            sb.table("fsm_sites").update({"contract_id": contract_id}).eq("id", payload["site_id"]).eq("company_id", company_id).execute()
        sb.table("fsm_sites").update({"contract_id": None}).eq("contract_id", contract_id).neq("id", payload.get("site_id") or "").eq("company_id", company_id).execute()

    return jsonify(result.data[0])
