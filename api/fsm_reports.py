"""
api/fsm_reports.py — FSM Module 17: Reports & Dashboards

Read-only. Computes every metric directly from existing FSM tables (no new
schema) — everything here is derived from data other modules already
capture. Deliberately does NOT fabricate numbers for metrics with no real
data source yet: Customer Satisfaction (no CSAT survey mechanism exists —
that would be a Module 14/Customer Portal follow-on) and Technician
Utilization (no attendance/working-hours tracking exists yet) are returned
as null with an explanation string rather than a made-up value.

Wired to match api/auth.py and the RBAC pattern in the rest of the FSM API
files — same gate, single 'fsm' page_key.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
from datetime import datetime

from api.auth import verify_token, get_sb

app = Flask(__name__)

OPEN_STATUSES = {"new", "acknowledged", "assigned", "accepted", "travelling", "on_site", "diagnosis"}
PENDING_STATUSES = {"waiting_customer", "waiting_parts", "waiting_approval"}
CLOSED_STATUSES = {"resolved", "closed"}
CHARGEABLE_BILLING_TYPES = {"chargeable_visit", "time_material", "fixed_price"}
AMC_CONTRACT_TYPES = {"amc", "cmc", "fully_comprehensive"}


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


@app.route("/api/fsm_reports", methods=["GET"])
def fsm_reports_router():
    action = request.args.get("action")
    if action == "summary":
        return summary()
    return jsonify({"error": "unknown action"}), 400


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def summary():
    company_id = g.claims['company_id']
    sb = get_sb()

    date_from = request.args.get("from")
    date_to = request.args.get("to")

    q = sb.table("fsm_tickets").select("*").eq("company_id", company_id).eq("is_deleted", False)
    if date_from:
        q = q.gte("created_at", date_from)
    if date_to:
        q = q.lte("created_at", date_to)
    tickets = q.execute().data or []

    by_status = {}
    for t in tickets:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    open_count = sum(by_status.get(s, 0) for s in OPEN_STATUSES)
    pending_count = sum(by_status.get(s, 0) for s in PENDING_STATUSES)
    closed_count = sum(by_status.get(s, 0) for s in CLOSED_STATUSES)

    repeat_failures = sum(1 for t in tickets if t.get("is_repeat_failure"))
    closed_tickets = [t for t in tickets if t["status"] in CLOSED_STATUSES]
    repeat_call_pct = round(100 * repeat_failures / len(closed_tickets), 1) if closed_tickets else None

    # Average resolution time + SLA compliance — only over tickets that
    # actually have both a created_at and a resolved_at.
    resolution_hours = []
    sla_met, sla_total = 0, 0
    for t in tickets:
        if t.get("resolved_at"):
            created = _parse_dt(t["created_at"])
            resolved = _parse_dt(t["resolved_at"])
            if created and resolved:
                resolution_hours.append((resolved - created).total_seconds() / 3600)
            if t.get("sla_resolution_due_at"):
                due = _parse_dt(t["sla_resolution_due_at"])
                if due:
                    sla_total += 1
                    if resolved and resolved <= due:
                        sla_met += 1
    avg_resolution_hours = round(sum(resolution_hours) / len(resolution_hours), 1) if resolution_hours else None
    sla_compliance_pct = round(100 * sla_met / sla_total, 1) if sla_total else None

    # First-time fix rate: closed tickets resolved with exactly one
    # completed work order count as a "first time fix."
    ticket_ids = [t["id"] for t in closed_tickets]
    first_time_fix_pct = None
    if ticket_ids:
        wos = (
            sb.table("fsm_work_orders")
            .select("ticket_id,completion_notes")
            .eq("company_id", company_id)
            .in_("ticket_id", ticket_ids)
            .execute()
            .data or []
        )
        completed_per_ticket = {}
        for w in wos:
            if w.get("completion_notes"):
                completed_per_ticket[w["ticket_id"]] = completed_per_ticket.get(w["ticket_id"], 0) + 1
        tickets_with_wo = [tid for tid in ticket_ids if completed_per_ticket.get(tid)]
        if tickets_with_wo:
            first_time = sum(1 for tid in tickets_with_wo if completed_per_ticket[tid] == 1)
            first_time_fix_pct = round(100 * first_time / len(tickets_with_wo), 1)

    # Engineer workload — open tickets per active engineer.
    engineers = sb.table("fsm_engineers").select("id,name").eq("company_id", company_id).eq("is_active", True).execute().data or []
    workload = {}
    for t in tickets:
        eid = t.get("assigned_engineer_id")
        if eid and t["status"] in OPEN_STATUSES:
            workload[eid] = workload.get(eid, 0) + 1
    engineer_workload = sorted(
        [{"id": e["id"], "name": e["name"], "open_tickets": workload.get(e["id"], 0)} for e in engineers],
        key=lambda x: -x["open_tickets"],
    )

    # Parts consumption (consumption + warranty_replacement transactions).
    txn_q = sb.table("fsm_spare_part_transactions").select("part_id,type,quantity,created_at").eq("company_id", company_id).in_("type", ["consumption", "warranty_replacement"])
    if date_from:
        txn_q = txn_q.gte("created_at", date_from)
    if date_to:
        txn_q = txn_q.lte("created_at", date_to)
    txns = txn_q.execute().data or []
    parts_consumed_qty = sum(t["quantity"] for t in txns)
    parts_cost = 0.0
    if txns:
        part_ids = list({t["part_id"] for t in txns})
        parts = sb.table("fsm_spare_parts").select("id,unit_cost").in_("id", part_ids).execute().data or []
        cost_by_id = {p["id"]: (p.get("unit_cost") or 0) for p in parts}
        parts_cost = sum(t["quantity"] * cost_by_id.get(t["part_id"], 0) for t in txns)

    # Financial.
    service_revenue = sum(
        float(t["billing_amount"] or 0) for t in tickets
        if t.get("billing_type") in CHARGEABLE_BILLING_TYPES and t.get("billing_status") in ("invoiced", "paid")
    )
    amc_contracts = (
        sb.table("fsm_contracts")
        .select("billing_rate")
        .eq("company_id", company_id)
        .eq("is_active", True)
        .in_("contract_type", list(AMC_CONTRACT_TYPES))
        .execute()
        .data or []
    )
    amc_contract_value = sum(float(c["billing_rate"] or 0) for c in amc_contracts)

    return jsonify({
        "operational": {
            "open_tickets": open_count,
            "pending_tickets": pending_count,
            "closed_tickets": closed_count,
            "total_tickets": len(tickets),
            "tickets_by_status": by_status,
            "engineer_workload": engineer_workload,
            "parts_consumed_qty": parts_consumed_qty,
            "parts_cost_consumed": round(parts_cost, 2),
            "repeat_failures": repeat_failures,
        },
        "financial": {
            "service_revenue": round(service_revenue, 2),
            "amc_contract_value": round(amc_contract_value, 2),
            "parts_cost_consumed": round(parts_cost, 2),
            "note": "Revenue reflects invoiced/paid chargeable tickets only. AMC contract value is the sum of active AMC/CMC/fully-comprehensive contracts' billing_rate — a recurring-value estimate, not a collected-cash figure.",
        },
        "kpis": {
            "avg_resolution_hours": avg_resolution_hours,
            "sla_compliance_pct": sla_compliance_pct,
            "first_time_fix_pct": first_time_fix_pct,
            "repeat_call_pct": repeat_call_pct,
            "customer_satisfaction": None,
            "customer_satisfaction_note": "Not tracked yet — no CSAT survey mechanism exists (would come with the Customer Portal).",
            "technician_utilization": None,
            "technician_utilization_note": "Not tracked yet — no working-hours/attendance data exists for engineers.",
        },
    })
