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

STATUS_LABELS_PY = {'draft': 'Draft', 'sent': 'Sent', 'awarded': 'Awarded', 'lost': 'Lost'}

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

def _can_view_all_quotes(claims):
    """Admins always see every quote in their company. A plain User only
    gets that too if a Company Admin has flagged can_view_all_quotes for
    them (Team & Settings). Looked up fresh each call (not baked into the
    JWT) so toggling the flag takes effect without forcing a re-login."""
    if claims['role'] == 'admin':
        return True
    u = sb.table('users').select('can_view_all_quotes').eq('id', claims['user_id']).execute()
    return bool(u.data and u.data[0].get('can_view_all_quotes'))

def _can_edit_quote(quote, claims):
    """Who can mutate a quote's content/lifecycle — save, submit for review,
    email to the customer, duplicate: the creator, a Company Admin, or
    anyone flagged can_view_all_quotes. That flag doubles as edit rights
    here (not just read visibility) by design — it's meant for people who
    need to manage quotes across the whole company, not just look at them."""
    if quote.get('created_by') == claims['user_id']:
        return True
    return _can_view_all_quotes(claims)

def _log_activity(qid, actor_id, action, detail=''):
    """Best-effort activity log entry — shown as a timeline in the quote's
    Log tab. Never raises: a logging failure must not block the actual
    action (quote save, review decision, email send, etc.)."""
    try:
        sb.table('quote_activity').insert({
            'quote_id': qid, 'actor_id': actor_id, 'action': action, 'detail': detail or None,
        }).execute()
    except Exception:
        traceback.print_exc()

def _is_assigned_reviewer_on_quote(qid, user_id):
    """Whether this user is an assigned reviewer on the quote's current
    pending version — the narrow exception that lets a reviewer open a
    specific quote they were asked to review, without granting them
    visibility into the company's other quotes."""
    q = sb.table('quotes').select('current_version').eq('id', qid).execute()
    if not q.data: return False
    vrows = sb.table('quote_versions').select('id').eq('quote_id', qid)\
        .eq('version_number', q.data[0].get('current_version')).execute()
    if not vrows.data: return False
    vid = vrows.data[0]['id']
    mine = sb.table('quote_version_reviewers').select('id').eq('version_id', vid).eq('user_id', user_id).execute()
    return bool(mine.data)

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

    q = sb.table('quotes')\
        .select('id,title,customer,status,currency,total_sell,total_gp,margin,created_at,updated_at,created_by', count='exact')\
        .eq('company_id', claims['company_id'])
    if not _can_view_all_quotes(claims):
        q = q.eq('created_by', claims['user_id'])
    rows = q.order('updated_at', desc=True).range(offset, offset + limit - 1).execute()
    return jsonify({'quotes': rows.data, 'total': rows.count or 0})

def _can_view_quote(qid, quote, claims):
    if quote.get('created_by') == claims['user_id'] or _can_view_all_quotes(claims):
        return True
    return _is_assigned_reviewer_on_quote(qid, claims['user_id'])

# ── Get single quote ───────────────────────────────────────────────────────────
@app.route('/api/quotes/<qid>', methods=['GET'])
def get_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    row = sb.table('quotes').select('*').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Not found'}), 404
    quote = row.data[0]

    if not _can_view_quote(qid, quote, claims):
        return jsonify({'error': 'Forbidden'}), 403

    return jsonify({'quote': quote})

# ── Quote activity log ────────────────────────────────────────────────────────
@app.route('/api/quotes/<qid>/activity', methods=['GET'])
def quote_activity(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    row = sb.table('quotes').select('id,created_by').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Not found'}), 404
    if not _can_view_quote(qid, row.data[0], claims):
        return jsonify({'error': 'Forbidden'}), 403

    rows = sb.table('quote_activity').select('*').eq('quote_id', qid).order('created_at', desc=True).execute()
    entries = rows.data or []
    actor_ids = list({e['actor_id'] for e in entries if e.get('actor_id')})
    actors_by_id = {}
    if actor_ids:
        ar = sb.table('users').select('id,name,email').in_('id', actor_ids).execute()
        actors_by_id = {u['id']: u for u in (ar.data or [])}
    for e in entries:
        a = actors_by_id.get(e.get('actor_id'), {})
        e['actor_name'] = a.get('name') or a.get('email') or 'System'
    return jsonify({'activity': entries})

# ── Create quote ───────────────────────────────────────────────────────────────
# SUB-001: plan-limit plumbing only, per product decision — no plan currently
# sets max_quotes_per_month, so this is a no-op today for every real company.
# It exists so a limit can be turned on later (via companies.plan_limits)
# without another deploy.
def _quote_limit_exceeded(claims):
    co = sb.table('companies').select('plan_limits').eq('id', claims['company_id']).execute().data
    limits = (co[0].get('plan_limits') if co else None) or {}
    max_per_month = limits.get('max_quotes_per_month')
    if max_per_month is None:
        return False
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    count = sb.table('quotes').select('id', count='exact')\
        .eq('company_id', claims['company_id']).gt('created_at', month_start).execute()
    return (count.count or 0) >= max_per_month

@app.route('/api/quotes', methods=['POST'])
def create_quote():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if _quote_limit_exceeded(claims):
        return jsonify({'error': "This company has reached its plan's quote limit for this month."}), 403

    d = request.json or {}
    row = sb.table('quotes').insert({
        'company_id':    claims['company_id'],
        'created_by':    claims['user_id'],
        'title':         d.get('title', 'Untitled Quote'),
        'customer':      d.get('customer', ''),
        'status':        d.get('status', 'draft'),
        'currency':      d.get('currency', 'AED'),
        'exchange_rate': d.get('exchange_rate', 1),
        'pricing_type':  d.get('pricing_type', 'markup'),
        'quote_data':    d.get('quote_data', []),
        'vendor_data':   d.get('vendor_data', []),
        'terms_data':    d.get('terms_data', []),
        'total_sell':    d.get('total_sell', 0),
        'total_gp':      d.get('total_gp', 0),
        'margin':        d.get('margin', 0),
    }).execute()
    quote = row.data[0]
    _log_activity(quote['id'], claims['user_id'], 'created', f"Quote \"{quote.get('title') or 'Untitled'}\" created")
    return jsonify({'quote': quote}), 201

# ── Project Performance: on-award hook ──────────────────────────────────────
# Duplicated calc logic (see calc_item/calc_opt in api/pdf.py, api/proposal.py
# and index.html) — Vercel Python functions can't share sibling modules, so
# every file that needs quote-line math carries its own copy. Keep in sync by
# hand if the pricing formula ever changes.
def _has_feature(claims, feature):
    return bool((claims.get('features') or {}).get(feature))

def _numf(v, d=0.0):
    try:
        return float(v) if v not in (None, '') else d
    except (TypeError, ValueError):
        return d

def _calc_item_pp(it, pricing_type):
    cad = _numf(it.get('cost')) * (1 - _numf(it.get('disc')) / 100.0) * (1 - _numf(it.get('discAdd')) / 100.0)
    pct = _numf(it.get('margin'), 0.2)
    if pricing_type == 'margin':
        pct = min(0.95, max(0, pct))
        up = round(cad / (1 - pct)) if pct < 1 else 0
    else:
        up = round(cad * (1 + pct))
    qty = _numf(it.get('qty'), 1)
    return {'unit_cost': cad, 'unit_price': up, 'total_price': up * qty, 'total_cost': cad * qty}

def _freeze_commercial_baseline(company_id, user_id, project_id, quote, option):
    """Called once, the instant a quote transitions to 'awarded'. Freezes the
    winning option's commercial position permanently -- per the Project
    Performance design, this snapshot must never be edited again even if
    product master costs are updated later. Safe to call more than once
    defensively (e.g. retry after a partial failure) -- it always inserts a
    fresh baseline rather than updating one in place, since a baseline is
    meant to be immutable history, not a live record."""
    pricing_type = quote.get('pricing_type') or 'markup'
    total_sell, total_cost = 0.0, 0.0
    section_payloads = []
    for s in (option.get('sections') or []):
        s_sell, s_cost, line_payloads = 0.0, 0.0, []
        for it in (s.get('items') or []):
            c = _calc_item_pp(it, pricing_type)
            s_sell += c['total_price']; s_cost += c['total_cost']
            line_payloads.append({
                'brand': (it.get('brand') or '')[:200] or None,
                'model': (it.get('model') or '')[:200] or None,
                'description': (it.get('desc') or '')[:1000] or None,
                'quantity': _numf(it.get('qty'), 1),
                'estimated_unit_cost': c['unit_cost'],
                'estimated_total_cost': c['total_cost'],
                'estimated_unit_price': c['unit_price'],
                'estimated_total_price': c['total_price'],
            })
        section_payloads.append({
            'section_name': (s.get('name') or s.get('title') or 'Section')[:200],
            'section_revenue': s_sell, 'section_estimated_cost': s_cost,
            'lines': line_payloads,
        })
        total_sell += s_sell; total_cost += s_cost

    gp = total_sell - total_cost
    gp_pct = (gp / total_sell * 100.0) if total_sell else 0.0

    baseline = sb.table('project_commercial_baselines').insert({
        'company_id': company_id, 'project_id': project_id, 'quote_id': quote['id'],
        'quote_ref': quote.get('quote_ref'), 'quote_revision': quote.get('current_version') or 0,
        'contract_value': total_sell, 'total_estimated_cost': total_cost,
        'expected_gp': gp, 'expected_gp_pct': gp_pct, 'created_by': user_id,
    }).execute().data[0]

    for s in section_payloads:
        srow = sb.table('project_baseline_sections').insert({
            'company_id': company_id, 'baseline_id': baseline['id'],
            'section_name': s['section_name'], 'section_revenue': s['section_revenue'],
            'section_estimated_cost': s['section_estimated_cost'],
        }).execute().data[0]
        if s['lines']:
            rows = [{**l, 'company_id': company_id, 'baseline_id': baseline['id'], 'section_id': srow['id']} for l in s['lines']]
            sb.table('project_baseline_lines').insert(rows).execute()

    sb.table('projects').update({
        'original_selling_price': total_sell, 'original_estimated_cost': total_cost,
        'original_gp': gp, 'original_gp_pct': gp_pct,
        'revenue_forecast': total_sell,
    }).eq('id', project_id).eq('company_id', company_id).execute()
    return baseline

def _activate_project_performance(claims, quote, won_option_index):
    """Find-or-create the local `projects` row for a newly-awarded quote and
    freeze its commercial baseline. Deliberately does not call Zoho here --
    ZohoAdapter lives in api/integrations.py and Vercel's Python builder
    won't bundle sibling modules across route files, so the actual Zoho
    project creation is triggered by the frontend immediately after this
    call succeeds (POST /api/integrations/zoho/create-project, the same
    endpoint the manual \"+ Zoho\" button already uses). Returns the project
    row plus whether it still needs that Zoho link."""
    company_id, user_id = claims['company_id'], claims['user_id']
    project_id = quote.get('project_id')
    if project_id:
        proj = sb.table('projects').select('*').eq('id', project_id).eq('company_id', company_id).execute()
        project = proj.data[0] if proj.data else None
    else:
        project = None

    if not project:
        row = sb.table('projects').insert({
            'company_id': company_id, 'created_by': user_id,
            'name': quote.get('title') or 'Untitled Project',
            'customer': quote.get('customer') or '',
            'status': 'active',
        }).execute()
        project = row.data[0]
        sb.table('quotes').update({'project_id': project['id']}).eq('id', quote['id']).eq('company_id', company_id).execute()

    sb.table('projects').update({
        'quotation_id': quote['id'], 'quote_ref': quote.get('quote_ref'),
        'won_option_index': won_option_index,
    }).eq('id', project['id']).eq('company_id', company_id).execute()

    # Idempotency (requirement: a project must never be created twice for the
    # same won quotation) -- if a baseline already exists for this project,
    # this is a repeat click (e.g. double-submit, or retrying after the Zoho
    # link step failed) -- reuse it rather than freezing a second one.
    existing_baseline = sb.table('project_commercial_baselines').select('id').eq('project_id', project['id']).limit(1).execute()
    already_activated = bool(existing_baseline.data)
    if not already_activated:
        options = quote.get('quote_data') or []
        option = options[won_option_index] if won_option_index < len(options) else (options[0] if options else {})
        _freeze_commercial_baseline(company_id, user_id, project['id'], quote, option)

    return {'project_id': project['id'], 'needs_zoho_link': not bool(project.get('zoho_project_id')), 'already_activated': already_activated}

# ── Update quote ───────────────────────────────────────────────────────────────
# A quote is only editable while its status is 'draft'. Once it's been
# emailed to the customer (status auto-flips to 'sent') or manually marked
# sent/awarded/lost, its content is locked — the only way to edit it again
# is to explicitly set the status back to 'draft' first. This keeps what a
# reviewer/customer saw for a given version from silently drifting.
CONTENT_FIELDS = ['title','customer','currency','exchange_rate','pricing_type','quote_data','vendor_data','terms_data','total_sell','total_gp','margin']

@app.route('/api/quotes/<qid>', methods=['PUT'])
def update_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    # Check ownership
    existing = sb.table('quotes').select('id,created_by,status,quote_data,pricing_type,current_version,project_id,quote_ref,title,customer').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404
    if not _can_edit_quote(existing.data[0], claims):
        return jsonify({'error': 'Forbidden'}), 403

    d = request.json or {}
    allowed = ['title','customer','status','currency','exchange_rate','pricing_type','quote_data','vendor_data','terms_data','total_sell','total_gp','margin']
    update = {k: d[k] for k in allowed if k in d}

    old_status = existing.data[0].get('status')
    new_status = update.get('status', old_status)
    is_transitioning = new_status != old_status
    touches_content = any(f in update for f in CONTENT_FIELDS)

    if old_status != 'draft' and not is_transitioning and touches_content:
        return jsonify({'error': f'This quote is locked because its status is "{STATUS_LABELS_PY.get(old_status, old_status)}". Set the status back to Draft to edit it.'}), 423

    row = sb.table('quotes').update(update).eq('id', qid).execute()
    updated_quote = row.data[0]
    # Log status changes specifically (not every autosave — that would flood
    # the log with noise since quote_data saves on every edit).
    if 'status' in update and update['status'] != old_status:
        _log_activity(qid, claims['user_id'], 'status_changed', f"{STATUS_LABELS_PY.get(old_status, old_status)} → {STATUS_LABELS_PY.get(update['status'], update['status'])}")

    return jsonify({'quote': updated_quote})

# ── Create Project (Project Performance) ────────────────────────────────────
# Deliberately a separate, explicit action rather than something that fires
# silently inside the status save above: a "Create Project" button appears
# in the quote editor once status is Awarded, and this is what it calls.
# Freezes the commercial baseline and creates/links the local project;
# Zoho project creation itself is a follow-up call the frontend makes to the
# existing /api/integrations/zoho/create-project endpoint (see the note on
# _activate_project_performance above for why that lives in a different file).
@app.route('/api/quotes/<qid>/create-project', methods=['POST'])
def create_project_from_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not _has_feature(claims, 'project_performance'):
        return jsonify({'error': 'Project Performance is not enabled for this company'}), 403

    existing = sb.table('quotes').select('id,created_by,status,quote_data,pricing_type,current_version,project_id,quote_ref,title,customer').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404
    quote = existing.data[0]
    if not _can_edit_quote(quote, claims):
        return jsonify({'error': 'Forbidden'}), 403
    if quote.get('status') != 'awarded':
        return jsonify({'error': 'This quote must be Awarded before a project can be created'}), 400

    d = request.json or {}
    options = quote.get('quote_data') or []
    won_idx = d.get('won_option_index')
    if won_idx is None:
        won_idx = 0 if len(options) <= 1 else None
    if won_idx is None:
        return jsonify({
            'error': 'This quote has more than one pricing option — pick which one was awarded.',
            'needs_won_option_index': True,
            'options': [{'index': i, 'label': o.get('label') or f'Option {i+1}'} for i, o in enumerate(options)],
        }), 409
    try:
        won_idx = int(won_idx)
    except (TypeError, ValueError):
        return jsonify({'error': 'won_option_index must be a number'}), 400
    if won_idx < 0 or (options and won_idx >= len(options)):
        return jsonify({'error': 'won_option_index is out of range for this quote'}), 400

    sb.table('quotes').update({'won_option_index': won_idx}).eq('id', qid).eq('company_id', claims['company_id']).execute()
    quote['won_option_index'] = won_idx

    try:
        pp_activation = _activate_project_performance(claims, quote, won_idx)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'Project creation failed: {str(e)[:250]}'}), 500

    if not pp_activation.get('already_activated'):
        _log_activity(qid, claims['user_id'], 'project_performance_activated',
                      f"Commercial baseline frozen, project {pp_activation['project_id']}")

    # Return the refreshed quote too so the frontend can update project_id
    # in place without a second round trip.
    refreshed = sb.table('quotes').select('*').eq('id', qid).eq('company_id', claims['company_id']).execute()
    return jsonify({'ok': True, 'project_performance': pp_activation, 'quote': refreshed.data[0] if refreshed.data else None})

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
    if not _can_edit_quote(orig.data[0], claims):
        return jsonify({'error': 'Forbidden'}), 403

    o = orig.data[0]
    row = sb.table('quotes').insert({
        'company_id':    claims['company_id'],
        'created_by':    claims['user_id'],
        'title':         o['title'] + ' (Copy)',
        'customer':      o['customer'],
        'status':        'draft',
        'currency':      o['currency'],
        'exchange_rate': o['exchange_rate'],
        'pricing_type':  o.get('pricing_type', 'markup'),
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
    # Same reasoning as email_quote() — submitting for review mutates the
    # quote, so it needs the same edit permission as saving.
    if not _can_edit_quote(quote, claims):
        return jsonify({'error': 'You don\'t have permission to submit this quote for review — ask a Company Admin to enable "view all quotes" for your account, or have the quote creator submit it'}), 403

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

    reviewer_names = ', '.join(r.get('name') or r.get('email') or '' for r in reviewers)
    _log_activity(qid, claims['user_id'], 'submitted_for_review', f"v{next_version} sent to {reviewer_names}")

    return jsonify({'version': version}), 201

@app.route('/api/quotes/<qid>/versions', methods=['GET'])
def list_versions(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    q = sb.table('quotes').select('id,created_by').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return jsonify({'error': 'Not found'}), 404
    # AUTH-001 fix: this endpoint was company-scoped but not visibility-scoped --
    # any teammate could list another user's private quote's review history.
    # Same permission rule as get_quote()/quote_activity() elsewhere in this file.
    if not _can_view_quote(qid, q.data[0], claims):
        return jsonify({'error': 'Forbidden'}), 403

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

    q = sb.table('quotes').select('id,created_by').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return jsonify({'error': 'Not found'}), 404
    # AUTH-001 fix: same reasoning as list_versions() above -- this exposes a
    # full pricing/terms snapshot and must be visibility-scoped, not just
    # company-scoped.
    if not _can_view_quote(qid, q.data[0], claims):
        return jsonify({'error': 'Forbidden'}), 403

    v = sb.table('quote_versions').select('*').eq('id', vid).eq('quote_id', qid).execute()
    if not v.data: return jsonify({'error': 'Not found'}), 404
    version = v.data[0]
    version['reviewers'] = _reviewers_by_version([vid]).get(vid, [])
    return jsonify({'version': version})

# ── Customer-send snapshots (list + detail, mirrors versions above) ────────────
@app.route('/api/quotes/<qid>/customer-sends', methods=['GET'])
def list_customer_sends(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    q = sb.table('quotes').select('id,created_by').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return jsonify({'error': 'Not found'}), 404
    if not _can_view_quote(qid, q.data[0], claims):
        return jsonify({'error': 'Forbidden'}), 403

    rows = sb.table('quote_emails').select(
        'id,version_number,sent_to,sent_by,sent_at,subject'
    ).eq('quote_id', qid).order('sent_at', desc=True).execute()
    sends = rows.data or []
    sender_ids = list({s['sent_by'] for s in sends if s.get('sent_by')})
    senders_by_id = {}
    if sender_ids:
        sr = sb.table('users').select('id,name,email').in_('id', sender_ids).execute()
        senders_by_id = {u['id']: u for u in (sr.data or [])}
    for s in sends:
        u = senders_by_id.get(s.get('sent_by'), {})
        s['sent_by_name'] = u.get('name') or u.get('email') or 'Unknown'
    return jsonify({'sends': sends})

@app.route('/api/quotes/<qid>/customer-sends/<sid>', methods=['GET'])
def get_customer_send(qid, sid):
    """Full snapshot (quote_data/terms_data/vendor_data) for a single customer
    send — kept separate from the list endpoint above so the list stays
    light. Sends made before the snapshot migration have null content
    columns; the frontend shows a friendly message for those instead of a
    blank diff."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    q = sb.table('quotes').select('id,created_by').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return jsonify({'error': 'Not found'}), 404
    if not _can_view_quote(qid, q.data[0], claims):
        return jsonify({'error': 'Forbidden'}), 403

    s = sb.table('quote_emails').select('*').eq('id', sid).eq('quote_id', qid).execute()
    if not s.data: return jsonify({'error': 'Not found'}), 404
    return jsonify({'send': s.data[0]})

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

    action = 'reviewer_approved' if status == 'approved' else 'reviewer_requested_changes'
    _log_activity(qid, claims['user_id'], action, f"v{version.get('version_number')}" + (f": {comment}" if comment else ''))

    return quote, agg, None

def _admin_finalize_version(qid, vid, claims, status, comment, log_action=None):
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

    action = log_action or ('admin_force_approved' if status == 'approved' else 'admin_force_requested_changes')
    _log_activity(qid, claims['user_id'], action, f"v{version.get('version_number')}" + (f": {comment}" if comment else ''))

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

    quote, err = _admin_finalize_version(qid, vid, claims, 'changes_requested', comment, log_action='admin_cancelled_review')
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

    _log_activity(qid, claims['user_id'], 'admin_reassigned_reviewers', f"v{version.get('version_number')}")

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

    q = sb.table('quotes').select(
        'id,title,customer,status,currency,exchange_rate,quote_data,terms_data,vendor_data,customer_version,created_by'
    ).eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return jsonify({'error': 'Not found'}), 404
    quote = q.data[0]
    # Sending to the customer mutates the quote (status, version) just like
    # saving does, so it requires the same edit permission as update_quote().
    if not _can_edit_quote(quote, claims):
        return jsonify({'error': 'You don\'t have permission to email this quote — ask a Company Admin to enable "view all quotes" for your account, or have the quote creator send it'}), 403

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

    # First successful send to the customer bumps the quote to "sent" (unless
    # it's already past that in its lifecycle — don't clobber awarded/lost),
    # and increments the customer-facing version counter (v1 on first send).
    # A full content snapshot is stored alongside the send record itself
    # (mirroring quote_versions for review submissions) so this exact send
    # stays viewable/comparable later even after the live quote keeps
    # changing.
    next_customer_version = (quote.get('customer_version') or 0) + 1
    sb.table('quote_emails').insert({
        'quote_id': qid, 'sent_by': claims['user_id'], 'sent_to': to_email, 'subject': subject,
        'version_number': next_customer_version,
        'title': quote.get('title'), 'customer': quote.get('customer'),
        'currency': quote.get('currency'), 'exchange_rate': quote.get('exchange_rate'),
        'quote_data': quote.get('quote_data'), 'terms_data': quote.get('terms_data'),
        'vendor_data': quote.get('vendor_data'),
    }).execute()

    quote_update = {'customer_version': next_customer_version}
    if quote.get('status') not in ('awarded', 'lost'):
        quote_update['status'] = 'sent'
    sb.table('quotes').update(quote_update).eq('id', qid).execute()

    _log_activity(qid, claims['user_id'], 'emailed_to_customer', f"v{next_customer_version} sent to {to_email}")

    return jsonify({'ok': True, 'sent_from': sender_email, 'customer_version': next_customer_version})

if __name__ == '__main__':
    app.run(debug=True)
