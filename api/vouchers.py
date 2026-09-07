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
import os, jwt, traceback, base64, time, re
from datetime import datetime
from supabase import create_client

from api.workflows import start_workflow_instance, get_workflow_instances_bulk, force_resolve_instance

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# Receipt/invoice attachments -- same private-bucket + signed-URL pattern as
# api/products.py's product-images and api/auth.py's company-assets (see
# _signed_url() there for the full rationale). Images or PDF, 4MB cap to
# stay comfortably under Vercel's default request body limit once
# base64-encoded (~33% bigger than the raw file).
RECEIPTS_BUCKET = 'voucher-receipts'
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
SIGNED_URL_EXPIRES_SECONDS = 10 * 365 * 24 * 60 * 60  # ~10 years, same as products.py
RECEIPT_EXT_MAP = {
    'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif',
    'application/pdf': 'pdf',
}


def _signed_url(bucket, path, expires_in=SIGNED_URL_EXPIRES_SECONDS):
    """Copied from api/products.py -- see its comment for why this defensively
    handles varying supabase-py response-key naming and bare-path results."""
    res = sb.storage.from_(bucket).create_signed_url(path, expires_in)
    url = res.get('signedURL') or res.get('signedUrl') or res.get('signed_url') or res.get('url')
    if not url:
        raise RuntimeError(f'Unexpected create_signed_url() response shape: {res!r}')
    if url.startswith('http://') or url.startswith('https://'):
        return url
    return f"{SUPABASE_URL}/storage/v1{url}"


def _extract_storage_path(url, bucket):
    """Copied from api/products.py -- recovers the object path from an
    already-stored signed URL so a replaced/removed receipt's old file can
    actually be deleted from storage, not just unlinked in the DB."""
    if not url:
        return None
    m = re.search(rf'/{re.escape(bucket)}/([^?]+)', url)
    return m.group(1) if m else None


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


def _project_budget_snapshots_bulk(sb, claims, vouchers):
    """Batched version of _project_budget_snapshot for the Vouchers list page.

    The original per-voucher version did up to 6 queries *per pending
    project-linked voucher* (project lookup, budget revision, 3 Zoho tables,
    vouchers-in-progress) -- fine for a single submission, but with N pending
    vouchers on screen that's N*6 sequential round trips, which is exactly
    why the list was slow to load. This does the same computation with a
    fixed handful of queries regardless of how many vouchers are in the
    list: one per distinct project_id involved, not one per voucher.

    Returns {voucher_id: snapshot_dict} for every pending, project-linked
    voucher in `vouchers` whose project could be found."""
    targets = [v for v in vouchers if v.get('payment_type', 'project') == 'project' and v.get('project_id') and v.get('status') == 'pending']
    if not targets:
        return {}
    project_ids = list({v['project_id'] for v in targets})

    projects = sb.table('projects').select('id,original_estimated_cost').in_('id', project_ids).execute().data or []
    budget_by_project = {p['id']: (float(p['original_estimated_cost']) if p.get('original_estimated_cost') is not None else None) for p in projects}

    # Latest revision per project -- rows come back ordered newest-first, so
    # the first one seen per project_id wins; older ones for the same
    # project are just skipped.
    revisions = sb.table('project_budget_revisions').select('project_id,revised_budget,revision_number') \
        .in_('project_id', project_ids).order('revision_number', desc=True).execute().data or []
    seen_rev = set()
    for r in revisions:
        pid = r['project_id']
        if pid in seen_rev:
            continue
        seen_rev.add(pid)
        if r.get('revised_budget') is not None:
            budget_by_project[pid] = float(r['revised_budget'])

    has_zoho_data = bool((claims.get('features') or {}).get('project_performance'))
    zoho_actual_by_project, zoho_committed_by_project = {}, {}
    if has_zoho_data:
        bills = sb.table('zoho_bills').select('project_id,total').in_('project_id', project_ids).execute().data or []
        expenses = sb.table('zoho_expenses').select('project_id,amount').in_('project_id', project_ids).execute().data or []
        for b in bills:
            zoho_actual_by_project[b['project_id']] = zoho_actual_by_project.get(b['project_id'], 0.0) + float(b.get('total') or 0)
        for e in expenses:
            zoho_actual_by_project[e['project_id']] = zoho_actual_by_project.get(e['project_id'], 0.0) + float(e.get('amount') or 0)
        pos = sb.table('zoho_purchase_orders').select('project_id,total,billed_total,status').in_('project_id', project_ids).execute().data or []
        for po in pos:
            if (po.get('status') or '').lower() in ('cancelled', 'void', 'deleted'):
                continue
            pid = po['project_id']
            zoho_committed_by_project[pid] = zoho_committed_by_project.get(pid, 0.0) + max(0.0, float(po.get('total') or 0) - float(po.get('billed_total') or 0))

    all_vouchers = sb.table('vouchers').select('id,project_id,amount,currency') \
        .in_('project_id', project_ids).in_('status', ['pending', 'approved']).execute().data or []
    voucher_sum_by_project = {}
    for vc in all_vouchers:
        if (vc.get('currency') or 'AED') != 'AED':
            continue
        pid = vc['project_id']
        voucher_sum_by_project[pid] = voucher_sum_by_project.get(pid, 0.0) + float(vc.get('amount') or 0)

    out = {}
    for v in targets:
        pid = v['project_id']
        budget = budget_by_project.get(pid)
        zoho_actual = zoho_actual_by_project.get(pid, 0.0)
        zoho_committed = zoho_committed_by_project.get(pid, 0.0)
        currency_mismatch = (v.get('currency') or 'AED') != 'AED'
        this_amount = float(v.get('amount') or 0)
        # voucher_sum_by_project includes this voucher's own row -- back it
        # out so "committed by others" doesn't double-count it.
        voucher_committed = voucher_sum_by_project.get(pid, 0.0) - (this_amount if not currency_mismatch else 0.0)
        total_committed = zoho_actual + zoho_committed + voucher_committed
        remaining_before = (budget - total_committed) if budget is not None else None
        remaining_after = (remaining_before - this_amount) if (remaining_before is not None and not currency_mismatch) else None
        out[v['id']] = {
            'budget': budget, 'zoho_actual': zoho_actual, 'zoho_committed': zoho_committed,
            'voucher_committed': voucher_committed, 'total_committed': total_committed,
            'remaining_before': remaining_before, 'remaining_after': remaining_after,
            'over_budget': bool(remaining_after is not None and remaining_after < 0),
            'currency_mismatch': currency_mismatch, 'has_zoho_data': has_zoho_data,
        }
    return out


def clean_voucher(d):
    out = {}
    for k in ('payee', 'currency', 'payment_method', 'category', 'invoice_no', 'remarks', 'project_id', 'project_name_freeform', 'payment_type', 'bank_name', 'bank_account', 'cost_center'):
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
    if d.get('vat_amount') not in (None, ''):
        try:
            out['vat_amount'] = float(d['vat_amount'])
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
    # submitter:users(name) resolves submitted_by to a display name -- the
    # only FK from vouchers to users, so no relationship hint is needed.
    q = sb.table('vouchers').select('*,projects(name),submitter:users(name)').eq('company_id', claims['company_id'])
    if status:
        q = q.eq('status', status)
    vouchers = q.order('created_at', desc=True).execute().data

    # Approvals now live in the workflow engine's tables, keyed by
    # (doc_key='vouchers', document_id=voucher.id) -- fetch all of them in
    # one batch rather than one query per voucher.
    instances = get_workflow_instances_bulk(sb, claims['company_id'], 'vouchers', [v['id'] for v in vouchers])
    budget_snapshots = _project_budget_snapshots_bulk(sb, claims, vouchers)
    for v in vouchers:
        inst = instances.get(v['id'])
        v['workflow_approvals'] = inst['approvals'] if inst else []
        if v['id'] in budget_snapshots:
            v['budget_snapshot'] = budget_snapshots[v['id']]
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


# ── Receipt / invoice attachment ────────────────────────────────────────────
@app.route('/api/vouchers/<vid>/receipt', methods=['POST'])
def set_receipt(vid):
    """Same base64-JSON-body upload pattern as api/products.py's set_image()
    and api/auth.py's company-profile logo upload -- see RECEIPTS_BUCKET's
    comment above for why. Called as a follow-up request right after a
    voucher is created (the New Voucher form doesn't block submission on
    this; a failed upload just means no receipt got attached)."""
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'Unauthorized'}), 401
    existing = sb.table('vouchers').select('id,receipt_url').eq('id', vid).eq('company_id', claims['company_id']).execute().data
    if not existing:
        return jsonify({'error': 'Not found'}), 404

    d = request.json or {}
    raw = str(d.get('data') or '')
    if not raw:
        return jsonify({'error': 'No file data provided'}), 400
    if ',' in raw:
        raw = raw.split(',', 1)[1]  # strip data: prefix
    try:
        data = base64.b64decode(raw)
    except Exception:
        return jsonify({'error': 'Invalid file data'}), 400

    ctype = str(d.get('content_type') or '').split(';')[0].strip().lower()
    ext = RECEIPT_EXT_MAP.get(ctype)
    if not ext:
        return jsonify({'error': f'Unsupported file type ({ctype or "unknown"}). Use JPG, PNG, WEBP, GIF or PDF.'}), 400
    if len(data) > MAX_RECEIPT_BYTES:
        return jsonify({'error': 'File is larger than 4 MB. Please use a smaller file.'}), 400
    if len(data) < 50:
        return jsonify({'error': 'That does not look like a valid file.'}), 400

    old_path = _extract_storage_path(existing[0].get('receipt_url'), RECEIPTS_BUCKET)
    path = f"{claims['company_id']}/{vid}-{int(time.time())}.{ext}"
    try:
        sb.storage.from_(RECEIPTS_BUCKET).upload(path, data, {'content-type': ctype})
    except Exception:
        return jsonify({'error': 'Could not save file to storage. Check that the voucher-receipts bucket exists.'}), 502
    try:
        signed_url = _signed_url(RECEIPTS_BUCKET, path)
    except Exception as e:
        return jsonify({'error': f'File uploaded but could not generate its access URL: {str(e)[:200]}'}), 502

    if old_path:
        try:
            sb.storage.from_(RECEIPTS_BUCKET).remove([old_path])
        except Exception:
            pass  # best-effort -- don't block the DB update if cleanup of the old file fails

    row = sb.table('vouchers').update({'receipt_url': signed_url, 'updated_at': datetime.utcnow().isoformat()}).eq('id', vid).execute()
    return jsonify({'voucher': row.data[0]})


@app.route('/api/vouchers/<vid>/receipt', methods=['DELETE'])
def remove_receipt(vid):
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'Unauthorized'}), 401
    existing = sb.table('vouchers').select('id,receipt_url').eq('id', vid).eq('company_id', claims['company_id']).execute().data
    if not existing:
        return jsonify({'error': 'Not found'}), 404
    old_path = _extract_storage_path(existing[0].get('receipt_url'), RECEIPTS_BUCKET)
    if old_path:
        try:
            sb.storage.from_(RECEIPTS_BUCKET).remove([old_path])
        except Exception:
            pass
    row = sb.table('vouchers').update({'receipt_url': None, 'updated_at': datetime.utcnow().isoformat()}).eq('id', vid).execute()
    return jsonify({'voucher': row.data[0]})
