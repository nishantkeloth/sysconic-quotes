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


PAYMENT_TYPES = {'project', 'general', 'other'}


def _project_budget_snapshot(sb, claims, project_id, extra_amount=0, extra_currency='AED', exclude_voucher_id=None):
    """Rough "how much of this project's budget is left" snapshot -- used to
    warn (never block) at submission time and to give approvers context
    before they click Approve. Never raises; returns None if the project
    can't be found.

    Budget = latest project_budget_revisions.revised_budget, falling back to
    projects.original_estimated_cost -- same precedence api/projects.py's
    pp_add_budget_revision() uses. "Committed" = Zoho actual/committed costs
    (same tables/filters as api/project_performance.py's
    recalculate_project(), read-only here so this has no side effects) when
    the project_performance feature is enabled, plus this project's own
    pending+approved vouchers (excluding exclude_voucher_id, so the voucher
    being evaluated isn't counted twice against extra_amount).

    Project financials have no currency column and are AED-only in practice;
    a voucher in another currency can't be netted in without an FX rate, so
    extra_amount is only subtracted when extra_currency == 'AED' --
    currency_mismatch flags the other case so callers can say so instead of
    silently showing a wrong number."""
    proj = sb.table('projects').select('id,original_estimated_cost').eq('id', project_id).execute().data
    if not proj:
        return None
    budget = proj[0].get('original_estimated_cost')
    rev = sb.table('project_budget_revisions').select('revised_budget') \
        .eq('project_id', project_id).order('revision_number', desc=True).limit(1).execute().data
    if rev:
        budget = rev[0].get('revised_budget')
    budget = float(budget) if budget is not None else None

    zoho_actual = zoho_committed = 0.0
    has_zoho_data = bool((claims.get('features') or {}).get('project_performance'))
    if has_zoho_data:
        bills = sb.table('zoho_bills').select('total').eq('project_id', project_id).execute().data or []
        expenses = sb.table('zoho_expenses').select('amount').eq('project_id', project_id).execute().data or []
        zoho_actual = sum(float(b.get('total') or 0) for b in bills) + sum(float(e.get('amount') or 0) for e in expenses)
        pos = sb.table('zoho_purchase_orders').select('total,billed_total,status').eq('project_id', project_id).execute().data or []
        zoho_committed = sum(
            max(0.0, float(po.get('total') or 0) - float(po.get('billed_total') or 0))
            for po in pos if (po.get('status') or '').lower() not in ('cancelled', 'void', 'deleted')
        )

    vq = sb.table('vouchers').select('id,amount,currency').eq('project_id', project_id).in_('status', ['pending', 'approved'])
    if exclude_voucher_id:
        vq = vq.neq('id', exclude_voucher_id)
    vouchers = vq.execute().data or []
    voucher_committed = sum(float(v.get('amount') or 0) for v in vouchers if (v.get('currency') or 'AED') == 'AED')

    total_committed = zoho_actual + zoho_committed + voucher_committed
    remaining_before = (budget - total_committed) if budget is not None else None
    currency_mismatch = extra_currency != 'AED'
    remaining_after = (remaining_before - extra_amount) if (remaining_before is not None and not currency_mismatch) else None

    return {
        'budget': budget, 'zoho_actual': zoho_actual, 'zoho_committed': zoho_committed,
        'voucher_committed': voucher_committed, 'total_committed': total_committed,
        'remaining_before': remaining_before, 'remaining_after': remaining_after,
        'over_budget': bool(remaining_after is not None and remaining_after < 0),
        'currency_mismatch': currency_mismatch, 'has_zoho_data': has_zoho_data,
    }


def clean_voucher(d):
    out = {}
    for k in ('payee', 'currency', 'payment_method', 'category', 'invoice_no', 'remarks', 'project_id', 'project_name_freeform', 'payment_type'):
        if k == 'project_id':
            # Comes from a <select> of real projects now -- an empty string
            # means "no project selected", which must be None, not '', or
            # the insert fails against the uuid column.
            if d.get(k):
                out[k] = d[k]
            continue
        if k == 'payment_type':
            out[k] = d[k] if d.get(k) in PAYMENT_TYPES else 'project'
            continue
        if k in d and d[k] is not None:
            out[k] = str(d[k]).strip()[:500]
    if 'amount' in d:
        try:
            out['amount'] = float(d['amount'])
        except (TypeError, ValueError):
            pass
    if d.get('due_date'):
        out['due_date'] = d['due_date']  # expects 'YYYY-MM-DD' from the date input
    if out.get('payment_type') != 'project':
        # A General/Admin or Other payment has no project -- ignore any
        # project_id that came through regardless (defense in depth; the
        # form already hides/clears the Project field for these types).
        out.pop('project_id', None)
    return out


# ── List vouchers ────────────────────────────────────────────────────────────
@app.route('/api/vouchers', methods=['GET'])
def list_vouchers():
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'Unauthorized'}), 401
    status = request.args.get('status')
    q = sb.table('vouchers').select('*,projects(name)').eq('company_id', claims['company_id'])
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
        # Budget context only for vouchers still awaiting a decision -- once
        # resolved it's history, and computing it costs a handful of extra
        # queries per voucher, so it's not worth doing for the whole list.
        if v.get('payment_type', 'project') == 'project' and v.get('project_id') and v.get('status') == 'pending':
            v['budget_snapshot'] = _project_budget_snapshot(
                sb, claims, v['project_id'],
                extra_amount=v.get('amount') or 0, extra_currency=v.get('currency') or 'AED',
                exclude_voucher_id=v['id'],
            )
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

    project_name = voucher.get('project_name_freeform') or ''
    if not project_name and voucher.get('project_id'):
        # The New Voucher form now sends a real project_id (a <select> of
        # actual projects, not free text), so the human-readable name has to
        # be looked up separately for the approval email.
        proj = sb.table('projects').select('name').eq('id', voucher['project_id']).execute().data
        project_name = proj[0]['name'] if proj else ''
    meta = {
        'Payee': voucher.get('payee', ''),
        'Amount': f"{voucher.get('currency', 'AED')} {voucher.get('amount', '')}",
        'Category': voucher.get('category', ''),
        'Project': project_name,
    }

    # Budget check -- warns, never blocks. Only meaningful for project-linked
    # vouchers where the project actually has a budget set.
    budget_info = None
    if voucher.get('payment_type', 'project') == 'project' and voucher.get('project_id'):
        budget_info = _project_budget_snapshot(
            sb, claims, voucher['project_id'],
            extra_amount=voucher.get('amount') or 0, extra_currency=voucher.get('currency') or 'AED',
            exclude_voucher_id=voucher['id'],
        )
    if budget_info and budget_info['budget'] is not None:
        meta['Project Budget'] = f"AED {budget_info['budget']:,.2f}"
        meta['Already Committed'] = f"AED {budget_info['total_committed']:,.2f}" + (' (incl. Zoho actuals/POs)' if budget_info['has_zoho_data'] else ' (approved + pending vouchers)')
        if budget_info['currency_mismatch']:
            meta['Remaining (before this voucher)'] = f"AED {budget_info['remaining_before']:,.2f} — voucher currency differs, not netted in"
        else:
            flag = ' — OVER BUDGET' if budget_info['over_budget'] else ''
            meta['Remaining After This Voucher'] = f"AED {budget_info['remaining_after']:,.2f}{flag}"

    result = start_workflow_instance(sb, claims['company_id'], 'vouchers', voucher['id'], meta)
    warnings = []
    if 'error' in result:
        # Workflow not configured yet -- the voucher is still submitted (so
        # nothing is lost), but nobody got emailed, so flag that clearly
        # instead of silently leaving it stuck in 'pending' forever.
        warnings.append(f"Submitted, but no approval workflow is set up for Payment Vouchers yet ({result['error']}). Ask an admin to configure it in Settings → Workflow Settings.")
    if budget_info and budget_info['over_budget']:
        warnings.append(f"Heads up: this voucher would leave the project AED {abs(budget_info['remaining_after']):,.2f} over its remaining budget. It's still been submitted -- approvers will see this flagged.")

    return jsonify({'voucher': voucher, 'warning': ' '.join(warnings) if warnings else None}), 201


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
    result = force_resolve_instance(sb, claims['company_id'], 'vouchers', vid, decision, acted_by_user_id=claims['user_id'])
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
