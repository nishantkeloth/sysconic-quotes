from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import os, jwt, traceback, requests
from datetime import datetime, timedelta, date
from supabase import create_client

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')
MS_TENANT_ID = os.environ.get('MS_TENANT_ID', 'b36855d2-9d26-43a4-bec6-82268a7713fb')
MS_CLIENT_ID = os.environ.get('MS_CLIENT_ID', '491f22c7-9dee-4c30-b828-acf8ba8d948c')
MS_CLIENT_SECRET = os.environ.get('MS_CLIENT_SECRET')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'nishant@sysconic.com')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# Comma-separated list of approver emails, in the order they must all sign off,
# e.g. "rajesh@sysconic.com,ajith@sysconic.com,nishant@sysconic.com". Ported
# from PV's hardcoded RAJESH_EMAIL/AJITH_EMAIL/NISHANT_EMAIL env vars -- kept
# as one env var here rather than a UI so this ships without a new settings
# screen. Fast-follow: move this into company_integrations (provider=
# 'voucher_approvers') so it's editable per-company from Settings, the same
# way Twilio/WhatsApp config would live there if that gets wired up later
# (see api/fsm_tickets.py's TODO -- WhatsApp isn't implemented anywhere in
# this app yet, so voucher notifications are email-only for now, same as
# everything else here).
VOUCHER_APPROVER_EMAILS = [e.strip() for e in os.environ.get('VOUCHER_APPROVER_EMAILS', '').split(',') if e.strip()]

ACTION_TOKEN_TTL_HOURS = 168  # 7 days, matches PV's magic-link expectation


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    return jsonify({'error': str(e)}), 500


def verify_token(req):
    auth = req.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except Exception:
        return None


def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))


@app.before_request
def _rbac_page_gate():
    # Action-link endpoints are deliberately excluded -- approvers click these
    # straight from an email/WhatsApp message without logging into QtCal at
    # all, same as PV's /action/<token>/<decision> today.
    if request.path.startswith('/api/vouchers/action/'):
        return None
    claims = verify_token(request)
    if not claims:
        return None
    if request.method == 'GET':
        return None
    if not has_page_access(claims, 'vouchers'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    return None


def get_ms_token():
    url = f'https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token'
    data = {
        'client_id': MS_CLIENT_ID,
        'client_secret': MS_CLIENT_SECRET,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials',
    }
    r = requests.post(url, data=data)
    return r.json().get('access_token')


def send_approval_email(to_email, voucher, approve_url, reject_url):
    try:
        token = get_ms_token()
        if not token:
            print('Failed to get MS token')
            return False
        subject = f"Payment voucher approval needed — {voucher.get('payee')} ({voucher.get('currency', 'AED')} {voucher.get('amount')})"
        body = f"""
        <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1a1a1a;max-width:600px;margin:0 auto;padding:20px">
            <div style="background:#1a3c6e;padding:24px;border-radius:8px 8px 0 0;text-align:center">
                <div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:.02em">SYSCONIC</div>
                <div style="font-size:12px;color:rgba(255,255,255,.6);margin-top:4px">Payment Voucher Approval</div>
            </div>
            <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:32px">
                <h2 style="color:#1a3c6e;margin:0 0 16px">Approval requested</h2>
                <table style="width:100%;font-size:14px;color:#333;margin-bottom:24px;border-collapse:collapse">
                    <tr><td style="padding:4px 0;color:#888">Payee</td><td style="padding:4px 0;text-align:right"><strong>{voucher.get('payee', '')}</strong></td></tr>
                    <tr><td style="padding:4px 0;color:#888">Amount</td><td style="padding:4px 0;text-align:right"><strong>{voucher.get('currency', 'AED')} {voucher.get('amount', '')}</strong></td></tr>
                    <tr><td style="padding:4px 0;color:#888">Category</td><td style="padding:4px 0;text-align:right">{voucher.get('category', '')}</td></tr>
                    <tr><td style="padding:4px 0;color:#888">Project</td><td style="padding:4px 0;text-align:right">{voucher.get('project_name_freeform', '') or ''}</td></tr>
                </table>
                <div style="text-align:center;margin-bottom:12px">
                    <a href="{approve_url}" style="background:#1a7f4b;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-size:15px;font-weight:700;display:inline-block;margin:0 6px">Approve</a>
                    <a href="{reject_url}" style="background:#a33;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-size:15px;font-weight:700;display:inline-block;margin:0 6px">Reject</a>
                </div>
                <p style="font-size:12px;color:#aaa;margin:0">This link expires in 7 days and can only be used once.</p>
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
            f'https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail',
            json=payload,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        )
        return r.status_code == 202
    except Exception as e:
        print(f'Voucher approval email error: {e}')
        return False


def make_action_token(voucher_id, approval_id):
    return jwt.encode(
        {
            'type': 'voucher_action',
            'voucher_id': voucher_id,
            'approval_id': approval_id,
            'exp': datetime.utcnow() + timedelta(hours=ACTION_TOKEN_TTL_HOURS),
        },
        JWT_SECRET,
        algorithm='HS256',
    )


def get_app_url():
    override = os.environ.get('APP_URL')
    if override:
        return override.rstrip('/')
    return request.url_root.rstrip('/')


def clean_voucher(d):
    out = {}
    for k in ('payee', 'currency', 'payment_method', 'category', 'invoice_no', 'remarks', 'project_id', 'project_name_freeform'):
        if k in d and d[k] is not None:
            out[k] = str(d[k]).strip()[:500] if k != 'project_id' else d[k]
    if 'amount' in d:
        try:
            out['amount'] = float(d['amount'])
        except (TypeError, ValueError):
            pass
    if d.get('due_date'):
        out['due_date'] = d['due_date']  # expects 'YYYY-MM-DD' from the date input
    return out


# ── List vouchers ────────────────────────────────────────────────────────────
@app.route('/api/vouchers', methods=['GET'])
def list_vouchers():
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'Unauthorized'}), 401
    status = request.args.get('status')
    q = sb.table('vouchers').select('*,voucher_approvals(*)').eq('company_id', claims['company_id'])
    if status:
        q = q.eq('status', status)
    rows = q.order('created_at', desc=True).execute()
    return jsonify({'vouchers': rows.data})


# ── Create voucher (submits for approval) ────────────────────────────────────
@app.route('/api/vouchers', methods=['POST'])
def create_voucher():
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'Unauthorized'}), 401
    d = clean_voucher(request.json or {})
    if not d.get('payee') or not d.get('amount') or not d.get('category'):
        return jsonify({'error': 'Payee, amount, and category are required'}), 400
    d['company_id'] = claims['company_id']
    d['submitted_by'] = claims['user_id']
    d['status'] = 'pending'
    voucher = sb.table('vouchers').insert(d).execute().data[0]

    # Resolve configured approver emails to real user rows in this company,
    # create one voucher_approvals row each, and email each a one-click
    # approve/reject link.
    approvers = []
    if VOUCHER_APPROVER_EMAILS:
        users = sb.table('users').select('id,email,name').eq('company_id', claims['company_id']).in_('email', VOUCHER_APPROVER_EMAILS).execute().data
        approvers = users
    for u in approvers:
        approval = sb.table('voucher_approvals').insert({
            'voucher_id': voucher['id'],
            'approver_user_id': u['id'],
            'status': 'pending',
        }).execute().data[0]
        token = make_action_token(voucher['id'], approval['id'])
        sb.table('voucher_approvals').update({'token': token}).eq('id', approval['id']).execute()
        base = get_app_url()
        approve_url = f"{base}/api/vouchers/action/{token}/approve"
        reject_url = f"{base}/api/vouchers/action/{token}/reject"
        send_approval_email(u['email'], voucher, approve_url, reject_url)

    return jsonify({'voucher': voucher}), 201


def _apply_approval_decision(approval, decision):
    """Shared by the emailed magic-link route and the in-app respond route.
    Updates one voucher_approvals row, then recomputes the parent voucher's
    status: a single rejection kills it; it only becomes fully approved once
    every approval row is 'approved' (parallel sign-off, same as PV's
    rajesh_status/ajith_status/nishant_status columns). Returns the new
    voucher-level status."""
    new_status = 'approved' if decision == 'approve' else 'rejected'
    sb.table('voucher_approvals').update({
        'status': new_status,
        'responded_at': datetime.utcnow().isoformat(),
    }).eq('id', approval['id']).execute()

    all_approvals = sb.table('voucher_approvals').select('status').eq('voucher_id', approval['voucher_id']).execute().data
    if any(a['status'] == 'rejected' for a in all_approvals):
        voucher_status = 'rejected'
    elif all(a['status'] == 'approved' for a in all_approvals):
        voucher_status = 'approved'
    else:
        voucher_status = 'pending'
    sb.table('vouchers').update({'status': voucher_status, 'updated_at': datetime.utcnow().isoformat()}).eq('id', approval['voucher_id']).execute()
    return voucher_status


# ── Approve / reject via emailed magic link -- no login required ────────────
@app.route('/api/vouchers/action/<token>/<decision>', methods=['GET'])
def voucher_action(token, decision):
    if decision not in ('approve', 'reject'):
        return "Invalid action.", 400
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        if payload.get('type') != 'voucher_action':
            raise ValueError('wrong token type')
    except Exception:
        return "This link has expired or is invalid.", 400

    approval = sb.table('voucher_approvals').select('*').eq('id', payload['approval_id']).execute().data
    if not approval:
        return "This link is no longer valid.", 400
    approval = approval[0]
    if approval['status'] != 'pending':
        return f"This voucher was already marked '{approval['status']}'.", 200

    voucher_status = _apply_approval_decision(approval, decision)
    new_status = 'approved' if decision == 'approve' else 'rejected'
    return f"Thanks — recorded as {new_status}. This voucher is currently {voucher_status}.", 200


# ── Approve / reject from inside the app (for approvers who are already
# logged in rather than clicking the emailed link) ──────────────────────────
@app.route('/api/vouchers/<vid>/respond', methods=['POST'])
def voucher_respond(vid):
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'Unauthorized'}), 401
    decision = (request.json or {}).get('decision')
    if decision not in ('approve', 'reject'):
        return jsonify({'error': "decision must be 'approve' or 'reject'"}), 400
    approval = sb.table('voucher_approvals').select('*').eq('voucher_id', vid).eq('approver_user_id', claims['user_id']).execute().data
    if not approval:
        return jsonify({'error': "You are not listed as an approver on this voucher"}), 403
    approval = approval[0]
    if approval['status'] != 'pending':
        return jsonify({'error': f"You already marked this '{approval['status']}'"}), 400
    voucher_status = _apply_approval_decision(approval, decision)
    return jsonify({'success': True, 'voucher_status': voucher_status})


# ── Admin force-override (replaces PV's 4-digit PIN with a real role check) ──
@app.route('/api/vouchers/<vid>/force', methods=['POST'])
def force_voucher(vid):
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'Unauthorized'}), 401
    if claims.get('role') != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    decision = (request.json or {}).get('decision')
    if decision not in ('approve', 'reject'):
        return jsonify({'error': "decision must be 'approve' or 'reject'"}), 400
    new_status = 'approved' if decision == 'approve' else 'rejected'
    sb.table('vouchers').update({'status': new_status, 'updated_at': datetime.utcnow().isoformat()}).eq('id', vid).eq('company_id', claims['company_id']).execute()
    return jsonify({'success': True})


# ── Delete (admin only) ──────────────────────────────────────────────────────
@app.route('/api/vouchers/<vid>', methods=['DELETE'])
def delete_voucher(vid):
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'Unauthorized'}), 401
    if claims.get('role') != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    sb.table('vouchers').delete().eq('id', vid).eq('company_id', claims['company_id']).execute()
    return jsonify({'success': True})
