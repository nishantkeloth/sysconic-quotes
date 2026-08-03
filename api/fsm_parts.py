"""
api/fsm_parts.py — FSM Module 12: Spare Parts

Wired to match api/auth.py and the RBAC pattern in the rest of the FSM API
files — same gate, single 'fsm' page_key.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import uuid
from datetime import datetime

from api.auth import verify_token, get_sb

app = Flask(__name__)

VALID_TXN_TYPES = {
    "stock_in", "reservation", "release_reservation", "consumption",
    "return", "warranty_replacement", "adjustment",
}
# How each transaction type moves quantity_on_hand / quantity_reserved.
# Positive = add, negative = subtract; 'adjustment' is the one type where
# the caller's quantity sign is used as-is (can be a manual correction
# either direction).
ON_HAND_DELTA = {
    "stock_in": 1, "consumption": -1, "return": 1, "warranty_replacement": -1,
}
RESERVED_DELTA = {"reservation": 1, "release_reservation": -1}


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
@app.route("/api/fsm_parts", methods=["GET", "POST", "PATCH"])
def fsm_parts_router():
    resource = request.args.get("resource")
    action = request.args.get("action")

    if resource == "part":
        if action == "create":
            return create_part()
        if action == "list":
            return list_parts()
        if action == "get":
            return get_part()
        if action == "update":
            return update_part()

    if resource == "transaction":
        if action == "create":
            return create_transaction()
        if action == "list":
            return list_transactions()

    return jsonify({"error": "unknown resource/action"}), 400


# ------------------------------------------------------------------
# Parts
# ------------------------------------------------------------------
def create_part():
    payload = request.get_json(force=True) or {}
    required = ["part_code", "name"]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    company_id = g.claims['company_id']
    record = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "part_code": payload["part_code"],
        "name": payload["name"],
        "category": payload.get("category"),
        "manufacturer": payload.get("manufacturer"),
        "compatible_models": payload.get("compatible_models"),
        "supplier": payload.get("supplier"),
        "stock_location": payload.get("stock_location"),
        "quantity_on_hand": int(payload.get("quantity_on_hand") or 0),
        "reorder_level": payload.get("reorder_level"),
        "unit_cost": payload.get("unit_cost"),
        "is_active": True,
        "created_at": datetime.utcnow().isoformat(),
    }

    sb = get_sb()
    try:
        result = sb.table("fsm_spare_parts").insert(record).execute()
    except Exception as e:
        if "fsm_spare_parts_company_id_part_code_key" in str(e):
            return jsonify({"error": f"part_code '{payload['part_code']}' already exists"}), 409
        raise
    return jsonify(result.data[0]), 201


def list_parts():
    company_id = g.claims['company_id']
    active_only = request.args.get("active_only", "true").lower() != "false"

    sb = get_sb()
    query = sb.table("fsm_spare_parts").select("*").eq("company_id", company_id)
    if active_only:
        query = query.eq("is_active", True)
    result = query.order("name").execute()
    return jsonify(result.data)


def get_part():
    company_id = g.claims['company_id']
    part_id = request.args.get("id")
    if not part_id:
        return jsonify({"error": "id required"}), 400

    sb = get_sb()
    result = sb.table("fsm_spare_parts").select("*").eq("id", part_id).eq("company_id", company_id).single().execute()
    return jsonify(result.data)


def update_part():
    company_id = g.claims['company_id']
    part_id = request.args.get("id")
    if not part_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    allowed = {"name", "category", "manufacturer", "compatible_models", "supplier",
               "stock_location", "reorder_level", "unit_cost", "is_active"}
    update = {k: v for k, v in payload.items() if k in allowed}
    if not update:
        return jsonify({"error": "no valid fields provided"}), 400
    update["updated_at"] = datetime.utcnow().isoformat()

    sb = get_sb()
    result = sb.table("fsm_spare_parts").update(update).eq("id", part_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404
    return jsonify(result.data[0])


# ------------------------------------------------------------------
# Transactions — every stock movement goes through here so
# quantity_on_hand / quantity_reserved always reflect the transaction log.
# ------------------------------------------------------------------
def create_transaction():
    payload = request.get_json(force=True) or {}
    part_id = payload.get("part_id")
    txn_type = payload.get("type")
    quantity = payload.get("quantity")

    if not part_id or txn_type not in VALID_TXN_TYPES or not quantity:
        return jsonify({"error": "part_id, a valid type, and a non-zero quantity are required"}), 400
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return jsonify({"error": "quantity must be an integer"}), 400
    if txn_type != "adjustment" and quantity < 0:
        return jsonify({"error": "quantity must be positive for this transaction type"}), 400

    company_id = g.claims['company_id']
    actor_id = g.claims['user_id']
    sb = get_sb()

    part = sb.table("fsm_spare_parts").select("*").eq("id", part_id).eq("company_id", company_id).single().execute().data
    if not part:
        return jsonify({"error": "part not found"}), 404

    on_hand_delta = quantity if txn_type == "adjustment" else ON_HAND_DELTA.get(txn_type, 0) * quantity
    reserved_delta = RESERVED_DELTA.get(txn_type, 0) * quantity

    new_on_hand = max(0, part["quantity_on_hand"] + on_hand_delta)
    new_reserved = max(0, part["quantity_reserved"] + reserved_delta)

    sb.table("fsm_spare_parts").update({
        "quantity_on_hand": new_on_hand,
        "quantity_reserved": new_reserved,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", part_id).execute()

    txn = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "part_id": part_id,
        "ticket_id": payload.get("ticket_id"),
        "work_order_id": payload.get("work_order_id"),
        "type": txn_type,
        "quantity": quantity,
        "notes": payload.get("notes"),
        "actor_id": actor_id,
        "created_at": datetime.utcnow().isoformat(),
    }
    sb.table("fsm_spare_part_transactions").insert(txn).execute()

    return jsonify({"transaction": txn, "quantity_on_hand": new_on_hand, "quantity_reserved": new_reserved}), 201


def list_transactions():
    company_id = g.claims['company_id']
    part_id = request.args.get("part_id")
    if not part_id:
        return jsonify({"error": "part_id required"}), 400

    sb = get_sb()
    result = (
        sb.table("fsm_spare_part_transactions")
        .select("*")
        .eq("company_id", company_id)
        .eq("part_id", part_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return jsonify(result.data)
