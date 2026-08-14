"""
api/fsm_portal.py — FSM Module 14: Customer Portal

External-facing surface — no staff login involved. A customer contact gets
their own fsm_portal_users row (created via a staff-sent invite, same
token+email pattern as api/auth.py's team invites, reusing get_ms_token/
SENDER_EMAIL) and can then log in with a separate portal JWT (role:
"portal_customer") to view their own tickets and self-log new ones —
this is the explicit AMC self-service requirement.

Deliberately simple compared to api/auth.py's staff session model: a
single 24h JWT, no refresh-token/session-rotation table. Portal customers
re-authenticate daily rather than staying signed in for weeks — an
acceptable tradeoff for a lean external portal, not worth building a
second session-rotation system for.

Re-sending an invite (fsm_portal_invites) doubles as "reset my password"
for an already-active user — no separate password-reset table needed.

Ticket creation forces customer_id/company_id from the portal session —
never trusts client input for those, so a portal user can't create a
ticket against another customer's account.

The customer-facing ticket view intentionally does NOT expose the raw
fsm_ticket_activity feed (it can contain internal engineer notes) — only
structured, known-safe fields: status, subject, description, priority,
and the milestone timestamps already tracked on the ticket itself.

Self-resolution: before a customer submits a ticket, the portal calls
self_help() (a Gemini call, duplicated from api/fsm_ai.py's troubleshoot
per the house convention of not sharing code across serverless files) to
suggest likely causes and a quick checklist grounded in the company's own
Knowledge Base — the goal is to cut unnecessary truck rolls for problems
a customer can fix themselves (a loose cable, a wrong input selected on
a control panel, etc.) while still making it one click to submit a real
ticket if the tips don't help.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import os
import json
import uuid
import bcrypt
import jwt
import requests
import traceback
import urllib.request
import urllib.error
from datetime import datetime, timedelta

from api.auth import get_sb, get_ms_token, SENDER_EMAIL

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI_MODEL = 'gemini-3.5-flash'
GEMINI_URL = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'

app = Flask(__name__)

JWT_SECRET = os.environ.get('JWT_SECRET')
PORTAL_TOKEN_HOURS = 24
INVITE_EXPIRES_DAYS = 7

VALID_TICKET_TYPES = {"incident", "service_request"}  # portal customers only pick between these two
VALID_PRIORITIES = {"low", "medium", "high", "critical"}


def get_app_url():
    override = os.environ.get('APP_URL')
    if override:
        return override.rstrip('/')
    return request.url_root.rstrip('/')


def make_portal_token(portal_user_id, company_id, customer_id):
    return jwt.encode({
        'portal_user_id': str(portal_user_id),
        'company_id': str(company_id),
        'customer_id': str(customer_id),
        'role': 'portal_customer',
        'exp': datetime.utcnow() + timedelta(hours=PORTAL_TOKEN_HOURS),
    }, JWT_SECRET, algorithm='HS256')


def verify_portal_token(req):
    auth = req.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    try:
        claims = jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except Exception:
        return None
    if claims.get('role') != 'portal_customer':
        return None
    return claims


# Actions unauthenticated by a portal_customer JWT — either genuinely public
# (login, accept_invite, invite_details, used before any session exists) or
# staff-authenticated instead (invite, list_portal_users — these check
# api.auth's verify_token internally in their own handler, not a portal
# token). Either way, portal_gate must not run its portal-token check on
# them, or a staff member's own Bearer token gets rejected here as an
# invalid portal token before their handler's staff-auth check ever runs —
# which previously bubbled up as a 401 that logged the staff member out of
# the whole app just for opening the Customer Portal Access panel.
PUBLIC_ACTIONS = {'login', 'accept_invite', 'invite_details'}
STAFF_AUTHENTICATED_ACTIONS = {'invite', 'list_portal_users', 'list_portal_status', 'set_active'}


@app.before_request
def portal_gate():
    action = (request.get_json(silent=True) or {}).get('action') if request.method == 'POST' else request.args.get('action')
    if action in PUBLIC_ACTIONS or action in STAFF_AUTHENTICATED_ACTIONS:
        return None

    claims = verify_portal_token(request)
    if not claims:
        return jsonify({'error': 'unauthorized'}), 401

    sb = get_sb()
    co = sb.table('companies').select('status,features').eq('id', claims['company_id']).execute().data
    if not co:
        return jsonify({'error': 'unauthorized'}), 401
    co = co[0]
    if co.get('status') == 'suspended':
        return jsonify({'error': 'This service is currently unavailable. Please contact your service provider.'}), 403
    if not (co.get('features') or {}).get('fsm_module'):
        return jsonify({'error': 'The customer portal is not enabled for this account.'}), 403

    # Checked on every request, not just at login, so a staff member locking
    # a portal login (set_active) takes effect immediately — the token
    # itself has no server-side revocation (see make_portal_token's comment),
    # so without this check a locked account would stay usable for up to
    # 24h until its JWT naturally expired.
    user = sb.table('fsm_portal_users').select('is_active').eq('id', claims['portal_user_id']).execute().data
    if not user or not user[0].get('is_active'):
        return jsonify({'error': 'This account has been deactivated. Contact your service provider.'}), 403

    g.claims = claims
    return None


@app.route("/api/fsm_portal", methods=["GET", "POST"])
def fsm_portal_router():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action == "login":
            return login(body)
        if action == "accept_invite":
            return accept_invite(body)
        if action == "invite":
            return send_invite(body)
        if action == "create_ticket":
            return create_ticket(body)
        if action == "self_help":
            return self_help(body)
        if action == "set_active":
            return set_active(body)
        return jsonify({"error": "unknown action"}), 400

    action = request.args.get("action")
    if action == "invite_details":
        return invite_details()
    if action == "me":
        return me()
    if action == "list_sites":
        return list_sites()
    if action == "list_assets":
        return list_assets()
    if action == "list_tickets":
        return list_tickets()
    if action == "get_ticket":
        return get_ticket()
    if action == "list_portal_users":
        return list_portal_users()
    if action == "list_portal_status":
        return list_portal_status()
    return jsonify({"error": "unknown action"}), 400


# ── Auth ─────────────────────────────────────────────────────────────────

def login(body):
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    sb = get_sb()
    rows = sb.table("fsm_portal_users").select("*").eq("email", email).execute().data or []
    if not rows:
        return jsonify({"error": "Invalid email or password"}), 401
    user = rows[0]

    if not user.get("is_active"):
        return jsonify({"error": "This account has been deactivated. Contact your service provider."}), 403

    pw_hash = user.get("password_hash") or ""
    if not pw_hash or not bcrypt.checkpw(password.encode(), pw_hash.encode()):
        return jsonify({"error": "Invalid email or password"}), 401

    co = sb.table("companies").select("status,features,name").eq("id", user["company_id"]).execute().data
    if not co or co[0].get("status") == "suspended":
        return jsonify({"error": "This service is currently unavailable."}), 403
    if not (co[0].get("features") or {}).get("fsm_module"):
        return jsonify({"error": "The customer portal is not enabled for this account."}), 403

    sb.table("fsm_portal_users").update({"last_login_at": datetime.utcnow().isoformat()}).eq("id", user["id"]).execute()
    token = make_portal_token(user["id"], user["company_id"], user["customer_id"])

    customer = sb.table("customers").select("name").eq("id", user["customer_id"]).execute().data
    return jsonify({
        "token": token,
        "user": {"id": user["id"], "name": user.get("name", ""), "email": user["email"]},
        "customer": {"id": user["customer_id"], "name": customer[0]["name"] if customer else ""},
        "company_name": co[0].get("name", ""),
    })


def invite_details():
    token = request.args.get("token", "")
    sb = get_sb()
    inv = sb.table("fsm_portal_invites").select("*").eq("token", token).eq("accepted", False).execute().data
    if not inv or inv[0]["expires_at"] < datetime.utcnow().isoformat():
        return jsonify({"error": "This invite link is invalid or has already been used"}), 400
    invite = inv[0]
    co = sb.table("companies").select("name").eq("id", invite["company_id"]).execute().data
    return jsonify({"email": invite["email"], "name": invite.get("name") or "", "company_name": co[0]["name"] if co else ""})


def accept_invite(body):
    token = body.get("token") or ""
    name = (body.get("name") or "").strip()
    password = body.get("password") or ""
    if not token:
        return jsonify({"error": "Missing invite token"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    sb = get_sb()
    inv = sb.table("fsm_portal_invites").select("*").eq("token", token).eq("accepted", False).execute().data
    if not inv or inv[0]["expires_at"] < datetime.utcnow().isoformat():
        return jsonify({"error": "This invite link is invalid or has already been used"}), 400
    invite = inv[0]

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    existing = sb.table("fsm_portal_users").select("id").eq("company_id", invite["company_id"]).eq("email", invite["email"]).execute().data
    if existing:
        # Re-invited (password reset) — update the existing login instead of duplicating it.
        user = sb.table("fsm_portal_users").update({
            "password_hash": pw_hash, "is_active": True,
            "name": name or invite.get("name") or "",
        }).eq("id", existing[0]["id"]).execute().data[0]
    else:
        user = sb.table("fsm_portal_users").insert({
            "id": str(uuid.uuid4()),
            "company_id": invite["company_id"],
            "customer_id": invite["customer_id"],
            "email": invite["email"],
            "name": name or invite.get("name") or "",
            "password_hash": pw_hash,
            "is_active": True,
            "created_at": datetime.utcnow().isoformat(),
        }).execute().data[0]

    sb.table("fsm_portal_invites").update({"accepted": True}).eq("id", invite["id"]).execute()

    token_out = make_portal_token(user["id"], user["company_id"], user["customer_id"])
    customer = sb.table("customers").select("name").eq("id", user["customer_id"]).execute().data
    return jsonify({
        "token": token_out,
        "user": {"id": user["id"], "name": user.get("name", ""), "email": user["email"]},
        "customer": {"id": user["customer_id"], "name": customer[0]["name"] if customer else ""},
    })


# ── Staff-side: invite a customer contact (called from the main app, staff auth) ──
# NOTE: this action is intentionally reachable without a portal token (see
# PUBLIC_ACTIONS) but it isn't actually public — it authenticates the
# *staff* member via the normal app JWT (api.auth.verify_token), completely
# separate from the portal_customer token used everywhere else in this file.
def send_invite(body):
    from api.auth import verify_token as verify_staff_token
    claims = verify_staff_token(request)
    if not claims:
        return jsonify({"error": "unauthorized"}), 401
    if not (claims.get('features') or {}).get('fsm_module'):
        return jsonify({'error': 'FSM module not enabled for this company'}), 403

    company_id = claims['company_id']
    customer_id = body.get("customer_id")
    email = (body.get("email") or "").strip().lower()
    name = (body.get("name") or "").strip()
    if not customer_id or not email:
        return jsonify({"error": "customer_id and email required"}), 400

    sb = get_sb()
    customer = sb.table("customers").select("name").eq("id", customer_id).eq("company_id", company_id).execute().data
    if not customer:
        return jsonify({"error": "customer not found"}), 404

    invite_token = str(uuid.uuid4())
    sb.table("fsm_portal_invites").insert({
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "customer_id": customer_id,
        "email": email,
        "name": name,
        "token": invite_token,
        "invited_by": claims.get("user_id"),
        "expires_at": (datetime.utcnow() + timedelta(days=INVITE_EXPIRES_DAYS)).isoformat(),
        "created_at": datetime.utcnow().isoformat(),
    }).execute()

    invite_url = f"{get_app_url()}?portal_invite={invite_token}"
    email_sent = _send_portal_invite_email(email, invite_url, customer[0]["name"], name)

    return jsonify({
        "invite_url": invite_url,
        "email_sent": email_sent,
        "message": f"Invite {'sent to ' + email if email_sent else 'created (email failed — share the link manually)'}",
    })


def _send_portal_invite_email(to_email, invite_url, customer_name, contact_name):
    try:
        token = get_ms_token()
        if not token:
            return False
        subject = "You're invited to the Service Portal"
        body = f"""
        <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1a1a1a;max-width:600px;margin:0 auto;padding:20px">
            <div style="background:#1a3c6e;padding:24px;border-radius:8px 8px 0 0;text-align:center">
                <div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:.02em">SERVICE PORTAL</div>
            </div>
            <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:32px">
                <h2 style="color:#1a3c6e;margin:0 0 16px">You're invited</h2>
                <p style="color:#555;line-height:1.6;margin-bottom:20px">
                    Hi {contact_name or ''}, you've been invited to log and track service tickets for <strong>{customer_name}</strong> online.
                </p>
                <div style="text-align:center;margin-bottom:28px">
                    <a href="{invite_url}" style="background:#1a3c6e;color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-size:15px;font-weight:700;display:inline-block">
                        Set Up Your Account →
                    </a>
                </div>
                <div style="background:#f8f9fc;border-radius:6px;padding:14px;margin-bottom:20px">
                    <p style="font-size:12px;color:#888;margin:0 0 6px">Or copy this link:</p>
                    <p style="font-size:12px;color:#1a3c6e;word-break:break-all;margin:0">{invite_url}</p>
                </div>
                <p style="font-size:12px;color:#aaa;margin:0">This invite expires in 7 days.</p>
            </div>
        </body></html>
        """
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_email}}],
                "from": {"emailAddress": {"address": SENDER_EMAIL}},
            }
        }
        r = requests.post(
            f"https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail",
            json=payload, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        return r.status_code == 202
    except Exception as e:
        print(f"Portal invite email error: {e}")
        return False


def list_portal_users():
    from api.auth import verify_token as verify_staff_token
    claims = verify_staff_token(request)
    if not claims:
        return jsonify({"error": "unauthorized"}), 401
    customer_id = request.args.get("customer_id")
    if not customer_id:
        return jsonify({"error": "customer_id required"}), 400
    sb = get_sb()
    users = sb.table("fsm_portal_users").select("id,email,name,is_active,last_login_at,created_at").eq("company_id", claims['company_id']).eq("customer_id", customer_id).execute().data or []
    pending = sb.table("fsm_portal_invites").select("email,name,expires_at,created_at").eq("company_id", claims['company_id']).eq("customer_id", customer_id).eq("accepted", False).execute().data or []
    return jsonify({"users": users, "pending_invites": pending})


# Company-wide, one query — lets the Customers list show a portal-status
# badge per row without an N+1 query per customer.
def list_portal_status():
    from api.auth import verify_token as verify_staff_token
    claims = verify_staff_token(request)
    if not claims:
        return jsonify({"error": "unauthorized"}), 401
    sb = get_sb()
    rows = sb.table("fsm_portal_users").select("customer_id,is_active").eq("company_id", claims['company_id']).execute().data or []
    status = {}
    for r in rows:
        cid = r["customer_id"]
        entry = status.setdefault(cid, {"active_count": 0, "total": 0})
        entry["total"] += 1
        if r.get("is_active"):
            entry["active_count"] += 1
    return jsonify({"status": status})


# Lock/unlock (deactivate/reactivate) an existing portal login. Deactivating
# doesn't delete the row or revoke an already-issued token early (the
# portal's stateless 24h JWT has no server-side revocation — see the file
# header for why that's an accepted tradeoff) but it does block every
# subsequent login attempt and every gated action once that token expires,
# which is at most 24 hours.
def set_active(body):
    from api.auth import verify_token as verify_staff_token
    claims = verify_staff_token(request)
    if not claims:
        return jsonify({"error": "unauthorized"}), 401

    portal_user_id = body.get("portal_user_id")
    is_active = bool(body.get("is_active"))
    if not portal_user_id:
        return jsonify({"error": "portal_user_id required"}), 400

    sb = get_sb()
    existing = sb.table("fsm_portal_users").select("id").eq("id", portal_user_id).eq("company_id", claims['company_id']).execute().data
    if not existing:
        return jsonify({"error": "not found"}), 404

    row = sb.table("fsm_portal_users").update({"is_active": is_active}).eq("id", portal_user_id).execute().data
    return jsonify({"user": row[0] if row else None})


# ── Portal-authenticated actions ────────────────────────────────────────

def me():
    sb = get_sb()
    user = sb.table("fsm_portal_users").select("id,name,email,customer_id").eq("id", g.claims["portal_user_id"]).execute().data
    if not user:
        return jsonify({"error": "not found"}), 404
    customer = sb.table("customers").select("name").eq("id", g.claims["customer_id"]).execute().data
    co = sb.table("companies").select("name").eq("id", g.claims["company_id"]).execute().data
    return jsonify({
        "user": user[0],
        "customer": {"id": g.claims["customer_id"], "name": customer[0]["name"] if customer else ""},
        "company_name": co[0]["name"] if co else "",
    })


def list_sites():
    sb = get_sb()
    rows = sb.table("fsm_sites").select("id,name,address").eq("customer_id", g.claims["customer_id"]).execute().data or []
    return jsonify({"sites": rows})


def list_assets():
    site_id = request.args.get("site_id")
    if not site_id:
        return jsonify({"error": "site_id required"}), 400
    sb = get_sb()
    # Ownership check: the site must actually belong to this portal user's customer.
    site = sb.table("fsm_sites").select("id,customer_id").eq("id", site_id).execute().data
    if not site or site[0]["customer_id"] != g.claims["customer_id"]:
        return jsonify({"error": "not found"}), 404
    rows = sb.table("fsm_assets").select("id,asset_code,category").eq("site_id", site_id).eq("is_deleted", False).execute().data or []
    return jsonify({"assets": rows})


def list_tickets():
    sb = get_sb()
    rows = (
        sb.table("fsm_tickets")
        .select("id,ticket_number,subject,status,priority,ticket_type,created_at,resolved_at")
        .eq("customer_id", g.claims["customer_id"])
        .eq("is_deleted", False)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
        .data or []
    )
    return jsonify({"tickets": rows})


def get_ticket():
    ticket_id = request.args.get("id")
    if not ticket_id:
        return jsonify({"error": "id required"}), 400
    sb = get_sb()
    t = sb.table("fsm_tickets").select(
        "id,ticket_number,subject,description,status,priority,ticket_type,site_id,asset_id,"
        "created_at,first_response_at,arrived_at,resolved_at,closed_at,assigned_engineer_id"
    ).eq("id", ticket_id).eq("customer_id", g.claims["customer_id"]).eq("is_deleted", False).execute().data
    if not t:
        return jsonify({"error": "not found"}), 404
    t = t[0]
    engineer_name = None
    if t.get("assigned_engineer_id"):
        eng = sb.table("fsm_engineers").select("name").eq("id", t["assigned_engineer_id"]).execute().data
        engineer_name = eng[0]["name"] if eng else None
    t.pop("assigned_engineer_id", None)
    t["engineer_name"] = engineer_name
    return jsonify({"ticket": t})


def create_ticket(body):
    company_id = g.claims["company_id"]
    customer_id = g.claims["customer_id"]  # forced from session — never trust client input here
    subject = (body.get("subject") or "").strip()
    description = (body.get("description") or "").strip()
    site_id = body.get("site_id") or None
    asset_id = body.get("asset_id") or None
    ticket_type = body.get("ticket_type") or "service_request"

    if not subject:
        return jsonify({"error": "subject required"}), 400
    if ticket_type not in VALID_TICKET_TYPES:
        return jsonify({"error": f"ticket_type must be one of {sorted(VALID_TICKET_TYPES)}"}), 400

    sb = get_sb()

    # Everything past this point talks to Postgres/PostgREST — wrapped so a
    # schema mismatch or constraint violation comes back as a readable JSON
    # error instead of an opaque 500 (this endpoint previously had no
    # try/except at all, so any DB-level failure surfaced as a bare Flask
    # error page with no detail — this is what let the underlying bug here
    # go undiagnosed from the browser alone).
    try:
        if site_id:
            site = sb.table("fsm_sites").select("id,customer_id").eq("id", site_id).execute().data
            if not site or site[0]["customer_id"] != customer_id:
                return jsonify({"error": "invalid site"}), 400
        if asset_id:
            asset = sb.table("fsm_assets").select("id,site_id").eq("id", asset_id).execute().data
            if not asset or (site_id and asset[0]["site_id"] != site_id):
                return jsonify({"error": "invalid asset"}), 400

        year = datetime.utcnow().year
        result = sb.rpc("fsm_next_ticket_number", {"p_company_id": company_id, "p_year": year}).execute()
        seq = result.data
        if isinstance(seq, list):
            seq = seq[0].get("fsm_next_ticket_number") if seq else 1
        ticket_number = f"FSM-{year}-{seq:04d}"

        ticket_id = str(uuid.uuid4())
        record = {
            "id": ticket_id,
            "company_id": company_id,
            "ticket_number": ticket_number,
            "site_id": site_id,
            "asset_id": asset_id,
            "customer_id": customer_id,
            "ticket_type": ticket_type,
            "priority": "medium",  # customers don't self-triage priority/critical — staff does via AI Suggest or manually
            "status": "new",
            "source": "customer_portal",
            "subject": subject,
            "description": description,
            "created_at": datetime.utcnow().isoformat(),
        }

        # Same contract lookup as the staff-side create_ticket (api/fsm_tickets.py)
        # — prefer a site-specific contract, fall back to customer-wide.
        contract = _find_applicable_contract(sb, company_id, customer_id, site_id)
        if contract:
            record["contract_id"] = contract["id"]
            created_dt = datetime.utcnow()
            if contract.get("sla_response_hours") is not None:
                record["sla_response_due_at"] = (created_dt + timedelta(hours=float(contract["sla_response_hours"]))).isoformat()
            if contract.get("sla_resolution_hours") is not None:
                record["sla_resolution_due_at"] = (created_dt + timedelta(hours=float(contract["sla_resolution_hours"]))).isoformat()
            ctype = contract.get("contract_type")
            if ctype == "warranty":
                record["billing_type"], record["billing_status"] = "warranty", "not_billable"
            elif ctype in ("amc", "cmc", "fully_comprehensive"):
                record["billing_type"], record["billing_status"] = "amc_included", "not_billable"
            else:
                record["billing_type"], record["billing_status"] = "time_material", "pending"
        else:
            record["billing_type"], record["billing_status"] = "chargeable_visit", "pending"

        row = sb.table("fsm_tickets").insert(record).execute().data[0]

        self_help_shown = bool(body.get("self_help_shown"))
        note = f"Ticket opened via the Customer Portal by {g.claims.get('portal_user_id')}"
        if self_help_shown:
            note += " (AI self-resolution tips were shown first and didn't resolve it)"
        sb.table("fsm_ticket_activity").insert({
            "id": str(uuid.uuid4()),
            "ticket_id": ticket_id,
            "company_id": company_id,
            "event_type": "ticket_created",
            "note": note,
            "metadata": {"source": "customer_portal", "self_help_shown": self_help_shown},
            "created_at": datetime.utcnow().isoformat(),
        }).execute()
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Could not create ticket: {e}"}), 500

    return jsonify({"ticket": row})


# ── Self-resolution tips (Gemini, grounded in the company's Knowledge Base) ──

SELF_HELP_PROMPT = """You are a friendly AV/ELV support assistant helping an end customer (not a technician) with a fault on their installed system — LED video wall, DSP/audio processor, control processor (Crestron/AMX/Extron), video wall processor, projector, or matrix switcher. They're about to submit a service ticket; your job is to see if there's anything safe and simple they can check themselves first, in plain non-technical language, before an engineer visit is needed.

Only suggest checks that are safe for a non-technical person: power/cable connections, restarting a device, checking an input/source selection, checking a remote's batteries, checking obvious display settings. NEVER suggest opening equipment, touching internal wiring, or anything requiring tools or technical training — if the fault sounds like it needs a technician (e.g. described as a hardware failure, burning smell, physical damage, or the customer already says they checked the basics), say so plainly and keep the list short or empty.

Ground your answer in the provided knowledge base excerpts when relevant; otherwise use general common-sense checks only.

Respond ONLY with valid JSON, no markdown fences, in exactly this shape:
{"can_self_check":true or false,"tips":["short plain-language step", ...max 4],"note":"one reassuring sentence, e.g. telling them it's fine to submit a ticket regardless"}"""


def _gemini(system_prompt, user_msg, max_tokens=800):
    if not GEMINI_API_KEY:
        return None, 'AI features are not configured yet.'
    body = json.dumps({
        'systemInstruction': {'parts': [{'text': system_prompt}]},
        'contents': [{'role': 'user', 'parts': [{'text': user_msg}]}],
        'generationConfig': {
            'temperature': 0.2, 'maxOutputTokens': max_tokens,
            'responseMimeType': 'application/json', 'thinkingConfig': {'thinkingBudget': 0},
        },
    }).encode('utf-8')
    req = urllib.request.Request(
        GEMINI_URL, data=body,
        headers={'Content-Type': 'application/json', 'x-goog-api-key': GEMINI_API_KEY},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None, 'AI is unavailable right now — you can still submit your ticket below.'
    try:
        parts = data['candidates'][0]['content']['parts']
        text = ''.join(p.get('text', '') for p in parts).strip()
    except (KeyError, IndexError):
        return None, 'AI is unavailable right now — you can still submit your ticket below.'
    if text.startswith('```'):
        text = text.strip('`').strip()
        if text.lower().startswith('json'):
            text = text[4:].strip()
    try:
        return json.loads(text), None
    except Exception:
        return None, 'AI is unavailable right now — you can still submit your ticket below.'


def self_help(body):
    company_id = g.claims["company_id"]
    customer_id = g.claims["customer_id"]
    subject = (body.get("subject") or "").strip()
    description = (body.get("description") or "").strip()
    asset_id = body.get("asset_id") or None

    if not subject and not description:
        return jsonify({"can_self_check": False, "tips": [], "note": ""})

    sb = get_sb()
    asset_category = ""
    if asset_id:
        asset = sb.table("fsm_assets").select("category,site_id").eq("id", asset_id).execute().data
        if asset:
            site = sb.table("fsm_sites").select("customer_id").eq("id", asset[0]["site_id"]).execute().data
            if site and site[0]["customer_id"] == customer_id:
                asset_category = asset[0].get("category") or ""

    search_terms = [t for t in (subject + " " + description).lower().split() if len(t) > 3][:8]
    kb_matches = []
    if search_terms or asset_category:
        rows = (
            sb.table("fsm_kb_articles")
            .select("title,category,content,asset_category")
            .eq("company_id", company_id)
            .in_("category", ["troubleshooting_guide", "faq", "known_error"])
            .eq("is_published", True)
            .limit(50)
            .execute()
            .data or []
        )
        for r in rows:
            hay = ((r.get("title") or "") + " " + (r.get("content") or "")).lower()
            score = sum(1 for t in search_terms if t in hay)
            if asset_category and r.get("asset_category") == asset_category:
                score += 2
            if score > 0:
                kb_matches.append((score, r))
        kb_matches.sort(key=lambda x: -x[0])
        kb_matches = [r for _, r in kb_matches[:3]]

    user_msg = json.dumps({
        "subject": subject, "description": description, "asset_category": asset_category,
        "knowledge_base_excerpts": [{"title": r["title"], "content": (r.get("content") or "")[:600]} for r in kb_matches],
    })
    result, err = _gemini(SELF_HELP_PROMPT, user_msg)
    if err or not result:
        return jsonify({"can_self_check": False, "tips": [], "note": err or ""})

    return jsonify({
        "can_self_check": bool(result.get("can_self_check")),
        "tips": [str(x)[:200] for x in (result.get("tips") or [])[:4]],
        "note": str(result.get("note", ""))[:300],
    })


def _find_applicable_contract(sb, company_id, customer_id, site_id):
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
