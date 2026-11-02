"""
api/fsm_contracts.py — FSM Module 10: Contract Management

Wired to match api/auth.py and the RBAC pattern in the rest of the FSM API
files — same gate, single 'fsm' page_key.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import re
import uuid
import calendar
from datetime import datetime, date, timedelta

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
        if action == "renew":
            return renew_contract()
        if action == "reclassify_gap":
            return reclassify_gap()

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


# ------------------------------------------------------------------
# Renewal
# ------------------------------------------------------------------
BILLING_FOR_TYPE = {
    "warranty": ("warranty", "not_billable"),
    "amc": ("amc_included", "not_billable"),
    "cmc": ("amc_included", "not_billable"),
    "fully_comprehensive": ("amc_included", "not_billable"),
    "time_material": ("time_material", "pending"),
    "labour_only": ("time_material", "pending"),
    "parts_only": ("time_material", "pending"),
}


def _add_months(d, months):
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _next_contract_number(num):
    if not num:
        return None
    m = re.match(r"^(.*?)(?:-R(\d+))?$", num)
    base, n = m.group(1), m.group(2)
    return f"{base}-R{int(n) + 1 if n else 1}"


def renew_contract():
    """Copy the contract into a new term, mark the old one inactive and link the
    two (renewed_from_id / renewed_to_id) so the history is visible."""
    company_id = g.claims['company_id']
    cid = request.args.get("id")
    if not cid:
        return jsonify({"error": "id required"}), 400
    payload = request.get_json(force=True) or {}
    sb = get_sb()

    rows = sb.table("fsm_contracts").select("*").eq("id", cid).eq("company_id", company_id).limit(1).execute().data or []
    if not rows:
        return jsonify({"error": "not found"}), 404
    old = rows[0]
    if old.get("renewed_to_id"):
        return jsonify({"error": "This contract has already been renewed."}), 400

    try:
        s = date.fromisoformat(old["start_date"]) if old.get("start_date") else None
        e = date.fromisoformat(old["end_date"]) if old.get("end_date") else None
        term = int(payload.get("term_months") or 0)
        if not term:
            term = max(1, round(((e - s).days + 1) / 30.4375)) if (s and e) else 12
        if payload.get("start_date"):
            new_start = date.fromisoformat(payload["start_date"])
        else:
            new_start = (e + timedelta(days=1)) if e else date.today()
        if payload.get("end_date"):
            new_end = date.fromisoformat(payload["end_date"])
        else:
            new_end = _add_months(new_start, term) - timedelta(days=1)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    if new_end < new_start:
        return jsonify({"error": "The new end date is before the new start date."}), 400

    def pick(k):
        return payload[k] if k in payload else old.get(k)

    new_id = str(uuid.uuid4())
    record = {
        "id": new_id,
        "company_id": company_id,
        "customer_id": old["customer_id"],
        "site_id": old.get("site_id"),
        "contract_number": payload.get("contract_number") or _next_contract_number(old.get("contract_number")),
        "contract_type": old["contract_type"],
        "coverage_notes": pick("coverage_notes"),
        "excluded_items": pick("excluded_items"),
        "sla_response_hours": pick("sla_response_hours"),
        "sla_resolution_hours": pick("sla_resolution_hours"),
        "billing_rate": pick("billing_rate"),
        "billing_notes": old.get("billing_notes"),
        "working_hours": old.get("working_hours"),
        "holiday_calendar_notes": old.get("holiday_calendar_notes"),
        "escalation_matrix": old.get("escalation_matrix"),
        "start_date": new_start.isoformat(),
        "end_date": new_end.isoformat(),
        "is_active": True,
        "renewed_from_id": cid,
        "created_at": datetime.utcnow().isoformat(),
    }
    try:
        created = sb.table("fsm_contracts").insert(record).execute().data[0]
    except Exception as ex:
        return jsonify({"error": f"Could not create the renewal (has migration-fsm-contract-renewal.sql been run?): {str(ex)[:200]}"}), 500

    try:
        sb.table("fsm_contracts").update({
            "is_active": False,
            "renewed_to_id": new_id,
            "renewed_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", cid).eq("company_id", company_id).execute()
    except Exception as ex:
        return jsonify({"error": f"Renewal {created.get('contract_number') or new_id} was created but the old contract could not be closed: {str(ex)[:200]}"}), 500

    if record["site_id"]:
        sb.table("fsm_sites").update({"contract_id": new_id}).eq("id", record["site_id"]).eq("company_id", company_id).execute()

    gap = _gap_ticket_ids(sb, company_id, created)
    return jsonify({"contract": created, "old_contract_id": cid, "gap_tickets": len(gap)}), 201


def _gap_ticket_ids(sb, company_id, contract):
    """Open-for-billing tickets raised while no contract was active, within the
    new contract's term and for the same customer/site."""
    try:
        q = (
            sb.table("fsm_tickets").select("id")
            .eq("company_id", company_id)
            .eq("customer_id", contract["customer_id"])
            .eq("coverage_gap", True)
            .in_("billing_status", ["pending", "not_billable"])
            # advance-generated PM tickets are created up to 90 days before their visit
            .gte("created_at", (date.fromisoformat(contract["start_date"]) - timedelta(days=90)).isoformat())
        )
        if contract.get("site_id"):
            q = q.eq("site_id", contract["site_id"])
        if contract.get("end_date"):
            q = q.lte("created_at", contract["end_date"] + "T23:59:59")
        return [r["id"] for r in (q.execute().data or [])]
    except Exception:
        return []


def reclassify_gap():
    """After a late renewal: apply the new contract to tickets raised during the
    gap (billing class from the contract; invoiced/paid tickets are never touched)."""
    company_id = g.claims['company_id']
    cid = request.args.get("id")
    if not cid:
        return jsonify({"error": "id required"}), 400
    sb = get_sb()
    rows = sb.table("fsm_contracts").select("*").eq("id", cid).eq("company_id", company_id).limit(1).execute().data or []
    if not rows:
        return jsonify({"error": "not found"}), 404
    contract = rows[0]
    ids = _gap_ticket_ids(sb, company_id, contract)
    btype, bstatus = BILLING_FOR_TYPE.get(contract["contract_type"], ("chargeable_visit", "pending"))
    n = 0
    for tid in ids:
        sb.table("fsm_tickets").update({
            "contract_id": cid,
            "billing_type": btype,
            "billing_status": bstatus,
            "coverage_gap": False,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", tid).eq("company_id", company_id).execute()
        try:
            sb.table("fsm_ticket_activity").insert({
                "id": str(uuid.uuid4()), "ticket_id": tid, "company_id": company_id,
                "event_type": "billing_updated", "actor_id": g.claims.get("user_id"),
                "note": f"Covered retroactively by renewed contract {contract.get('contract_number') or cid[:8]}",
                "metadata": {}, "created_at": datetime.utcnow().isoformat(),
            }).execute()
        except Exception:
            pass
        n += 1
    return jsonify({"updated": n})
