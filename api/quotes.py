from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import os, jwt, traceback, base64, requests, re
from datetime import datetime
from supabase import create_client

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')

# Duplicated from api/auth.py — Vercel Python functions must be fully
# self-contained (no cross-file imports between api/*.py routes).
MS_TENANT_ID = os.environ.get('MS_TENANT_ID', 'b36855d2-9d26-43a4-bec6-82268a7713fb')
MS_CLIENT_ID = os.environ.get('MS_CLIENT_ID', '491f22c7-9dee-4c30-b828-acf8ba8d948c')
MS_CLIENT_SECRET = os.environ.get('MS_CLIENT_SECRET')
PDFSHIFT_API_KEY = os.environ.get('PDFSHIFT_API_KEY')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_ms_token():
    """Get a Microsoft Graph access token using client credentials."""
    url = f'https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token'
    data = {
        'client_id': MS_CLIENT_ID,
        'client_secret': MS_CLIENT_SECRET,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials'
    }
    r = requests.post(url, data=data)
    return r.json().get('access_token')

@app.errorhandler(Exception)
def handle_exception(e):
    """Catch anything a route doesn't handle itself and return clean JSON
    instead of Flask's raw HTML error page, so the frontend always gets a
    parseable {'error': ...} response."""
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    return jsonify({'error': str(e)}), 500

def verify_token(req):
    auth = req.headers.get('Authorization','')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

# ── List quotes ────────────────────────────────────────────────────────────────
@app.route('/api/quotes', methods=['GET'])
def list_quotes():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    # Optional limit/offset, defaulting high enough to behave like "no limit"
    # for any company's realistic current size — added as pagination
    # infrastructure for when a company's quote count grows large, without
    # changing today's dashboard behavior (which doesn't pass these yet).
    try: limit = min(1000, max(1, int(request.args.get('limit', 500))))
    except: limit = 500
    try: offset = max(0, int(request.args.get('offset', 0)))
    except: offset = 0

    rows = sb.table('quotes')\
        .select('id,title,customer,status,currency,total_sell,total_gp,margin,created_at,updated_at,created_by', count='exact')\
        .eq('company_id', claims['company_id'])\
        .order('updated_at', desc=True)\
        .range(offset, offset + limit - 1)\
        .execute()
    return jsonify({'quotes': rows.data, 'total': rows.count or 0})

# ── Get single quote ───────────────────────────────────────────────────────────
@app.route('/api/quotes/<qid>', methods=['GET'])
def get_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    row = sb.table('quotes').select('*').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Not found'}), 404
    return jsonify({'quote': row.data[0]})

# ── Create quote ───────────────────────────────────────────────────────────────
@app.route('/api/quotes', methods=['POST'])
def create_quote():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    d = request.json or {}
    row = sb.table('quotes').insert({
        'company_id':    claims['company_id'],
        'created_by':    claims['user_id'],
        'title':         d.get('title', 'Untitled Quote'),
        'customer':      d.get('customer', ''),
        'status':        d.get('status', 'draft'),
        'currency':      d.get('currency', 'AED'),
        'exchange_rate': d.get('exchange_rate', 1),
        'quote_data':    d.get('quote_data', []),
        'vendor_data':   d.get('vendor_data', []),
        'terms_data':    d.get('terms_data', []),
        'total_sell':    d.get('total_sell', 0),
        'total_gp':      d.get('total_gp', 0),
        'margin':        d.get('margin', 0),
    }).execute()
    return jsonify({'quote': row.data[0]}), 201

# ── Update quote ───────────────────────────────────────────────────────────────
@app.route('/api/quotes/<qid>', methods=['PUT'])
def update_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    # Check ownership
    existing = sb.table('quotes').select('id,created_by').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404
    if claims['role'] != 'admin' and existing.data[0]['created_by'] != claims['user_id']:
        return jsonify({'error': 'Forbidden'}), 403

    d = request.json or {}
    allowed = ['title','customer','status','currency','exchange_rate','quote_data','vendor_data','terms_data','total_sell','total_gp','margin']
    update = {k: d[k] for k in allowed if k in d}

    row = sb.table('quotes').update(update).eq('id', qid).execute()
    return jsonify({'quote': row.data[0]})

# ── Delete quote ───────────────────────────────────────────────────────────────
@app.route('/api/quotes/<qid>', methods=['DELETE'])
def delete_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('quotes').select('id,created_by').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404
    if claims['role'] != 'admin' and existing.data[0]['created_by'] != claims['user_id']:
        return jsonify({'error': 'Forbidden'}), 403

    sb.table('quotes').delete().eq('id', qid).execute()
    return jsonify({'ok': True})

# ── Duplicate quote ────────────────────────────────────────────────────────────
@app.route('/api/quotes/<qid>/duplicate', methods=['POST'])
def duplicate_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    orig = sb.table('quotes').select('*').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not orig.data: return jsonify({'error': 'Not found'}), 404

    o = orig.data[0]
    row = sb.table('quotes').insert({
        'company_id':    claims['company_id'],
        'created_by':    claims['user_id'],
        'title':         o['title'] + ' (Copy)',
        'customer':      o['customer'],
        'status':        'draft',
        'currency':      o['currency'],
        'exchange_rate': o['exchange_rate'],
        'quote_data':    o['quote_data'],
        'vendor_data':   o['vendor_data'],
        'terms_data':    o['terms_data'],
        'total_sell':    o['total_sell'],
        'total_gp':      o['total_gp'],
        'margin':        o['margin'],
    }).execute()
    return jsonify({'quote': row.data[0]}), 201

# ── Internal review workflow ────────────────────────────────────────────────
# A quote can be submitted for internal review before it goes to a customer.
# Each submission snapshots the current quote_data/terms_data/vendor_data as
# a new immutable `quote_versions` row, so a reviewer always sees exactly
# what was submitted even if the preparer keeps editing the live quote
# afterwards — and past submissions stay viewable for audit/history.

@app.route('/api/quotes/company-users', methods=['GET'])
def company_users():
    """Lightweight user picker list (id/name/email only) for any authenticated
    company user — used by the quote creator to pick who reviews a
    submission. Deliberately NOT admin-gated like /api/auth/team, and
    doesn't expose role/invite-management data."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    rows = sb.table('users').select('id,name,email,is_platform_admin')\
        .eq('company_id', claims['company_id']).order('name').execute()
    # Filter in Python (not via .eq(bool)) to match the same safe pattern
    # /api/auth/team already uses for excluding platform-admin accounts.
    users = [{'id': u['id'], 'name': u.get('name'), 'email': u.get('email')}
             for u in (rows.data or []) if not u.get('is_platform_admin')]
    return jsonify({'users': users})

@app.route('/api/quotes/reviewer-candidates', methods=['GET'])
def reviewer_candidates():
    """Users eligible to be picked as a reviewer — i.e. flagged can_review by
    a Company Admin in Team & Settings. This is what the 'Send to Review'
    picker uses; company-users above stays general-purpose."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    rows = sb.table('users').select('id,name,email,is_platform_admin,can_review')\
        .eq('company_id', claims['company_id']).order('name').execute()
    users = [{'id': u['id'], 'name': u.get('name'), 'email': u.get('email')}
             for u in (rows.data or []) if u.get('can_review') and not u.get('is_platform_admin')]
    return jsonify({'users': users})

def _reviewers_by_version(version_ids):
    """id -> [{user_id,name,email,status,comment,decided_at}] for the given
    version ids, in one round trip each to quote_version_reviewers/users."""
    if not version_ids: return {}
    rr = sb.table('quote_version_reviewers').select('*').in_('version_id', version_ids).execute()
    rows = rr.data or []
    uids = list({r['user_id'] for r in rows})
    users_by_id = {}
    if uids:
        ur = sb.table('users').select('id,name,email').in_('id', uids).execute()
        users_by_id = {u['id']: u for u in (ur.data or [])}
    out = {}
    for r in rows:
        u = users_by_id.get(r['user_id'], {})
        out.setdefault(r['version_id'], []).append({
            'user_id': r['user_id'], 'name': u.get('name'), 'email': u.get('email'),
            'status': r['status'], 'comment': r.get('comment'), 'decided_at': r.get('decided_at'),
        })
    return out

def _send_review_notification(sender_email, sender_name, to_email, quote_title, comment_note=''):
    """Best-effort notification email — failures here must never block the
    review action itself (submit/approve/request-changes all still succeed
    even if Graph is unreachable or misconfigured)."""
    if not MS_CLIENT_SECRET or not to_email:
        return False
    try:
        token = get_ms_token()
        if not token: return False
        body_html = f"""
        <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1a1a1a;max-width:600px;margin:0 auto;padding:20px">
            <p style="line-height:1.6">{comment_note}</p>
            <p style="line-height:1.6;color:#555">— {sender_name}</p>
        </body></html>
        """
        payload = {
            'message': {
                'subject': f"Review requested — {quote_title or 'Quotation'}",
                'body': {'contentType': 'HTML', 'content': body_html},
                'toRecipients': [{'emailAddress': {'address': to_email}}],
                'from': {'emailAddress': {'address': sender_email}},
            },
            'saveToSentItems': True,
        }
        r = requests.post(
            f'https://graph.microsoft.com/v1.0/users/{sender_email}/sendMail',
            json=payload, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            timeout=15
        )
        return r.status_code == 202
    except Exception:
        traceback.print_exc()
        return False

@app.route('/api/quotes/<qid>/submit-review', methods=['POST'])
def submit_review(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    q = sb.table('quotes').select('*').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return jsonify({'error': 'Not found'}), 404
    quote = q.data[0]

    d = request.json or {}
    reviewer_ids = list({rid for rid in (d.get('reviewer_ids') or []) if rid})
    if not reviewer_ids:
        return jsonify({'error': 'Select at least one reviewer'}), 400

    # Only allow picking reviewers who belong to this company AND are flagged
    # can_review — enforced server-side, not just filtered in the picker UI.
    valid = sb.table('users').select('id,name,email').in_('id', reviewer_ids)\
        .eq('company_id', claims['company_id']).eq('can_review', True).execute()
    reviewers = valid.data or []
    if not reviewers:
        return jsonify({'error': 'None of the selected reviewers are eligible (they need the reviewer flag — set in Team & Settings)'}), 400

    next_version = (quote.get('current_version') or 0) + 1
    ver = sb.table('quote_versions').insert({
        'quote_id': qid,
        'version_number': next_version,
        'title': quote.get('title'),
        'customer': quote.get('customer'),
        'quote_data': quote.get('quote_data'),
        'terms_data': quote.get('terms_data'),
        'vendor_data': quote.get('vendor_data'),
        'status': 'pending',
        'submitted_by': claims['user_id'],
    }).execute()
    version = ver.data[0]

    sb.table('quote_version_reviewers').insert([
        {'version_id': version['id'], 'user_id': r['id']} for r in reviewers
    ]).execute()

    sb.table('quotes').update({
        'review_status': 'pending',
        'current_version': next_version,
    }).eq('id', qid).execute()

    sender = sb.table('users').select('email,name,ms365_email').eq('id', claims['user_id']).execute()
    sender_email = (sender.data[0].get('ms365_email') or sender.data[0]['email']) if sender.data else None
    sender_name = (sender.data[0].get('name') or sender_email) if sender.data else 'A colleague'
    if sender_email:
        note = f'{sender_name} sent you "{quote.get("title") or "a quotation"}" for review (version {next_version}). Log in to Sysconic Quote Manager and open Review Queue to take a look.'
        for r in reviewers:
            _send_review_notification(sender_email, sender_name, r.get('email'), quote.get('title'), note)

    return jsonify({'version': version}), 201

@app.route('/api/quotes/<qid>/versions', methods=['GET'])
def list_versions(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    q = sb.table('quotes').select('id').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return jsonify({'error': 'Not found'}), 404

    rows = sb.table('quote_versions').select(
        'id,version_number,status,submitted_by,submitted_at,reviewed_by,reviewed_at,review_comment,title,customer'
    ).eq('quote_id', qid).order('version_number', desc=True).execute()
    versions = rows.data or []
    rmap = _reviewers_by_version([v['id'] for v in versions])
    for v in versions:
        v['reviewers'] = rmap.get(v['id'], [])
    return jsonify({'versions': versions})

@app.route('/api/quotes/<qid>/versions/<vid>', methods=['GET'])
def get_version(qid, vid):
    """Full snapshot (including quote_data/terms_data) for a single version —
    kept separate from the list endpoint above so the list stays light."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    q = sb.table('quotes').select('id').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return jsonify({'error': 'Not found'}), 404

    v = sb.table('quote_versions').select('*').eq('id', vid).eq('quote_id', qid).execute()
    if not v.data: return jsonify({'error': 'Not found'}), 404
    version = v.data[0]
    version['reviewers'] = _reviewers_by_version([vid]).get(vid, [])
    return jsonify({'version': version})

@app.route('/api/quotes/review-queue', methods=['GET'])
def review_queue():
    """Quotes where the current user has been personally assigned as a
    reviewer and hasn't decided yet — a "waiting on me" queue. Review is
    assigned per-quote by the creator, not open to the whole company, so
    this is scoped to the caller rather than company-wide.

    ?scope=all (Company-Admin only) instead returns every quote in the
    company currently pending review, regardless of who's assigned — this
    is what backs the admin-override UI (force-approve/reassign/cancel)."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    if request.args.get('scope') == 'all' and claims['role'] == 'admin':
        qrows = sb.table('quotes').select('id,title,customer,total_sell,current_version,updated_at,created_by')\
            .eq('company_id', claims['company_id']).eq('review_status', 'pending')\
            .order('updated_at', desc=True).execute()
        quotes = qrows.data or []
        if quotes:
            qids = [q['id'] for q in quotes]
            vrows = sb.table('quote_versions').select('id,quote_id,version_number').in_('quote_id', qids).execute()
            version_id_by_quote_and_number = {(v['quote_id'], v['version_number']): v['id'] for v in (vrows.data or [])}
            for q in quotes:
                vid = version_id_by_quote_and_number.get((q['id'], q.get('current_version')))
                q['pending_version_id'] = vid
                q['reviewers'] = _reviewers_by_version([vid]).get(vid, []) if vid else []
        return jsonify({'quotes': quotes})

    mine = sb.table('quote_version_reviewers').select('version_id')\
        .eq('user_id', claims['user_id']).eq('status', 'pending').execute()
    version_ids = list({r['version_id'] for r in (mine.data or [])})
    if not version_ids:
        return jsonify({'quotes': []})

    vrows = sb.table('quote_versions').select('id,quote_id,version_number').in_('id', version_ids).execute()
    latest_by_quote = {v['quote_id']: (v['id'], v['version_number']) for v in (vrows.data or [])}
    if not latest_by_quote:
        return jsonify({'quotes': []})

    qrows = sb.table('quotes').select('id,title,customer,total_sell,current_version,updated_at,created_by')\
        .eq('company_id', claims['company_id']).eq('review_status', 'pending')\
        .in_('id', list(latest_by_quote.keys())).order('updated_at', desc=True).execute()
    out = []
    for q in (qrows.data or []):
        vid, vnum = latest_by_quote.get(q['id'], (None, None))
        # Only surface it if my pending assignment is on the quote's current
        # submission — a superseded older version shouldn't keep nagging me.
        if vnum != q.get('current_version'):
            continue
        q['pending_version_id'] = vid
        out.append(q)
    return jsonify({'quotes': out})

def _resolve_version(qid, vid, claims):
    q = sb.table('quotes').select('*').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return None, None
    v = sb.table('quote_versions').select('*').eq('id', vid).eq('quote_id', qid).execute()
    if not v.data: return q.data[0], None
    return q.data[0], v.data[0]

def _recompute_aggregate(vid):
    """Any 'changes_requested' wins immediately; otherwise the version only
    becomes 'approved' once every assigned reviewer has approved."""
    all_rows = sb.table('quote_version_reviewers').select('status').eq('version_id', vid).execute()
    statuses = [r['status'] for r in (all_rows.data or [])]
    if any(s == 'changes_requested' for s in statuses):
        return 'changes_requested'
    if statuses and all(s == 'approved' for s in statuses):
        return 'approved'
    return 'pending'

def _record_reviewer_decision(qid, vid, claims, status, comment):
    """Records one assigned reviewer's decision on a version, then
    recomputes the version's aggregate status. Returns (quote, agg_status, error_response)."""
    quote, version = _resolve_version(qid, vid, claims)
    if not quote: return None, None, (jsonify({'error': 'Quote not found'}), 404)
    if not version: return None, None, (jsonify({'error': 'Version not found'}), 404)

    mine = sb.table('quote_version_reviewers').select('id')\
        .eq('version_id', vid).eq('user_id', claims['user_id']).execute()
    if not mine.data:
        return None, None, (jsonify({'error': 'You are not an assigned reviewer for this version'}), 403)

    sb.table('quote_version_reviewers').update({
        'status': status, 'comment': comment or None, 'decided_at': datetime.utcnow().isoformat(),
    }).eq('version_id', vid).eq('user_id', claims['user_id']).execute()

    agg = _recompute_aggregate(vid)

    ver_update = {'status': agg}
    if agg != 'pending':
        ver_update['reviewed_by'] = claims['user_id']
        ver_update['reviewed_at'] = datetime.utcnow().isoformat()
        if comment: ver_update['review_comment'] = comment
    sb.table('quote_versions').update(ver_update).eq('id', vid).execute()

    # Only reflect onto the parent quote once the group decision is final,
    # and only if this is still the quote's latest submission.
    if agg != 'pending' and quote.get('current_version') == version.get('version_number'):
        sb.table('quotes').update({'review_status': agg}).eq('id', qid).execute()

    return quote, agg, None

def _admin_finalize_version(qid, vid, claims, status, comment):
    """Company-Admin override: short-circuits the assigned reviewers and
    forces a final decision directly. Any reviewer rows still 'pending' get
    marked as superseded so their own view doesn't show a stale pending
    forever. Returns (quote, error_response)."""
    quote, version = _resolve_version(qid, vid, claims)
    if not quote: return None, (jsonify({'error': 'Quote not found'}), 404)
    if not version: return None, (jsonify({'error': 'Version not found'}), 404)

    sb.table('quote_version_reviewers').update({
        'status': status, 'comment': 'Overridden by admin decision', 'decided_at': datetime.utcnow().isoformat(),
    }).eq('version_id', vid).eq('status', 'pending').execute()

    sb.table('quote_versions').update({
        'status': status, 'reviewed_by': claims['user_id'], 'reviewed_at': datetime.utcnow().isoformat(),
        'review_comment': comment or None, 'admin_override': True,
    }).eq('id', vid).execute()

    if quote.get('current_version') == version.get('version_number'):
        sb.table('quotes').update({'review_status': status}).eq('id', qid).execute()

    return quote, None

@app.route('/api/quotes/<qid>/versions/<vid>/approve', methods=['POST'])
def approve_version(qid, vid):
    """Only a user the quote creator assigned as a reviewer on this specific
    version can approve it. Comment is optional."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    d = request.json or {}
    comment = (d.get('comment') or '').strip()

    quote, agg, err = _record_reviewer_decision(qid, vid, claims, 'approved', comment)
    if err: return err
    return jsonify({'ok': True, 'version_status': agg})

@app.route('/api/quotes/<qid>/versions/<vid>/request-changes', methods=['POST'])
def request_changes(qid, vid):
    """Only an assigned reviewer on this version can request changes."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    d = request.json or {}
    comment = (d.get('comment') or '').strip()
    if not comment:
        return jsonify({'error': 'A comment is required when requesting changes'}), 400

    quote, agg, err = _record_reviewer_decision(qid, vid, claims, 'changes_requested', comment)
    if err: return err
    return jsonify({'ok': True, 'version_status': agg})

# ── Company-Admin review overrides ───────────────────────────────────────────
# These don't require the admin to be an assigned reviewer — Admins can force
# a decision, cancel a submission, or reassign who's reviewing, on top of the
# normal per-reviewer flow above.
@app.route('/api/quotes/<qid>/versions/<vid>/admin-decide', methods=['POST'])
def admin_decide_version(qid, vid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403

    d = request.json or {}
    decision = (d.get('decision') or '').strip()
    comment = (d.get('comment') or '').strip()
    if decision not in ('approved', 'changes_requested'):
        return jsonify({'error': "decision must be 'approved' or 'changes_requested'"}), 400
    if decision == 'changes_requested' and not comment:
        return jsonify({'error': 'A comment is required when requesting changes'}), 400

    quote, err = _admin_finalize_version(qid, vid, claims, decision, comment)
    if err: return err
    return jsonify({'ok': True})

@app.route('/api/quotes/<qid>/versions/<vid>/cancel-review', methods=['POST'])
def cancel_review(qid, vid):
    """Aborts a pending submission (e.g. wrong reviewers picked, plans
    changed) without anyone having to formally reject it. Reuses the
    'changes_requested' terminal status so the quote goes back to editable —
    admin_override + the comment distinguish this from a real rejection."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403

    d = request.json or {}
    comment = (d.get('comment') or '').strip() or 'Review cancelled by admin.'

    quote, err = _admin_finalize_version(qid, vid, claims, 'changes_requested', comment)
    if err: return err
    return jsonify({'ok': True})

@app.route('/api/quotes/<qid>/versions/<vid>/reassign-reviewers', methods=['POST'])
def reassign_reviewers(qid, vid):
    """Replaces who's reviewing a still-pending version. Reviewers who
    already decided are left alone (for the audit trail) even if they're
    dropped from the new list; only undecided assignments are added/removed."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403

    quote, version = _resolve_version(qid, vid, claims)
    if not quote: return jsonify({'error': 'Quote not found'}), 404
    if not version: return jsonify({'error': 'Version not found'}), 404
    if version['status'] != 'pending':
        return jsonify({'error': 'This version is already finalized — nothing to reassign'}), 400

    d = request.json or {}
    reviewer_ids = list({rid for rid in (d.get('reviewer_ids') or []) if rid})
    if not reviewer_ids:
        return jsonify({'error': 'Select at least one reviewer'}), 400

    valid = sb.table('users').select('id').in_('id', reviewer_ids)\
        .eq('company_id', claims['company_id']).eq('can_review', True).execute()
    valid_ids = {u['id'] for u in (valid.data or [])}
    if not valid_ids:
        return jsonify({'error': 'None of the selected reviewers are eligible (must have the reviewer flag)'}), 400

    existing_rows = sb.table('quote_version_reviewers').select('*').eq('version_id', vid).execute().data or []
    existing_user_ids = {r['user_id'] for r in existing_rows}

    for r in existing_rows:
        if r['user_id'] not in valid_ids and r['status'] == 'pending':
            sb.table('quote_version_reviewers').delete().eq('id', r['id']).execute()

    new_rows = [{'version_id': vid, 'user_id': uid} for uid in valid_ids if uid not in existing_user_ids]
    if new_rows:
        sb.table('quote_version_reviewers').insert(new_rows).execute()

    agg = _recompute_aggregate(vid)
    sb.table('quote_versions').update({'status': agg}).eq('id', vid).execute()
    if agg != 'pending' and quote.get('current_version') == version.get('version_number'):
        sb.table('quotes').update({'review_status': agg}).eq('id', qid).execute()

    return jsonify({'ok': True, 'version_status': agg})

# ── Email to customer ────────────────────────────────────────────────────────
# Sends from the logged-in user's own Microsoft mailbox (not a fixed service
# account), with the quote PDF attached. Requires the app's Graph app
# registration to have Mail.Send application permission across the tenant
# (already true today, since the same setup already sends invite emails as a
# fixed sender) — no per-user OAuth needed.
@app.route('/api/quotes/<qid>/email', methods=['POST'])
def email_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not MS_CLIENT_SECRET:
        return jsonify({'error': 'Email sending is not configured (missing MS_CLIENT_SECRET)'}), 500
    if not PDFSHIFT_API_KEY:
        return jsonify({'error': 'PDF rendering is not configured (missing PDFSHIFT_API_KEY)'}), 500

    q = sb.table('quotes').select('id,title').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return jsonify({'error': 'Not found'}), 404
    quote = q.data[0]

    d = request.json or {}
    to_email = (d.get('to') or '').strip()
    subject = (d.get('subject') or f"Quotation — {quote.get('title') or 'Sysconic'}").strip()
    message = d.get('message') or ''
    html = d.get('html') or ''
    from_email = (d.get('from_email') or '').strip()
    if not to_email:
        return jsonify({'error': 'Recipient email is required'}), 400
    if not html:
        return jsonify({'error': 'No quote HTML provided to render'}), 400

    sender = sb.table('users').select('email,name,ms365_email').eq('id', claims['user_id']).execute()
    if not sender.data:
        return jsonify({'error': 'Could not look up your account'}), 500
    # The app login email isn't necessarily a real mailbox in the sender's
    # Microsoft 365 tenant (Graph's send-as-user call 404s if it isn't), so
    # prefer the M365 mailbox the user has told us to send from. If the
    # frontend just submitted a new one, save it for next time too.
    if from_email:
        sb.table('users').update({'ms365_email': from_email}).eq('id', claims['user_id']).execute()
        sender_email = from_email
    else:
        sender_email = sender.data[0].get('ms365_email') or sender.data[0]['email']
    sender_name = sender.data[0].get('name') or sender_email

    # Render the exact same HTML the browser shows, to a PDF, via PDFShift.
    try:
        pdf_resp = requests.post(
            'https://api.pdfshift.io/v3/convert/pdf',
            json={'source': html, 'use_print': True, 'format': 'A4', 'sandbox': False},
            headers={'X-API-Key': PDFSHIFT_API_KEY, 'Content-Type': 'application/json'},
            timeout=25
        )
        if pdf_resp.status_code != 200:
            return jsonify({'error': f'PDF service error ({pdf_resp.status_code}): {pdf_resp.text[:300]}'}), 502
        pdf_b64 = base64.b64encode(pdf_resp.content).decode('ascii')
    except Exception as e:
        return jsonify({'error': f'PDF generation failed: {str(e)[:200]}'}), 500

    fname = re.sub(r'[^\w\-]+', '-', f"Quotation-{quote.get('title') or qid[:8]}") + '.pdf'

    token = get_ms_token()
    if not token:
        return jsonify({'error': 'Could not authenticate with Microsoft Graph'}), 502

    body_html = f"""
    <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1a1a1a;max-width:600px;margin:0 auto;padding:20px">
        <p style="line-height:1.6">{message.replace(chr(10), '<br>') if message else f'Please find attached the quotation <strong>{quote.get("title") or ""}</strong>.'}</p>
        <p style="line-height:1.6;color:#555">Best regards,<br>{sender_name}</p>
    </body></html>
    """

    payload = {
        'message': {
            'subject': subject,
            'body': {'contentType': 'HTML', 'content': body_html},
            'toRecipients': [{'emailAddress': {'address': to_email}}],
            'from': {'emailAddress': {'address': sender_email}},
            'attachments': [{
                '@odata.type': '#microsoft.graph.fileAttachment',
                'name': fname,
                'contentType': 'application/pdf',
                'contentBytes': pdf_b64,
            }],
        },
        'saveToSentItems': True,
    }

    try:
        r = requests.post(
            f'https://graph.microsoft.com/v1.0/users/{sender_email}/sendMail',
            json=payload,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            timeout=25
        )
    except Exception as e:
        return jsonify({'error': f'Could not reach Microsoft Graph: {str(e)[:200]}'}), 502

    if r.status_code != 202:
        return jsonify({'error': f'Email send failed ({r.status_code}): {r.text[:300]}'}), 502

    sb.table('quote_emails').insert({
        'quote_id': qid, 'sent_by': claims['user_id'], 'sent_to': to_email, 'subject': subject,
    }).execute()

    return jsonify({'ok': True, 'sent_from': sender_email})

if __name__ == '__main__':
    app.run(debug=True)
