"""
api/vouchers.py — Payment Vouchers (merged in from the standalone sysconic-pv app)

Approval used to be driven by a hardcoded VOUCHER_APPROVER_EMAILS env var
(comma-separated, resolved to user rows on every submission) and its own
voucher_approvals table + magic-link token machinery. Both have been retired
in favor of api/workflows.py's Configurable Workflow Engine: submitting a
voucher now just calls start_workflow_instance(doc_key='vouchers'), and the
engine handles picking approvers (configured per-company in Settings ->
Workflow Settings, no redeploy needed), emailing magic links, and syncing
the outcome back onto vouchers.status via DOCUMENT_STATUS_SYNC. See
api/workflows.py's module docstring for why this file and it share a trust
boundary (direct function import, not an HTTP round trip).

NOTE: for a submitted voucher to actually get approvers, an admin must have
enabled "Payment Vouchers" in Settings -> Workflow Settings and configured
at least one stage to require approval with real approvers picked -- the
auto-seeded default ("Draft" -> "Approved") means the *first* stage doesn't
require approval, so an admin should delete that Draft stage (or mark it as
requiring approval) so a submitted voucher doesn't just sit un-actioned.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import os, jwt, traceback
from datetime import datetime
from supabase import create_client

from api.workflows import start_workflow_instance, get_workflow_instances_bulk, force_resolve_instance

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


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
    claims = verify_token(request)
    if not claims:
        return None
    if request.method == 'GET':
        return None
    if not has_page_access(claims, 'vouchers'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    return None


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
    q = sb.table('vouchers').select('*').eq('company_id', claims['company_id'])
    if status:
        q = q.eq('status', status)
    vouchers = q.order('created_at', desc=True).execute().data

    # Approvals now live in the workflow engine's tables, keyed by
    # (doc_key='vouchers', document_id=voucher.id) -- fetch all of them in
    # one batch rather than one query per voucher.
    instances = get_workflow_instances_bulk(sb, claims['company_id'], 'vouchers', [v['id'] for v in vouchers])
    for v in vouchers:
        inst = instances.get(v['id'])
        v['workflow_approvals'] = inst['approvals'] if inst else []
    return jsonify({'vouchers': vouchers})


# ── Create voucher (submits for approval via the Workflow Engine) ───────────
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

    meta = {
        'Payee': voucher.get('payee', ''),
        'Amount': f"{voucher.get('currency', 'AED')} {voucher.get('amount', '')}",
        'Category': voucher.get('category', ''),
        'Project': voucher.get('project_name_freeform', '') or '',
    }
    result = start_workflow_instance(sb, claims['company_id'], 'vouchers', voucher['id'], meta)
    warning = None
    if 'error' in result:
        # Workflow not configured yet -- the voucher is still submitted (so
        # nothing is lost), but nobody got emailed, so flag that clearly
        # instead of silently leaving it stuck in 'pending' forever.
        warning = f"Submitted, but no approval workflow is set up for Payment Vouchers yet ({result['error']}). Ask an admin to configure it in Settings → Workflow Settings."

    return jsonify({'voucher': voucher, 'warning': warning}), 201


# ── Admin force-override (bypasses whichever approvers haven't responded) ───
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
    voucher = sb.table('vouchers').select('id').eq('id', vid).eq('company_id', claims['company_id']).execute().data
    if not voucher:
        return jsonify({'error': 'Not found'}), 404
    result = force_resolve_instance(sb, claims['company_id'], 'vouchers', vid, decision)
    if 'error' in result:
        # No workflow instance exists for this voucher (e.g. it was
        # submitted before the workflow was configured) -- fall back to a
        # direct status flip so the override still works.
        new_status = 'approved' if decision == 'approve' else 'rejected'
        sb.table('vouchers').update({'status': new_status, 'updated_at': datetime.utcnow().isoformat()}).eq('id', vid).execute()
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
