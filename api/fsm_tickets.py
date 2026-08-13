"""
api/fsm_tickets.py — FSM Phase 1: Tickets, Activity Timeline, Work Orders

Wired to match api/auth.py and the RBAC pattern in api/quotes.py.
Same gate as api/fsm_assets.py — single 'fsm' page_key.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import uuid
import requests
import traceback
from datetime import datetime, timedelta

from api.auth import verify_token, get_sb, get_ms_token, SENDER_EMAIL

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
# Module 9 (Corrective Maintenance) — a ticket carries breakdown-diagnosis
# fields when it's one of these types.
CM_TICKET_TYPES = {"incident", "corrective_maintenance"}
VALID_FAILURE_CATEGORIES = {
    "hardware_failure", "firmware_software", "cabling_connectivity", "power_electrical",
    "configuration_error", "environmental", "user_error", "wear_and_tear", "other",
}
REPEAT_FAILURE_WINDOW_DAYS = 90

# Module 13 (Billing) — classification + status tracked on the ticket.
VALID_BILLING_TYPES = {"warranty", "amc_included", "chargeable_visit", "time_material", "fixed_price"}
VALID_BILLING_STATUSES = {"not_billable", "pending", "quoted", "invoiced", "paid"}


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

    # "My Jobs" (resource=my) is the field-engineer self-service surface --
    # it only needs the narrower 'myJobs' permission (or full 'fsm' access,
    # so admins/coordinators can preview it too). Every other resource on
    # this router still requires full 'fsm' page access. Ownership of the
    # specific ticket/work order is enforced inside each my_* handler, not
    # here -- this gate only decides whether the endpoint family is reachable
    # at all.
    if request.args.get('resource') == 'my':
        if not (has_page_access(claims, 'myJobs') or has_page_access(claims, 'fsm')):
            return jsonify({'error': 'You do not have access to this feature.'}), 403
        g.claims = claims
        return None

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
        if action == "update_cm":
            return update_corrective_details()
        if action == "update_billing":
            return update_billing()

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
        if action == "reschedule":
            return reschedule_work_order()
        if action == "asset_statuses":
            return get_work_order_asset_statuses()

    # Module 5/6 self-service: a field engineer's own scoped view of exactly
    # the tickets assigned to them -- see the my_* functions below.
    if resource == "my":
        if action == "me":
            return my_profile()
        if action == "list":
            return my_list_tickets()
        if action == "get":
            return my_get_ticket()
        if action == "update_status":
            return my_update_status()
        if action == "complete_work_order":
            return my_complete_work_order()
        if action == "asset_statuses":
            return my_asset_statuses()

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


def _find_applicable_contract(sb, company_id, customer_id, site_id):
    """Module 11 (SLA Management): prefer a contract scoped to this exact
    site; fall back to a customer-wide contract (site_id is null). Only
    considers active contracts that haven't passed their end_date."""
    today = datetime.utcnow().date().isoformat()

    def _query(site_filter):
        q = (
            sb.table("fsm_contracts")
            .select("*")
            .eq("company_id", company_id)
            .eq("customer_id", customer_id)
            .eq("is_active", True)
        )
        q = q.is_("site_id", "null") if site_filter is None else q.eq("site_id", site_filter)
        return q.execute().data or []

    candidates = []
    if site_id:
        candidates = [c for c in _query(site_id) if not c.get("end_date") or c["end_date"] >= today]
    if not candidates:
        candidates = [c for c in _query(None) if not c.get("end_date") or c["end_date"] >= today]
    return candidates[0] if candidates else None


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
# Module 16 (Notifications) — email only for Phase 1. SMS/WhatsApp/push
# need a provider (Twilio/WhatsApp Business API/etc.) that isn't wired up
# yet; every send is logged to fsm_notifications regardless of outcome so
# there's a visible record even when delivery fails. Reuses the same
# Microsoft Graph sender api/auth.py already uses for invite/reset emails.
# ------------------------------------------------------------------
def _notify(sb, company_id, ticket_id, event_type, to_email, subject, html_body):
    if not to_email:
        return
    entry = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "ticket_id": ticket_id,
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
        _log_activity(sb, ticket_id, company_id, "notification_sent",
                      note=f"{event_type.replace('_',' ').title()} email to {to_email} — {entry['status']}")


def _notify_wrapper(fn):
    """Notifications should never fail the request they're attached to —
    wrap each call site so an email/DB hiccup doesn't turn a successful
    ticket action into a 500."""
    try:
        fn()
    except Exception as e:
        print(f"FSM notification dispatch error: {e}")


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

    # Module 11: apply SLA deadlines from whichever contract covers this
    # ticket's site/customer, if any.
    contract = _find_applicable_contract(sb, company_id, record["customer_id"], record["site_id"])
    if contract:
        record["contract_id"] = contract["id"]
        created_dt = datetime.utcnow()
        if contract.get("sla_response_hours") is not None:
            record["sla_response_due_at"] = (created_dt + timedelta(hours=float(contract["sla_response_hours"]))).isoformat()
        if contract.get("sla_resolution_hours") is not None:
            record["sla_resolution_due_at"] = (created_dt + timedelta(hours=float(contract["sla_resolution_hours"]))).isoformat()

    # Module 13: default billing classification from the same contract.
    ctype = contract.get("contract_type") if contract else None
    if ctype == "warranty":
        record["billing_type"], record["billing_status"] = "warranty", "not_billable"
    elif ctype in ("amc", "cmc", "fully_comprehensive"):
        record["billing_type"], record["billing_status"] = "amc_included", "not_billable"
    elif ctype in ("time_material", "labour_only", "parts_only"):
        record["billing_type"], record["billing_status"] = "time_material", "pending"
    else:
        record["billing_type"], record["billing_status"] = "chargeable_visit", "pending"

    # Module 9: auto-detect repeat failures — same asset, same breakdown-type
    # ticket, resolved/closed within the last REPEAT_FAILURE_WINDOW_DAYS.
    if record["ticket_type"] in CM_TICKET_TYPES and record["asset_id"]:
        cutoff = (datetime.utcnow() - timedelta(days=REPEAT_FAILURE_WINDOW_DAYS)).isoformat()
        prior = (
            sb.table("fsm_tickets")
            .select("id")
            .eq("company_id", company_id)
            .eq("asset_id", record["asset_id"])
            .in_("ticket_type", list(CM_TICKET_TYPES))
            .in_("status", ["resolved", "closed"])
            .gte("created_at", cutoff)
            .limit(1)
            .execute()
        )
        record["is_repeat_failure"] = bool(prior.data)

    result = sb.table("fsm_tickets").insert(record).execute()
    created = result.data[0]

    _log_activity(sb, ticket_id, company_id, "created", actor_id=actor_id, note="Ticket created")

    if record["site_id"]:
        def _send():
            site = sb.table("fsm_sites").select("contact_email,name").eq("id", record["site_id"]).single().execute().data
            if site and site.get("contact_email"):
                _notify(sb, company_id, ticket_id, "ticket_created", site["contact_email"],
                        f"Service ticket {created['ticket_number']} created",
                        f"<p>A new service ticket <strong>{created['ticket_number']}</strong> — {created['subject']} — has been logged for {site.get('name','your site')}.</p>")
        _notify_wrapper(_send)

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

    current = (
        sb.table("fsm_tickets")
        .select("status,first_response_at")
        .eq("id", ticket_id).eq("company_id", company_id).single().execute().data
    )
    now_iso = datetime.utcnow().isoformat()
    update = {"status": new_status, "updated_at": now_iso}

    # Module 11: stamp SLA milestone timestamps as the ticket moves through
    # its lifecycle. Each is set once (first time reached), except
    # resolved_at which refreshes if a ticket bounces back and gets
    # re-resolved.
    if current:
        if new_status != "new" and not current.get("first_response_at"):
            update["first_response_at"] = now_iso
        if new_status == "on_site":
            update["arrived_at"] = now_iso
        if new_status == "resolved":
            update["resolved_at"] = now_iso
        if new_status == "closed":
            update["closed_at"] = now_iso

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
    current = sb.table("fsm_tickets").select("first_response_at").eq("id", ticket_id).eq("company_id", company_id).single().execute().data
    now_iso = datetime.utcnow().isoformat()
    update = {
        "assigned_engineer_id": engineer_id,
        "status": "assigned",
        "updated_at": now_iso,
    }
    if current and not current.get("first_response_at"):
        update["first_response_at"] = now_iso
    result = sb.table("fsm_tickets").update(update).eq("id", ticket_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404

    _log_activity(sb, ticket_id, company_id, "assigned", actor_id=actor_id, note=f"Assigned to engineer {engineer_id}")

    def _send():
        eng = sb.table("fsm_engineers").select("email").eq("id", engineer_id).single().execute().data
        tk = result.data[0]
        if eng and eng.get("email"):
            _notify(sb, company_id, ticket_id, "engineer_assigned", eng["email"],
                    f"Assigned: {tk['ticket_number']} — {tk['subject']}",
                    f"<p>You've been assigned to ticket <strong>{tk['ticket_number']}</strong> — {tk['subject']}. Priority: {tk['priority']}.</p>")
    _notify_wrapper(_send)

    return jsonify(result.data[0])


def update_corrective_details():
    """Module 9 (Corrective Maintenance): root cause / fix / downtime fields
    captured as an incident/corrective_maintenance ticket gets diagnosed and
    resolved. Available on any ticket type — not force-restricted server-side
    since some 'service_request' tickets turn out to be breakdowns too — but
    the frontend only surfaces this form for CM-flavored ticket types."""
    company_id = g.claims['company_id']
    actor_id = g.claims['user_id']
    ticket_id = request.args.get("id")
    if not ticket_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    if "failure_category" in payload and payload["failure_category"] and payload["failure_category"] not in VALID_FAILURE_CATEGORIES:
        return jsonify({"error": f"invalid failure_category: {payload['failure_category']}"}), 400

    allowed = {"failure_category", "root_cause", "corrective_action",
               "temporary_fix", "permanent_fix", "downtime_minutes"}
    update = {k: v for k, v in payload.items() if k in allowed}
    if not update:
        return jsonify({"error": "no valid fields provided"}), 400
    update["updated_at"] = datetime.utcnow().isoformat()

    sb = get_sb()
    result = sb.table("fsm_tickets").update(update).eq("id", ticket_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404

    _log_activity(sb, ticket_id, company_id, "cm_details_updated", actor_id=actor_id,
                  note="Corrective maintenance details updated")

    return jsonify(result.data[0])


def update_billing():
    """Module 13 (Billing): classification/amount/status for a service visit.
    Actual quotation/invoice documents are NOT generated here — for a
    chargeable ticket the expected flow is to use the app's existing
    Quotes module (Deals-style 'Convert to Quote') rather than duplicating
    that pipeline in FSM."""
    company_id = g.claims['company_id']
    actor_id = g.claims['user_id']
    ticket_id = request.args.get("id")
    if not ticket_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    if "billing_type" in payload and payload["billing_type"] not in VALID_BILLING_TYPES:
        return jsonify({"error": f"invalid billing_type: {payload['billing_type']}"}), 400
    if "billing_status" in payload and payload["billing_status"] not in VALID_BILLING_STATUSES:
        return jsonify({"error": f"invalid billing_status: {payload['billing_status']}"}), 400

    allowed = {"billing_type", "billing_status", "billing_amount", "billing_notes"}
    update = {k: v for k, v in payload.items() if k in allowed}
    if not update:
        return jsonify({"error": "no valid fields provided"}), 400
    update["updated_at"] = datetime.utcnow().isoformat()

    sb = get_sb()
    result = sb.table("fsm_tickets").update(update).eq("id", ticket_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404

    _log_activity(sb, ticket_id, company_id, "billing_updated", actor_id=actor_id,
                  note="Billing details updated")

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

    # PM report fields — health rating, key observations, customer sign-off
    # name/date, revision, functional test results. All optional; kept as a
    # single jsonb blob since the shape mirrors what only the report PDF
    # renders, not anything else queried/filtered on.
    ALLOWED_REPORT_META_KEYS = {
        "health_rating", "key_observations", "customer_signature_name",
        "customer_signature_date", "revision", "functional_tests",
    }
    report_meta_in = payload.get("report_meta") or {}
    report_meta = {k: v for k, v in report_meta_in.items() if k in ALLOWED_REPORT_META_KEYS}

    update = {
        "completion_notes": payload.get("completion_notes"),
        "signature_url": payload.get("signature_url"),
        "updated_at": datetime.utcnow().isoformat(),
    }
    if "checklist" in payload:
        update["checklist"] = payload["checklist"]
    if report_meta:
        update["report_meta"] = report_meta

    result = sb.table("fsm_work_orders").update(update).eq("id", wo_id).eq("company_id", company_id).execute()
    if not result.data:
        return jsonify({"error": "not found"}), 404

    # Per-asset (per-room) status for this visit — upsert one row per asset,
    # scoped to this company so a cross-tenant asset_id can't be smuggled in.
    asset_statuses = payload.get("asset_statuses") or []
    if asset_statuses:
        valid_asset_ids = set()
        if asset_statuses:
            ids = [a.get("asset_id") for a in asset_statuses if a.get("asset_id")]
            if ids:
                owned = sb.table("fsm_assets").select("id").eq("company_id", company_id).in_("id", ids).execute().data or []
                valid_asset_ids = {r["id"] for r in owned}
        rows = []
        for a in asset_statuses:
            aid = a.get("asset_id")
            if not aid or aid not in valid_asset_ids:
                continue
            rows.append({
                "company_id": company_id,
                "work_order_id": wo_id,
                "asset_id": aid,
                "status": a.get("status") or "completed",
                "remarks": a.get("remarks"),
                "updated_at": datetime.utcnow().isoformat(),
            })
        if rows:
            sb.table("fsm_work_order_assets").upsert(rows, on_conflict="work_order_id,asset_id").execute()

    ticket_id = result.data[0]["ticket_id"]
    _log_activity(sb, ticket_id, company_id, "completed", note="Work order completed")

    def _send():
        tk = sb.table("fsm_tickets").select("ticket_number,subject,site_id").eq("id", ticket_id).single().execute().data
        if not tk or not tk.get("site_id"):
            return
        site = sb.table("fsm_sites").select("contact_email").eq("id", tk["site_id"]).single().execute().data
        if site and site.get("contact_email"):
            _notify(sb, company_id, ticket_id, "work_completed", site["contact_email"],
                    f"Work completed: {tk['ticket_number']}",
                    f"<p>The service visit for ticket <strong>{tk['ticket_number']}</strong> — {tk['subject']} — has been completed.</p>")
    _notify_wrapper(_send)

    return jsonify(result.data[0])


def get_work_order_asset_statuses():
    """Returns every asset at the work order's site, merged with any
    per-asset status/remarks already saved for this specific visit (via
    complete_work_order). Used to build the per-room checklist in the
    Complete Work Order modal and the PM report — always returns the full
    site asset list (not just ones already marked) so a new/incomplete visit
    still shows every room to check off, defaulting to 'completed'."""
    company_id = g.claims['company_id']
    wo_id = request.args.get("id")
    if not wo_id:
        return jsonify({"error": "id required"}), 400

    sb = get_sb()
    wo = sb.table("fsm_work_orders").select("ticket_id").eq("id", wo_id).eq("company_id", company_id).execute().data
    if not wo:
        return jsonify({"error": "not found"}), 404

    ticket = sb.table("fsm_tickets").select("site_id").eq("id", wo[0]["ticket_id"]).eq("company_id", company_id).execute().data
    site_id = ticket[0]["site_id"] if ticket else None
    if not site_id:
        return jsonify([])

    assets = sb.table("fsm_assets").select("id,asset_code,category,manufacturer,model").eq("company_id", company_id).eq("site_id", site_id).eq("is_deleted", False).order("asset_code").execute().data or []
    saved = sb.table("fsm_work_order_assets").select("asset_id,status,remarks").eq("company_id", company_id).eq("work_order_id", wo_id).execute().data or []
    saved_by_id = {s["asset_id"]: s for s in saved}

    out = []
    for a in assets:
        s = saved_by_id.get(a["id"], {})
        out.append({
            "asset_id": a["id"],
            "asset_code": a["asset_code"],
            "category": a.get("category"),
            "manufacturer": a.get("manufacturer"),
            "model": a.get("model"),
            "status": s.get("status", "completed"),
            "remarks": s.get("remarks", ""),
        })
    return jsonify(out)


def reschedule_work_order():
    """Module 6 (Scheduling & Dispatch): drag-and-drop a work order onto a
    different engineer and/or day on the scheduler. Also keeps the parent
    ticket's assigned_engineer_id in sync so the ticket detail picker and
    the scheduler never disagree about who's assigned.

    Wrapped in try/except (matching api/platform.py's pattern) so a DB error
    (e.g. a stale engineer_id whose row was deleted, tripping the
    fsm_work_orders -> fsm_engineers foreign key) comes back as a readable
    JSON error instead of a raw Flask 500 HTML page — the frontend's api()
    helper already surfaces r.error via alert() once it's actual JSON."""
    company_id = g.claims['company_id']
    actor_id = g.claims['user_id']
    wo_id = request.args.get("id")
    if not wo_id:
        return jsonify({"error": "id required"}), 400

    payload = request.get_json(force=True) or {}
    update = {"updated_at": datetime.utcnow().isoformat()}
    if "engineer_id" in payload:
        update["engineer_id"] = payload["engineer_id"]
    if "visit_date" in payload:
        update["visit_date"] = payload["visit_date"]

    sb = get_sb()
    try:
        result = sb.table("fsm_work_orders").update(update).eq("id", wo_id).eq("company_id", company_id).execute()
    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        if "foreign key" in msg.lower() and "engineer" in msg.lower():
            msg = "That engineer no longer exists. Refresh the Schedule tab and try again."
        return jsonify({"error": msg}), 500

    if not result.data:
        return jsonify({"error": "not found"}), 404

    wo = result.data[0]
    ticket_id = wo["ticket_id"]

    if "engineer_id" in payload and payload["engineer_id"]:
        try:
            sb.table("fsm_tickets").update({
                "assigned_engineer_id": payload["engineer_id"],
                "updated_at": datetime.utcnow().isoformat(),
            }).eq("id", ticket_id).eq("company_id", company_id).execute()
        except Exception as e:
            # The reschedule itself already succeeded above — don't fail the
            # whole request over the secondary ticket-sync write.
            traceback.print_exc()

    note_bits = []
    if "engineer_id" in payload:
        note_bits.append(f"engineer -> {payload['engineer_id']}")
    if "visit_date" in payload:
        note_bits.append(f"visit date -> {payload['visit_date']}")
    try:
        _log_activity(sb, ticket_id, company_id, "rescheduled", actor_id=actor_id,
                      note="Work order rescheduled: " + ", ".join(note_bits) if note_bits else "Work order rescheduled")
    except Exception as e:
        traceback.print_exc()

    return jsonify(result.data[0])


# ------------------------------------------------------------------
# "My Jobs" — field-engineer self-service (Module 5/6)
#
# A logged-in user's fsm_engineers row is found via user_id (set once, by an
# admin, when linking their account to an engineer profile under Setup ->
# Engineers). Every handler here re-derives that engineer id from the JWT and
# filters/checks against it server-side -- the caller can only ever see or
# touch tickets assigned to their own linked engineer record, regardless of
# what ids they pass in the URL. This is what makes 'myJobs' safe to grant
# without full 'fsm' access: the page-level gate in rbac_gate() only decides
# whether this resource is reachable, ownership is enforced here.
# ------------------------------------------------------------------
def _my_engineer(sb, claims):
    rows = (
        sb.table("fsm_engineers")
        .select("id,name")
        .eq("company_id", claims["company_id"])
        .eq("user_id", claims["user_id"])
        .eq("is_active", True)
        .execute().data
    )
    return rows[0] if rows else None


def _ticket_owned_by_engineer(sb, company_id, ticket_id, engineer_id):
    rows = (
        sb.table("fsm_tickets")
        .select("id,assigned_engineer_id")
        .eq("id", ticket_id).eq("company_id", company_id)
        .execute().data
    )
    return bool(rows) and rows[0].get("assigned_engineer_id") == engineer_id


def _work_order_owned_by_engineer(sb, company_id, wo_id, engineer_id):
    rows = (
        sb.table("fsm_work_orders")
        .select("id,ticket_id")
        .eq("id", wo_id).eq("company_id", company_id)
        .execute().data
    )
    if not rows:
        return False
    return _ticket_owned_by_engineer(sb, company_id, rows[0]["ticket_id"], engineer_id)


NOT_LINKED_MSG = "Your login isn't linked to an engineer profile yet. Ask your admin to link it under Field Service -> Setup -> Engineers."


def my_profile():
    sb = get_sb()
    eng = _my_engineer(sb, g.claims)
    return jsonify({"linked": bool(eng), "engineer": eng})


def my_list_tickets():
    sb = get_sb()
    eng = _my_engineer(sb, g.claims)
    if not eng:
        return jsonify({"error": NOT_LINKED_MSG, "linked": False}), 403
    company_id = g.claims["company_id"]
    q = (
        sb.table("fsm_tickets").select("*")
        .eq("company_id", company_id).eq("is_deleted", False)
        .eq("assigned_engineer_id", eng["id"])
    )
    if request.args.get("include_closed") != "1":
        q = q.not_.in_("status", ["closed", "cancelled"])
    result = q.order("created_at", desc=True).execute()
    return jsonify(result.data or [])


def my_get_ticket():
    sb = get_sb()
    eng = _my_engineer(sb, g.claims)
    if not eng:
        return jsonify({"error": NOT_LINKED_MSG, "linked": False}), 403
    company_id = g.claims["company_id"]
    ticket_id = request.args.get("id")
    if not ticket_id or not _ticket_owned_by_engineer(sb, company_id, ticket_id, eng["id"]):
        return jsonify({"error": "not found"}), 404
    t = sb.table("fsm_tickets").select("*").eq("id", ticket_id).eq("company_id", company_id).single().execute().data
    wos = (
        sb.table("fsm_work_orders").select("*")
        .eq("ticket_id", ticket_id).eq("company_id", company_id).eq("is_deleted", False)
        .execute().data or []
    )
    activity = (
        sb.table("fsm_ticket_activity").select("*")
        .eq("ticket_id", ticket_id).eq("company_id", company_id)
        .order("created_at", desc=True).execute().data or []
    )
    return jsonify({"ticket": t, "work_orders": wos, "activity": activity})


def my_update_status():
    """Ownership-checked wrapper around the existing update_ticket_status() --
    reused as-is so SLA milestone stamping (first_response_at/arrived_at/
    resolved_at/closed_at) and activity logging stay identical between the
    staff and engineer views instead of drifting into two implementations."""
    sb = get_sb()
    eng = _my_engineer(sb, g.claims)
    if not eng:
        return jsonify({"error": NOT_LINKED_MSG, "linked": False}), 403
    ticket_id = request.args.get("id")
    if not ticket_id or not _ticket_owned_by_engineer(sb, g.claims["company_id"], ticket_id, eng["id"]):
        return jsonify({"error": "not found"}), 404
    return update_ticket_status()


def my_complete_work_order():
    """Ownership-checked wrapper around the existing complete_work_order()."""
    sb = get_sb()
    eng = _my_engineer(sb, g.claims)
    if not eng:
        return jsonify({"error": NOT_LINKED_MSG, "linked": False}), 403
    wo_id = request.args.get("id")
    if not wo_id or not _work_order_owned_by_engineer(sb, g.claims["company_id"], wo_id, eng["id"]):
        return jsonify({"error": "not found"}), 404
    return complete_work_order()


def my_asset_statuses():
    """Ownership-checked wrapper around the existing get_work_order_asset_statuses()."""
    sb = get_sb()
    eng = _my_engineer(sb, g.claims)
    if not eng:
        return jsonify({"error": NOT_LINKED_MSG, "linked": False}), 403
    wo_id = request.args.get("id")
    if not wo_id or not _work_order_owned_by_engineer(sb, g.claims["company_id"], wo_id, eng["id"]):
        return jsonify({"error": "not found"}), 404
    return get_work_order_asset_statuses()
