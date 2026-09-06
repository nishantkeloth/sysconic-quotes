"""
api/workflows.py — Configurable Workflow Engine

Generalizes the per-document approval patterns already in Quotes
(quote_versions/quote_version_reviewers) and Payment Vouchers
(vouchers/voucher_approvals) into one reusable engine: each document type
can have an admin-configured sequence of stages, each optionally requiring
approval from one or more people before advancing, with an email
notification fired when a stage becomes active.

v1 scope: stage pipeline + approval gates + email-on-enter notifications.
'webhook' / 'update_field' action types are stored (workflow_stage_actions.
action_type) but not executed yet -- fast-follow once a first use case needs
them.

Deliberately layered ALONGSIDE existing status columns/logic in quotes.py /
delivery_challans.py / fsm_tickets.py / vouchers.py rather than replacing
them -- see migration-add-workflow-engine.sql's header comment. Document
types are admin-configurable (workflow_document_types), not hardcoded to
the four seeded ones.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import os, re, jwt, traceback, requests
from datetime import datetime, timedelta

from api.auth import verify_token, get_sb, get_ms_token, SENDER_EMAIL

app = Flask(__name__)

JWT_SECRET = os.environ.get('JWT_SECRET')
ACTION_TOKEN_TTL_HOURS = 168  # 7 days, matches api/vouchers.py's magic-link TTL

APPROVAL_MODES = {'any', 'all', 'sequential'}
ACTION_TRIGGERS = {'on_enter', 'on_approve', 'on_reject'}


@app.errorhandler(Exception)
def handle_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    return jsonify({'error': str(e)}), 500


def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))


def has_feature(claims, feature):
    return bool((claims.get('features') or {}).get(feature))


@app.before_request
def rbac_gate():
    # Magic-link approval endpoints are hit straight from an email with no
    # login at all, same as api/vouchers.py's /action/<token>/<decision>.
    if request.path.startswith('/api/workflows/action/'):
        return None
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'unauthorized'}), 401
    if not has_feature(claims, 'workflowEngine'):
        return jsonify({'error': 'Workflow Engine is not enabled for this company'}), 403
    g.claims = claims
    return None


def _is_admin():
    return g.claims.get('role') == 'admin'


def _require_settings_access():
    """Config (document types / stages / approvers / actions) is a settings
    surface -- viewable by anyone with the 'workflowSettings' page
    permission, but only mutable by a real admin (checked separately in each
    mutating route), same split used by api/vouchers.py's force/delete."""
    if not has_page_access(g.claims, 'workflowSettings'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    return None


def get_app_url():
    override = os.environ.get('APP_URL')
    if override:
        return override.rstrip('/')
    return request.url_root.rstrip('/')


def _slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-') or 'stage'


# ── Config: document types ──────────────────────────────────────────────────
@app.route('/api/workflows/document-types', methods=['GET'])
def list_document_types():
    err = _require_settings_access()
    if err:
        return err
    sb = get_sb()
    rows = sb.table('workflow_document_types').select('*,workflow_definitions(id)') \
        .eq('company_id', g.claims['company_id']).order('label').execute().data
    for r in rows:
        defs = r.pop('workflow_definitions', None) or []
        r['definition_id'] = defs[0]['id'] if defs else None
    return jsonify({'document_types': rows})


@app.route('/api/workflows/document-types/<dtid>', methods=['PUT'])
def update_document_type(dtid):
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    sb = get_sb()
    dt = sb.table('workflow_document_types').select('*').eq('id', dtid).eq('company_id', g.claims['company_id']).execute().data
    if not dt:
        return jsonify({'error': 'Not found'}), 404
    dt = dt[0]
    enabled = bool((request.json or {}).get('enabled'))
    sb.table('workflow_document_types').update({'enabled': enabled}).eq('id', dtid).execute()

    definition_id = None
    if enabled:
        existing = sb.table('workflow_definitions').select('id').eq('document_type_id', dtid).execute().data
        if existing:
            definition_id = existing[0]['id']
        else:
            definition = sb.table('workflow_definitions').insert({
                'company_id': g.claims['company_id'],
                'document_type_id': dtid,
                'name': dt['label'] + ' workflow',
                'is_active': True,
            }).execute().data[0]
            definition_id = definition['id']
            # Seed a sensible two-stage default (Draft -> Approved) so a
            # freshly-enabled document type isn't an empty pipeline; admin
            # can rename/add/remove stages immediately after from Settings.
            sb.table('workflow_stages').insert([
                {'workflow_id': definition_id, 'seq': 0, 'key': 'draft', 'label': 'Draft', 'requires_approval': False},
                {'workflow_id': definition_id, 'seq': 1, 'key': 'approved', 'label': 'Approved', 'requires_approval': True, 'approval_mode': 'all'},
            ]).execute()
    return jsonify({'success': True, 'definition_id': definition_id})


# ── Config: definitions + nested stages/approvers/actions ──────────────────
def _load_definition(sb, definition_id, company_id):
    d = sb.table('workflow_definitions').select('*').eq('id', definition_id).eq('company_id', company_id).execute().data
    if not d:
        return None
    d = d[0]
    stages = sb.table('workflow_stages').select('*').eq('workflow_id', definition_id).order('seq').execute().data
    stage_ids = [s['id'] for s in stages]
    approvers = sb.table('workflow_stage_approvers').select('*,users(name,email)').in_('stage_id', stage_ids).execute().data if stage_ids else []
    actions = sb.table('workflow_stage_actions').select('*').in_('stage_id', stage_ids).execute().data if stage_ids else []
    for s in stages:
        s['approvers'] = [a for a in approvers if a['stage_id'] == s['id']]
        s['actions'] = [a for a in actions if a['stage_id'] == s['id']]
    d['stages'] = stages
    return d


@app.route('/api/workflows/definitions/<did>', methods=['GET'])
def get_definition(did):
    err = _require_settings_access()
    if err:
        return err
    sb = get_sb()
    d = _load_definition(sb, did, g.claims['company_id'])
    if not d:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'definition': d})


def _stage_in_company(sb, stage_id):
    """Confirms `stage_id` belongs to a definition owned by the caller's
    company before any mutation -- workflow_stages has no company_id column
    of its own, so this join is the tenant check."""
    row = sb.table('workflow_stages').select('*,workflow_definitions!inner(company_id)').eq('id', stage_id).execute().data
    if not row or row[0]['workflow_definitions']['company_id'] != g.claims['company_id']:
        return None
    return row[0]


@app.route('/api/workflows/definitions/<did>/stages', methods=['POST'])
def add_stage(did):
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    sb = get_sb()
    d = sb.table('workflow_definitions').select('id').eq('id', did).eq('company_id', g.claims['company_id']).execute().data
    if not d:
        return jsonify({'error': 'Not found'}), 404
    body = request.json or {}
    label = (body.get('label') or '').strip()
    if not label:
        return jsonify({'error': 'Label is required'}), 400
    existing = sb.table('workflow_stages').select('seq').eq('workflow_id', did).order('seq', desc=True).limit(1).execute().data
    next_seq = (existing[0]['seq'] + 1) if existing else 0
    stage = sb.table('workflow_stages').insert({
        'workflow_id': did, 'seq': next_seq, 'key': _slugify(label) + '-' + str(next_seq), 'label': label,
        'requires_approval': bool(body.get('requires_approval')),
        'approval_mode': body.get('approval_mode') if body.get('approval_mode') in APPROVAL_MODES else 'all',
    }).execute().data[0]
    return jsonify({'stage': stage}), 201


@app.route('/api/workflows/stages/<sid>', methods=['PUT'])
def update_stage(sid):
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    sb = get_sb()
    if not _stage_in_company(sb, sid):
        return jsonify({'error': 'Not found'}), 404
    body = request.json or {}
    patch = {}
    if 'label' in body and str(body['label']).strip():
        patch['label'] = str(body['label']).strip()
    if 'requires_approval' in body:
        patch['requires_approval'] = bool(body['requires_approval'])
    if 'approval_mode' in body and body['approval_mode'] in APPROVAL_MODES:
        patch['approval_mode'] = body['approval_mode']
    if 'seq' in body:
        try:
            patch['seq'] = int(body['seq'])
        except (TypeError, ValueError):
            pass
    if patch:
        sb.table('workflow_stages').update(patch).eq('id', sid).execute()
    return jsonify({'success': True})


@app.route('/api/workflows/stages/<sid>', methods=['DELETE'])
def delete_stage(sid):
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    sb = get_sb()
    if not _stage_in_company(sb, sid):
        return jsonify({'error': 'Not found'}), 404
    sb.table('workflow_stages').delete().eq('id', sid).execute()
    return jsonify({'success': True})


@app.route('/api/workflows/stages/<sid>/approvers', methods=['POST'])
def add_stage_approver(sid):
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    sb = get_sb()
    if not _stage_in_company(sb, sid):
        return jsonify({'error': 'Not found'}), 404
    body = request.json or {}
    user_id, role = body.get('approver_user_id'), body.get('approver_role')
    if not user_id and not role:
        return jsonify({'error': 'approver_user_id or approver_role is required'}), 400
    row = sb.table('workflow_stage_approvers').insert({
        'stage_id': sid, 'approver_user_id': user_id, 'approver_role': role,
    }).execute().data[0]
    return jsonify({'approver': row}), 201


@app.route('/api/workflows/stage-approvers/<aid>', methods=['DELETE'])
def delete_stage_approver(aid):
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    sb = get_sb()
    row = sb.table('workflow_stage_approvers').select('*,workflow_stages!inner(workflow_definitions!inner(company_id))').eq('id', aid).execute().data
    if not row or row[0]['workflow_stages']['workflow_definitions']['company_id'] != g.claims['company_id']:
        return jsonify({'error': 'Not found'}), 404
    sb.table('workflow_stage_approvers').delete().eq('id', aid).execute()
    return jsonify({'success': True})


@app.route('/api/workflows/stages/<sid>/actions', methods=['POST'])
def add_stage_action(sid):
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    sb = get_sb()
    if not _stage_in_company(sb, sid):
        return jsonify({'error': 'Not found'}), 404
    body = request.json or {}
    trigger = body.get('trigger') if body.get('trigger') in ACTION_TRIGGERS else 'on_enter'
    row = sb.table('workflow_stage_actions').insert({
        'stage_id': sid, 'trigger': trigger, 'action_type': body.get('action_type') or 'notify_email',
        'config': body.get('config') or {},
    }).execute().data[0]
    return jsonify({'action': row}), 201


@app.route('/api/workflows/stage-actions/<aid>', methods=['DELETE'])
def delete_stage_action(aid):
    if not _is_admin():
        return jsonify({'error': 'Admin only'}), 403
    sb = get_sb()
    row = sb.table('workflow_stage_actions').select('*,workflow_stages!inner(workflow_definitions!inner(company_id))').eq('id', aid).execute().data
    if not row or row[0]['workflow_stages']['workflow_definitions']['company_id'] != g.claims['company_id']:
        return jsonify({'error': 'Not found'}), 404
    sb.table('workflow_stage_actions').delete().eq('id', aid).execute()
    return jsonify({'success': True})


@app.route('/api/workflows/company-users', methods=['GET'])
def company_users():
    err = _require_settings_access()
    if err:
        return err
    sb = get_sb()
    rows = sb.table('users').select('id,name,email').eq('company_id', g.claims['company_id']).order('name').execute().data
    return jsonify({'users': rows})


# ── Runtime: instances + approvals ──────────────────────────────────────────
def _active_definition_for_doc_key(sb, company_id, doc_key):
    dt = sb.table('workflow_document_types').select('id,enabled').eq('company_id', company_id).eq('doc_key', doc_key).execute().data
    if not dt or not dt[0]['enabled']:
        return None
    definition = sb.table('workflow_definitions').select('*').eq('document_type_id', dt[0]['id']).eq('is_active', True).execute().data
    return definition[0] if definition else None


def _stages_for_definition(sb, definition_id):
    return sb.table('workflow_stages').select('*').eq('workflow_id', definition_id).order('seq').execute().data


def make_action_token(instance_id, approval_id):
    return jwt.encode({
        'type': 'workflow_action', 'instance_id': instance_id, 'approval_id': approval_id,
        'exp': datetime.utcnow() + timedelta(hours=ACTION_TOKEN_TTL_HOURS),
    }, JWT_SECRET, algorithm='HS256')


def send_stage_email(to_email, subject, heading, meta, approve_url=None, reject_url=None):
    try:
        token = get_ms_token()
        if not token:
            print('Failed to get MS token')
            return False
        rows = ''.join(
            f'<tr><td style="padding:4px 0;color:#888">{k}</td><td style="padding:4px 0;text-align:right"><strong>{v}</strong></td></tr>'
            for k, v in (meta or {}).items()
        )
        actions = ''
        if approve_url and reject_url:
            actions = f'''<div style="text-align:center;margin-bottom:12px">
                <a href="{approve_url}" style="background:#1a7f4b;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-size:15px;font-weight:700;display:inline-block;margin:0 6px">Approve</a>
                <a href="{reject_url}" style="background:#a33;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-size:15px;font-weight:700;display:inline-block;margin:0 6px">Reject</a>
            </div>'''
        body = f"""
        <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1a1a1a;max-width:600px;margin:0 auto;padding:20px">
            <div style="background:#1a3c6e;padding:24px;border-radius:8px 8px 0 0;text-align:center">
                <div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:.02em">SYSCONIC</div>
                <div style="font-size:12px;color:rgba(255,255,255,.6);margin-top:4px">Workflow Notification</div>
            </div>
            <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:32px">
                <h2 style="color:#1a3c6e;margin:0 0 16px">{heading}</h2>
                <table style="width:100%;font-size:14px;color:#333;margin-bottom:24px;border-collapse:collapse">{rows}</table>
                {actions}
                <p style="font-size:12px;color:#aaa;margin:0">This link expires in 7 days and can only be used once.</p>
            </div>
        </body></html>
        """
        payload = {"message": {"subject": subject, "body": {"contentType": "HTML", "content": body},
                                "toRecipients": [{"emailAddress": {"address": to_email}}],
                                "from": {"emailAddress": {"address": SENDER_EMAIL}}}}
        r = requests.post(
            f'https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail',
            json=payload,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        )
        return r.status_code == 202
    except Exception as e:
        print(f'Workflow email error: {e}')
        return False


def _resolve_approver_user_ids(sb, company_id, stage):
    ids = set()
    for a in sb.table('workflow_stage_approvers').select('*').eq('stage_id', stage['id']).execute().data:
        if a.get('approver_user_id'):
            ids.add(a['approver_user_id'])
        elif a.get('approver_role') == 'admin':
            for u in sb.table('users').select('id').eq('company_id', company_id).eq('role', 'admin').execute().data:
                ids.add(u['id'])
    return ids


def _enter_stage(sb, instance, stage, meta):
    """Runs whenever an instance's current stage changes to `stage`:
    creates one approval row + emails each resolved approver if the stage
    requires approval, and fires any 'on_enter' notify_email actions
    regardless of whether approval is required."""
    sb.table('workflow_instances').update({
        'current_stage_id': stage['id'], 'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', instance['id']).execute()

    if stage.get('requires_approval'):
        approver_ids = list(_resolve_approver_user_ids(sb, instance['company_id'], stage))
        users = sb.table('users').select('id,email,name').eq('company_id', instance['company_id']).in_('id', approver_ids).execute().data if approver_ids else []
        for u in users:
            approval = sb.table('workflow_stage_approvals').insert({
                'instance_id': instance['id'], 'stage_id': stage['id'], 'approver_user_id': u['id'], 'status': 'pending',
            }).execute().data[0]
            token = make_action_token(instance['id'], approval['id'])
            sb.table('workflow_stage_approvals').update({'token': token}).eq('id', approval['id']).execute()
            base = get_app_url()
            approve_url = f"{base}/api/workflows/action/{token}/approve"
            reject_url = f"{base}/api/workflows/action/{token}/reject"
            send_stage_email(u['email'], f"Approval needed — {stage['label']}", f"Approval requested: {stage['label']}", meta, approve_url, reject_url)

    for act in sb.table('workflow_stage_actions').select('*').eq('stage_id', stage['id']).eq('trigger', 'on_enter').execute().data:
        if act['action_type'] == 'notify_email':
            to = (act.get('config') or {}).get('to') or ''
            if to.startswith('user:'):
                u = sb.table('users').select('email').eq('id', to.split(':', 1)[1]).execute().data
                if u:
                    send_stage_email(u[0]['email'], f"{stage['label']} — update", f"Document entered stage: {stage['label']}", meta)


def start_workflow_instance(sb, company_id, doc_key, document_id, meta=None):
    """Public entry point for other modules (e.g. api/vouchers.py) to kick
    off a workflow on submission, without going through HTTP -- both files
    run in the same trust boundary, so a direct call avoids an extra round
    trip and avoids having to fabricate a Bearer token. Returns
    {'instance': ...} or {'error': ...}; never raises for "not configured"
    since that's an expected, recoverable state (admin just hasn't set up
    this document type's workflow yet)."""
    meta = meta or {}
    dt = sb.table('workflow_document_types').select('*').eq('company_id', company_id).eq('doc_key', doc_key).execute().data
    if not dt or not dt[0]['enabled']:
        return {'error': f"Workflow is not enabled for '{doc_key}'"}
    definition = _active_definition_for_doc_key(sb, company_id, doc_key)
    if not definition:
        return {'error': 'No active workflow defined for this document type'}
    existing = sb.table('workflow_instances').select('*').eq('document_type_id', dt[0]['id']).eq('document_id', document_id).execute().data
    if existing:
        return {'instance': existing[0], 'already_existed': True}
    stages = _stages_for_definition(sb, definition['id'])
    if not stages:
        return {'error': 'This workflow has no stages configured yet'}
    instance = sb.table('workflow_instances').insert({
        'company_id': company_id, 'document_type_id': dt[0]['id'], 'document_id': document_id,
        'workflow_id': definition['id'], 'current_stage_id': stages[0]['id'], 'status': 'in_progress',
    }).execute().data[0]
    _enter_stage(sb, instance, stages[0], meta)
    return {'instance': instance}


@app.route('/api/workflows/instances', methods=['POST'])
def create_instance():
    sb = get_sb()
    body = request.json or {}
    result = start_workflow_instance(sb, g.claims['company_id'], body.get('doc_key'), body.get('document_id'), body.get('meta'))
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result), (200 if result.get('already_existed') else 201)


def get_workflow_instance(sb, company_id, doc_key, document_id):
    """Public entry point mirroring start_workflow_instance -- returns the
    instance (with nested stages + approvals) or None if none exists yet."""
    dt = sb.table('workflow_document_types').select('id').eq('company_id', company_id).eq('doc_key', doc_key).execute().data
    if not dt:
        return None
    inst = sb.table('workflow_instances').select('*').eq('document_type_id', dt[0]['id']).eq('document_id', document_id).execute().data
    if not inst:
        return None
    inst = inst[0]
    stages = _stages_for_definition(sb, inst['workflow_id'])
    approvals = sb.table('workflow_stage_approvals').select('id,stage_id,approver_user_id,status,responded_at,users(name,email)').eq('instance_id', inst['id']).execute().data
    for s in stages:
        s['approvals'] = [a for a in approvals if a['stage_id'] == s['id']]
    inst['stages'] = stages
    return inst


def get_workflow_instances_bulk(sb, company_id, doc_key, document_ids):
    """Batched version of get_workflow_instance for list pages -- returns
    {document_id: instance_with_approvals} for every id that has an
    instance, in 2 queries total instead of N. Approvals only (no nested
    stages) since list rows just need a status summary, not the full
    pipeline."""
    if not document_ids:
        return {}
    dt = sb.table('workflow_document_types').select('id').eq('company_id', company_id).eq('doc_key', doc_key).execute().data
    if not dt:
        return {}
    instances = sb.table('workflow_instances').select('*').eq('document_type_id', dt[0]['id']).in_('document_id', document_ids).execute().data
    if not instances:
        return {}
    instance_ids = [i['id'] for i in instances]
    approvals = sb.table('workflow_stage_approvals').select('id,instance_id,stage_id,approver_user_id,status,responded_at,users(name,email)').in_('instance_id', instance_ids).execute().data
    by_doc = {}
    for inst in instances:
        inst['approvals'] = [a for a in approvals if a['instance_id'] == inst['id']]
        by_doc[inst['document_id']] = inst
    return by_doc


@app.route('/api/workflows/instances', methods=['GET'])
def get_instance():
    sb = get_sb()
    doc_key, document_id = request.args.get('doc_key'), request.args.get('document_id')
    if not doc_key or not document_id:
        return jsonify({'error': 'doc_key and document_id are required'}), 400
    inst = get_workflow_instance(sb, g.claims['company_id'], doc_key, document_id)
    return jsonify({'instance': inst})


# Some document types have their own pre-existing `status` column that
# other code (list pages, colored badges, filters) already reads -- e.g.
# api/vouchers.py's vouchers.status. Rather than making every such module
# poll the workflow engine, sync the final outcome back onto that column
# the moment an instance resolves. Add an entry here whenever a new
# document type is wired into the engine and its 'rejected'/'completed'
# workflow outcomes need to map onto its own status values.
DOCUMENT_STATUS_SYNC = {
    'vouchers': {'table': 'vouchers', 'rejected': 'rejected', 'completed': 'approved'},
}


def _sync_document_status(sb, doc_key, document_id, workflow_status):
    cfg = DOCUMENT_STATUS_SYNC.get(doc_key)
    if not cfg or workflow_status not in cfg:
        return
    sb.table(cfg['table']).update({
        'status': cfg[workflow_status], 'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', document_id).execute()


def _doc_key_for_instance(sb, instance):
    dt = sb.table('workflow_document_types').select('doc_key').eq('id', instance['document_type_id']).execute().data
    return dt[0]['doc_key'] if dt else None


def _apply_stage_decision(sb, approval, decision):
    """Shared by the magic-link route and the in-app respond route. Mirrors
    api/vouchers.py's old _apply_approval_decision: any rejection kills the
    instance; a stage only clears once its approvals satisfy the stage's
    approval_mode, at which point the instance auto-advances to the next
    stage (or completes if it was the last one). Whenever the instance
    resolves either way, syncs the outcome onto the source document's own
    status column via DOCUMENT_STATUS_SYNC."""
    new_status = 'approved' if decision == 'approve' else 'rejected'
    sb.table('workflow_stage_approvals').update({
        'status': new_status, 'responded_at': datetime.utcnow().isoformat(),
    }).eq('id', approval['id']).execute()

    instance = sb.table('workflow_instances').select('*').eq('id', approval['instance_id']).execute().data[0]
    stage = sb.table('workflow_stages').select('*').eq('id', approval['stage_id']).execute().data[0]
    statuses = [a['status'] for a in sb.table('workflow_stage_approvals').select('status')
                .eq('instance_id', instance['id']).eq('stage_id', stage['id']).execute().data]
    mode = stage.get('approval_mode') or 'all'

    if any(s == 'rejected' for s in statuses):
        sb.table('workflow_instances').update({'status': 'rejected', 'updated_at': datetime.utcnow().isoformat()}).eq('id', instance['id']).execute()
        _sync_document_status(sb, _doc_key_for_instance(sb, instance), instance['document_id'], 'rejected')
        return 'rejected'

    stage_cleared = all(s == 'approved' for s in statuses) if mode in ('all', 'sequential') else any(s == 'approved' for s in statuses)
    if not stage_cleared:
        return 'pending'

    stages = _stages_for_definition(sb, instance['workflow_id'])
    idx = next((i for i, s in enumerate(stages) if s['id'] == stage['id']), None)
    if idx is None or idx == len(stages) - 1:
        sb.table('workflow_instances').update({'status': 'completed', 'updated_at': datetime.utcnow().isoformat()}).eq('id', instance['id']).execute()
        _sync_document_status(sb, _doc_key_for_instance(sb, instance), instance['document_id'], 'completed')
        return 'completed'
    _enter_stage(sb, instance, stages[idx + 1], {})
    return 'in_progress'


def force_resolve_instance(sb, company_id, doc_key, document_id, decision):
    """Admin override -- e.g. api/vouchers.py's force-approve/reject.
    Resolves every pending approval on the document's current workflow
    instance at once and syncs the outcome, bypassing whichever approvers
    haven't responded yet. Returns {'error': ...} if no instance exists
    (e.g. the workflow wasn't configured yet when the document was
    submitted) so the caller can fall back to whatever it did before this
    engine existed."""
    dt = sb.table('workflow_document_types').select('id').eq('company_id', company_id).eq('doc_key', doc_key).execute().data
    if not dt:
        return {'error': 'Unknown document type'}
    inst = sb.table('workflow_instances').select('*').eq('document_type_id', dt[0]['id']).eq('document_id', document_id).execute().data
    if not inst:
        return {'error': 'No workflow instance found for this document'}
    inst = inst[0]
    new_status = 'approved' if decision == 'approve' else 'rejected'
    workflow_status = 'completed' if decision == 'approve' else 'rejected'
    sb.table('workflow_stage_approvals').update({
        'status': new_status, 'responded_at': datetime.utcnow().isoformat(),
    }).eq('instance_id', inst['id']).eq('status', 'pending').execute()
    sb.table('workflow_instances').update({
        'status': workflow_status, 'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', inst['id']).execute()
    _sync_document_status(sb, doc_key, document_id, workflow_status)
    return {'success': True, 'status': workflow_status}


# ── Approve / reject via emailed magic link -- no login required ────────────
@app.route('/api/workflows/action/<token>/<decision>', methods=['GET'])
def workflow_action(token, decision):
    if decision not in ('approve', 'reject'):
        return "Invalid action.", 400
    sb = get_sb()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        if payload.get('type') != 'workflow_action':
            raise ValueError('wrong token type')
    except Exception:
        return "This link has expired or is invalid.", 400
    approval = sb.table('workflow_stage_approvals').select('*').eq('id', payload['approval_id']).execute().data
    if not approval:
        return "This link is no longer valid.", 400
    approval = approval[0]
    if approval['status'] != 'pending':
        return f"This was already marked '{approval['status']}'.", 200
    result = _apply_stage_decision(sb, approval, decision)
    return f"Thanks — recorded as {'approved' if decision == 'approve' else 'rejected'}. Current status: {result}.", 200


# ── Approve / reject from inside the app ────────────────────────────────────
@app.route('/api/workflows/approvals/<aid>/respond', methods=['POST'])
def respond_approval(aid):
    sb = get_sb()
    decision = (request.json or {}).get('decision')
    if decision not in ('approve', 'reject'):
        return jsonify({'error': "decision must be 'approve' or 'reject'"}), 400
    approval = sb.table('workflow_stage_approvals').select('*').eq('id', aid).eq('approver_user_id', g.claims['user_id']).execute().data
    if not approval:
        return jsonify({'error': 'You are not listed as an approver on this stage'}), 403
    approval = approval[0]
    if approval['status'] != 'pending':
        return jsonify({'error': f"You already marked this '{approval['status']}'"}), 400
    result = _apply_stage_decision(sb, approval, decision)
    return jsonify({'success': True, 'status': result})


@app.route('/api/workflows/instances/<iid>/advance', methods=['POST'])
def advance_instance(iid):
    """Manual advance for a stage that doesn't require approval -- e.g. move
    a Draft-stage document into the next stage. Approval-gated stages
    advance automatically via _apply_stage_decision instead."""
    sb = get_sb()
    instance = sb.table('workflow_instances').select('*').eq('id', iid).eq('company_id', g.claims['company_id']).execute().data
    if not instance:
        return jsonify({'error': 'Not found'}), 404
    instance = instance[0]
    stage = sb.table('workflow_stages').select('*').eq('id', instance['current_stage_id']).execute().data[0]
    if stage.get('requires_approval'):
        return jsonify({'error': 'This stage requires approval before it can advance'}), 400
    stages = _stages_for_definition(sb, instance['workflow_id'])
    idx = next((i for i, s in enumerate(stages) if s['id'] == stage['id']), None)
    if idx is None or idx == len(stages) - 1:
        sb.table('workflow_instances').update({'status': 'completed', 'updated_at': datetime.utcnow().isoformat()}).eq('id', iid).execute()
        return jsonify({'success': True, 'status': 'completed'})
    _enter_stage(sb, instance, stages[idx + 1], (request.json or {}).get('meta') or {})
    return jsonify({'success': True, 'status': 'in_progress'})
