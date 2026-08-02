"""
api/fsm_assets.py — FSM Phase 1: Sites & Assets

Wired to match api/auth.py and the RBAC pattern in api/quotes.py:
- verify_token(request) -> claims dict or None
- has_page_access(claims, 'fsm') -> bool (admin always True, else role_permissions.get('fsm'))
- get_sb() -> supabase client
- features gate: claims['features'].get('fsm_module')

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import uuid
from datetime import datetime

from api.auth import verify_token, get_sb

app = Flask(__name__)


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
@app.route("/api/fsm_assets", methods=["GET", "POST", "PATCH"])
def fsm_assets_router():
    resource = request.args.get("resource")
    action = request.args.get("action")

    if resource == "site":
        if action == "create":
            return create_site()
        if action == "list":
            return list_sites()
        if action == "get":
            return get_site()
        if action == "update":
            return update_site()

    if resource == "asset":
        if action == "create":
            return create_asset()
        if action == "list":
            return list_assets()
        if action == "get":
            return get_asset()
        if action == "update":
            return update_asset()

    return jsonify({"error": "unknown resource/action"}), 400


# ------------------------------------------------------------------
# Sites
# ------------------------------------------------------------------
def create_site():
    payload = request.get_json(force=True) or {}
    required = ["customer_id", "name"]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    company_id = g.claims['company_id']
    record = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "customer_id": payload["customer_id"],
        "name": payload["name"],
        "address": payload.get("address"),
        "building": payload.get("building"),
        "floor": payload.get("floor"),
        "room": payload.get("room"),
        "department": payload.get("department"),
        "contact_name": payload.get("contact_name"),
        "contact_phone": payload.get("contact_phone"),
        "contact_email": payload.get("contact_email"),
        "notes": payload.get("notes"),
        "created_at": datetime.utcnow().isoformat(),
    }

    sb = get_sb()
    result = sb.table("fsm_sites").insert(record).execute()
    return jsonify(result.data[0]), 201


def list_sites():
    company_id = g.claims['company_id']
    customer_id = request.args.get("customer_id")

    sb = get_sb()
    query = sb.table("fsm_sites").select("*").eq("company_id", company_id).eq("is_deleted", False)
    if customer_id:
        query = query.eq("customer_id", customer_id)
    result = query.execute()
    return jsonify(result.data)


def get_site():
    company_id = g.claims['company_id']
    site_id = request.args.get("id")
    if not site_id:
        return jsonify({"error": "id required"}), 400

    sb = get_sb()
    result = sb.table("fsm_sites").select("*").eq("id", site_id).eq("company_id", company_id).single().execute()
    return jsonify(result.data)


def update_site():
    company_id = g.claims['company_id']
    site_id = request.args.get("id")
    if not site_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    payload["updated_at"] = datetime.utcnow().isoformat()

    sb = get_sb()
    result = sb.table("fsm_sites").update(payload).eq("id", site_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404
    return jsonify(result.data[0])


# ------------------------------------------------------------------
# Assets
# ------------------------------------------------------------------
def create_asset():
    payload = request.get_json(force=True) or {}
    required = ["site_id", "asset_code"]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    company_id = g.claims['company_id']
    record = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "site_id": payload["site_id"],
        "parent_asset_id": payload.get("parent_asset_id"),
        "asset_code": payload["asset_code"],
        "category": payload.get("category"),
        "manufacturer": payload.get("manufacturer"),
        "model": payload.get("model"),
        "serial_number": payload.get("serial_number"),
        "barcode": payload.get("barcode"),
        "installation_date": payload.get("installation_date"),
        "warranty_expiry": payload.get("warranty_expiry"),
        "supplier": payload.get("supplier"),
        "purchase_order": payload.get("purchase_order"),
        "firmware_version": payload.get("firmware_version"),
        "software_version": payload.get("software_version"),
        "status": payload.get("status", "active"),
        "photo_urls": payload.get("photo_urls", []),
        "document_urls": payload.get("document_urls", []),
        "created_at": datetime.utcnow().isoformat(),
    }

    sb = get_sb()
    try:
        result = sb.table("fsm_assets").insert(record).execute()
    except Exception as e:
        if "uq_fsm_assets_code_per_company" in str(e):
            return jsonify({"error": f"asset_code '{payload['asset_code']}' already exists"}), 409
        raise
    return jsonify(result.data[0]), 201


def list_assets():
    company_id = g.claims['company_id']
    site_id = request.args.get("site_id")

    sb = get_sb()
    query = sb.table("fsm_assets").select("*").eq("company_id", company_id).eq("is_deleted", False)
    if site_id:
        query = query.eq("site_id", site_id)
    result = query.execute()
    return jsonify(result.data)


def get_asset():
    company_id = g.claims['company_id']
    asset_id = request.args.get("id")
    if not asset_id:
        return jsonify({"error": "id required"}), 400

    sb = get_sb()
    result = sb.table("fsm_assets").select("*").eq("id", asset_id).eq("company_id", company_id).single().execute()
    return jsonify(result.data)


def update_asset():
    company_id = g.claims['company_id']
    asset_id = request.args.get("id")
    if not asset_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    payload["updated_at"] = datetime.utcnow().isoformat()

    sb = get_sb()
    result = sb.table("fsm_assets").update(payload).eq("id", asset_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404
    return jsonify(result.data[0])
