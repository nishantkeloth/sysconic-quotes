from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import os, jwt, traceback, base64, requests, re, time, secrets, hashlib, uuid, json
from datetime import datetime, timedelta
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
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'nishant@sysconic.com')
CRON_SECRET = os.environ.get('CRON_SECRET')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

STATUS_LABELS_PY = {'draft': 'Draft', 'sent': 'Sent', 'awarded': 'Awarded', 'lost': 'Lost', 'cancelled': 'Cancelled'}
VALID_QUOTE_STATUSES = ('draft', 'sent', 'awarded', 'lost', 'cancelled')

# ── Proposal Builder concept images ──────────────────────────────────────────
# Per-section "concept view" images for the Professional Proposal template
# (gated behind the proposal_builder company feature). Same private-bucket +
# long-lived-signed-URL pattern as product-images / voucher-receipts / company-
# assets: the signed URL itself is what gets stored on the section object
# inside quote_data (as `image`), so no separate DB table is needed here --
# it's just persisted the next time the quote autosaves like any other field.
PROPOSAL_IMAGE_BUCKET = 'proposal-images'
MAX_PROPOSAL_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB
SIGNED_URL_EXPIRES_SECONDS = 10 * 365 * 24 * 60 * 60  # ~10 years

def _signed_url(bucket, path, expires_in=SIGNED_URL_EXPIRES_SECONDS):
    res = sb.storage.from_(bucket).create_signed_url(path, expires_in)
    url = res.get('signedURL') or res.get('signedUrl') or res.get('signed_url') or res.get('url')
    if not url:
        raise RuntimeError(f'Unexpected create_signed_url() response shape: {res!r}')
    if url.startswith('http://') or url.startswith('https://'):
        return url
    return f"{SUPABASE_URL}/storage/v1{url}"

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

_REVIEW_QUEUE_SUFFIXES = ('/approve', '/request-changes', '/admin-decide', '/cancel-review', '/reassign-reviewers')

@app.before_request
def _rbac_page_gate():
    claims = verify_token(request)
    if not claims:
        return None
    path = request.path
    if path == '/api/quotes/review-queue' or path.endswith(_REVIEW_QUEUE_SUFFIXES):
        page_key = 'reviewQueue'
    elif path.startswith('/api/deals'):
        page_key = 'deals'
    elif path.startswith('/api/insights'):
        page_key = 'insights'
    else:
        page_key = 'quotes'
    if not has_page_access(claims, page_key):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    return None
def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))

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
        .select('id,quote_ref,title,customer,status,currency,total_sell,total_gp,margin,created_at,updated_at,created_by', count='exact')\
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

# ── Quotation reference numbering (migration-quote-numbering.sql) ──────────
# Auto-generated on every new quote as PREFIX-YEAR-NNN (e.g. QT-2026-001).
# The counter resets each calendar year and is scoped per company. Prefix and
# zero-padding width are per-company settings (defaults 'QT'/3 apply with no
# settings row needed); the running number itself lives in
# quote_number_sequences and is only ever touched atomically via the
# get_next_quote_number() Postgres function -- never read-then-write from
# Python, so concurrent quote creation can't hand out the same number twice.
def _numbering_settings(company_id):
    row = sb.table('quote_numbering_settings').select('prefix,padding').eq('company_id', company_id).execute()
    if row.data:
        return row.data[0].get('prefix') or 'QT', row.data[0].get('padding') or 3
    return 'QT', 3

def _next_quote_ref(company_id):
    """Never raises -- a numbering hiccup must not block quote creation.
    Returns None (same as today's manual quote_ref) if anything goes wrong."""
    try:
        year = datetime.now().year
        prefix, padding = _numbering_settings(company_id)
        n = sb.rpc('get_next_quote_number', {'p_company_id': company_id, 'p_year': year}).execute().data
        if not n:
            return None
        return f"{prefix}-{year}-{str(n).zfill(padding)}"
    except Exception:
        traceback.print_exc()
        return None

@app.route('/api/quotes/numbering', methods=['GET'])
def get_quote_numbering():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403
    company_id = claims['company_id']
    prefix, padding = _numbering_settings(company_id)
    seq = sb.table('quote_number_sequences').select('year,next_number').eq('company_id', company_id).order('year').execute()
    return jsonify({'prefix': prefix, 'padding': padding, 'current_year': datetime.now().year, 'sequences': seq.data or []})

@app.route('/api/quotes/numbering', methods=['PATCH'])
def update_quote_numbering():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403
    company_id = claims['company_id']
    d = request.json or {}

    if 'prefix' in d or 'padding' in d:
        prefix = (d.get('prefix') or 'QT').strip()[:20] or 'QT'
        try:
            padding = max(1, min(6, int(d.get('padding') or 3)))
        except (TypeError, ValueError):
            padding = 3
        sb.table('quote_numbering_settings').upsert({
            'company_id': company_id, 'prefix': prefix, 'padding': padding,
            'updated_by': claims['user_id'], 'updated_at': datetime.utcnow().isoformat(),
        }, on_conflict='company_id').execute()

    if 'year' in d and 'next_number' in d:
        try:
            year = int(d['year']); next_number = int(d['next_number'])
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid year or next_number'}), 400
        if next_number < 1:
            return jsonify({'error': 'next_number must be at least 1'}), 400
        sb.table('quote_number_sequences').upsert({
            'company_id': company_id, 'year': year, 'next_number': next_number,
            'updated_at': datetime.utcnow().isoformat(),
        }, on_conflict='company_id,year').execute()

    return get_quote_numbering()

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
        'proposal_data': d.get('proposal_data', {}),
        'total_sell':    d.get('total_sell', 0),
        'total_gp':      d.get('total_gp', 0),
        'margin':        d.get('margin', 0),
        'quote_ref':     _next_quote_ref(claims['company_id']),
    }).execute()
    quote = row.data[0]
    _log_activity(quote['id'], claims['user_id'], 'created', f"Quote \"{quote.get('title') or 'Untitled'}\" created")
    return jsonify({'quote': quote}), 201

# ── Proposal Builder: upload a per-section concept image ────────────────────
# Returns a signed URL only -- it's the caller's job to then set that URL as
# `image` on the relevant section inside quote_data and save the quote as
# normal (schedSave() in index.html). Nothing is written to the quotes row
# here, so an image upload with no follow-up save just leaves an orphaned
# object in storage rather than corrupting quote state.
@app.route('/api/quotes/<qid>/proposal-image', methods=['POST'])
def upload_proposal_image(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not _has_feature(claims, 'proposal_builder'):
        return jsonify({'error': 'The Professional Proposal Builder is not enabled for this company.'}), 403

    existing = sb.table('quotes').select('id').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    d = request.json or {}
    raw = str(d.get('data') or '')
    if not raw: return jsonify({'error': 'No image data provided'}), 400
    if ',' in raw: raw = raw.split(',', 1)[1]  # strip data: prefix
    try:
        data = base64.b64decode(raw)
    except Exception:
        return jsonify({'error': 'Invalid image data'}), 400

    if len(data) > MAX_PROPOSAL_IMAGE_BYTES:
        return jsonify({'error': 'Image is larger than 4 MB. Please use a smaller image.'}), 400
    if len(data) < 100:
        return jsonify({'error': 'That does not look like a valid image.'}), 400

    ctype = str(d.get('content_type') or 'image/jpeg').split(';')[0].strip().lower()
    ext = {'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}.get(ctype)
    if not ext:
        return jsonify({'error': f'Unsupported image type ({ctype or "unknown"}). Use JPG, PNG or WEBP.'}), 400

    section_id = re.sub(r'[^a-zA-Z0-9_-]', '', str(d.get('section_id') or 'section'))[:60]
    path = f"{claims['company_id']}/{qid}-{section_id}-{int(time.time())}.{ext}"
    try:
        sb.storage.from_(PROPOSAL_IMAGE_BUCKET).upload(path, data, {'content-type': ctype})
    except Exception:
        return jsonify({'error': 'Could not save image to storage. Check that the proposal-images bucket exists.'}), 502

    try:
        signed_url = _signed_url(PROPOSAL_IMAGE_BUCKET, path)
    except Exception as e:
        return jsonify({'error': f'Image uploaded but could not generate its access URL: {str(e)[:200]}'}), 502

    return jsonify({'image_url': signed_url})

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
    # it['cost'] is "Net Cost" (Reg-Cost x (1-Sup-Disc%), computed client-side
    # at data entry) -- the true cost to us. disc/discAdd are customer-facing
    # discounts: they reduce the sell price (cad -> unit price) but never our
    # actual cost, so unit_cost/total_cost here use the true Net Cost rather
    # than the customer-discounted basis, matching standard AV/distribution
    # practice -- a customer discount should show up as reduced margin, not
    # be absorbed invisibly by shrinking the cost basis too.
    # Mirrors index.html's calcItem().
    true_cost = _numf(it.get('cost'))
    cad = true_cost * (1 - _numf(it.get('disc')) / 100.0) * (1 - _numf(it.get('discAdd')) / 100.0)
    pct = _numf(it.get('margin'), 0.2)
    if pricing_type == 'margin':
        pct = min(0.95, max(0, pct))
        up = round(cad / (1 - pct)) if pct < 1 else 0
    else:
        up = round(cad * (1 + pct))
    qty = _numf(it.get('qty'), 1)
    return {'unit_cost': true_cost, 'unit_price': up, 'total_price': up * qty, 'total_cost': true_cost * qty}

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
#
# vendor_data is deliberately excluded: it's the internal Vendor payment
# tracker (what's owed to suppliers), not part of the customer-facing
# commercial baseline this lock protects -- and it's only really meaningful
# once a quote is Awarded (that's when a real payable exists), so locking it
# at exactly that point made the tracker unusable for its own stated purpose
# (discovered when a real company had zero vendor-payable entries on any of
# their 30 Awarded quotes -- every attempt to add one after award was being
# silently rejected with the same "quote is locked" error as an edit to
# pricing would get).
CONTENT_FIELDS = ['title','customer','currency','exchange_rate','pricing_type','quote_data','terms_data','proposal_data','total_sell','total_gp','margin']

# A quote may only move forward (be emailed, sent for review, marked Sent/Awarded)
# once its customer is a real record in this company's Customer Master. The editor
# enforces this in the UI too; this is the authoritative check.
CUSTOMER_REQUIRED_MSG = 'Select a valid customer from your Customer Master before continuing (or add the customer first).'

def _customer_in_master(claims, name):
    name = (name or '').strip()
    if not name:
        return False
    try:
        safe = name.replace('%', '').replace(',', ' ').replace('(', ' ').replace(')', ' ').replace('*', ' ').strip()
        if not safe:
            return False
        rows = sb.table('customers').select('name,company_name').eq('company_id', claims['company_id']) \
            .or_(f'name.ilike.{safe},company_name.ilike.{safe}').limit(10).execute().data or []
        want = name.lower()
        return any((r.get('name') or '').strip().lower() == want or (r.get('company_name') or '').strip().lower() == want for r in rows)
    except Exception as e:
        print('customer master check failed (allowing):', e)
        return True

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
    allowed = ['title','customer','status','currency','exchange_rate','pricing_type','quote_data','vendor_data','terms_data','proposal_data','total_sell','total_gp','margin']
    update = {k: d[k] for k in allowed if k in d}
    if 'status' in update and update['status'] not in VALID_QUOTE_STATUSES:
        return jsonify({'error': 'Invalid status.'}), 400

    old_status = existing.data[0].get('status')
    new_status = update.get('status', old_status)
    is_transitioning = new_status != old_status
    if is_transitioning and new_status in ('sent', 'awarded') and not _customer_in_master(claims, update.get('customer', existing.data[0].get('customer'))):
        return jsonify({'error': CUSTOMER_REQUIRED_MSG, 'code': 'CUSTOMER_REQUIRED'}), 422
    touches_content = any(f in update for f in CONTENT_FIELDS)

    if old_status != 'draft' and not is_transitioning and touches_content:
        return jsonify({'error': f'This quote is locked because its status is "{STATUS_LABELS_PY.get(old_status, old_status)}". Set the status back to Draft to edit it.'}), 423

    # Once a quote is Awarded, its status becomes a protected checkpoint --
    # a project may already have been created off it, so only a Company
    # Admin can move it to a different status from here.
    if old_status == 'awarded' and is_transitioning and claims['role'] != 'admin':
        return jsonify({'error': 'This quote is Awarded. Only a Company Admin can change its status from here.'}), 403

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

# ── Delete quote (disabled by policy) ─────────────────────────────────────────
# Once a quote is created it must never be deleted -- quote numbers are an
# audit trail and are referenced by projects, invoices and vouchers. Kept as
# an explicit 403 (rather than just removing the route) so a stale cached
# client gets a clear message instead of a bare 405, and so no role --
# admin included -- can delete through the API.
@app.route('/api/quotes/<qid>', methods=['DELETE'])
def delete_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'error': 'Quotes cannot be deleted once created.'}), 403

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
        'quote_ref':     _next_quote_ref(claims['company_id']),
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

    if not _customer_in_master(claims, quote.get('customer')):
        return jsonify({'error': CUSTOMER_REQUIRED_MSG, 'code': 'CUSTOMER_REQUIRED'}), 422

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
#
# Extra attachments (e.g. product datasheets picked from the user's desktop)
# ride alongside the auto-generated quote PDF in the same Graph fileAttachment
# array. Graph's plain JSON sendMail payload (no upload session) tops out
# around 3 MB per file / ~25 MB per message once base64 overhead is counted --
# these caps are deliberately conservative so a send fails fast client-side
# with a clear message instead of a confusing Graph API error.
MAX_EMAIL_ATTACHMENT_BYTES = 3 * 1024 * 1024        # per extra attachment
MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES = 10 * 1024 * 1024  # all extra attachments combined
MAX_EMAIL_ATTACHMENTS_COUNT = 5

def _safe_attachment_name(name):
    name = re.sub(r'[\r\n\t]+', ' ', (name or 'attachment')).strip()
    return name[:150] or 'attachment'

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
MAX_EMAIL_RECIPIENTS = 20  # per field (To / CC) -- generous, just a sanity cap

def _parse_email_list(raw, field_label):
    """Comma/semicolon-separated addresses (or a JSON array, either works) ->
    (deduped list, error_message). Returns a clear error rather than silently
    dropping a bad address -- a typo'd recipient failing silently on a
    customer-facing email is worse than a blocked send the user can fix and
    resend immediately."""
    parts = raw if isinstance(raw, list) else re.split(r'[;,]', raw or '')
    seen, out, bad = set(), [], []
    for p in parts:
        addr = (p or '').strip()
        if not addr: continue
        if not _EMAIL_RE.match(addr):
            bad.append(addr); continue
        key = addr.lower()
        if key in seen: continue
        seen.add(key); out.append(addr)
    if bad:
        return None, f'{field_label} has an invalid email address: {", ".join(bad)}'
    if len(out) > MAX_EMAIL_RECIPIENTS:
        return None, f'{field_label} has too many addresses (max {MAX_EMAIL_RECIPIENTS}).'
    return out, None

def _decode_email_attachment(att):
    """One {filename, content_type, data} entry from the frontend -> a Graph
    fileAttachment dict, or (None, error_message) if it fails validation.
    Never raises -- a bad attachment should produce a clear 400, not a 500."""
    data_url = att.get('data') or ''
    raw = data_url.split(',', 1)[1] if ',' in data_url else data_url
    try:
        content_bytes = base64.b64decode(raw)
    except Exception:
        return None, f"Could not read attachment \"{att.get('filename') or ''}\" -- it may be corrupted."
    if not content_bytes:
        return None, f"Attachment \"{att.get('filename') or ''}\" is empty."
    if len(content_bytes) > MAX_EMAIL_ATTACHMENT_BYTES:
        mb = MAX_EMAIL_ATTACHMENT_BYTES // (1024*1024)
        return None, f"\"{att.get('filename') or 'Attachment'}\" is over {mb} MB -- please attach a smaller file."
    return {
        '@odata.type': '#microsoft.graph.fileAttachment',
        'name': _safe_attachment_name(att.get('filename')),
        'contentType': att.get('content_type') or 'application/octet-stream',
        'contentBytes': base64.b64encode(content_bytes).decode('ascii'),
    }, None

@app.route('/api/quotes/<qid>/email', methods=['POST'])
def email_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not MS_CLIENT_SECRET:
        return jsonify({'error': 'Email sending is not configured (missing MS_CLIENT_SECRET)'}), 500

    q = sb.table('quotes').select(
        'id,title,customer,status,currency,exchange_rate,quote_data,terms_data,vendor_data,customer_version,created_by'
    ).eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not q.data: return jsonify({'error': 'Not found'}), 404
    quote = q.data[0]
    # Sending to the customer mutates the quote (status, version) just like
    # saving does, so it requires the same edit permission as update_quote().
    if not _can_edit_quote(quote, claims):
        return jsonify({'error': 'You don\'t have permission to email this quote — ask a Company Admin to enable "view all quotes" for your account, or have the quote creator send it'}), 403

    if not _customer_in_master(claims, quote.get('customer')):
        return jsonify({'error': CUSTOMER_REQUIRED_MSG, 'code': 'CUSTOMER_REQUIRED'}), 422

    d = request.json or {}
    to_list, err = _parse_email_list(d.get('to'), 'To')
    if err: return jsonify({'error': err}), 400
    if not to_list:
        return jsonify({'error': 'Recipient email is required'}), 400
    cc_list, err = _parse_email_list(d.get('cc'), 'CC')
    if err: return jsonify({'error': err}), 400
    subject = (d.get('subject') or f"Quotation — {quote.get('title') or 'Sysconic'}").strip()
    message = d.get('message') or ''
    html = d.get('html') or ''
    from_email = (d.get('from_email') or '').strip()
    if not html:
        return jsonify({'error': 'No quote HTML provided to render'}), 400

    extra_attachments = d.get('attachments') or []
    if len(extra_attachments) > MAX_EMAIL_ATTACHMENTS_COUNT:
        return jsonify({'error': f'Attach at most {MAX_EMAIL_ATTACHMENTS_COUNT} files.'}), 400
    graph_extra_attachments = []
    total_extra_bytes = 0
    for att in extra_attachments:
        graph_att, err = _decode_email_attachment(att)
        if err:
            return jsonify({'error': err}), 400
        total_extra_bytes += len(base64.b64decode(graph_att['contentBytes']))
        if total_extra_bytes > MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES:
            mb = MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES // (1024*1024)
            return jsonify({'error': f'Attached files total over {mb} MB combined -- please attach fewer/smaller files.'}), 400
        graph_extra_attachments.append(graph_att)

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

    fname = re.sub(r'[^\w\-]+', '-', f"Quotation-{quote.get('title') or qid[:8]}") + '.pdf'

    # Render the exact same HTML the browser shows, to a PDF -- same
    # self-hosted Puppeteer/Chromium route the "Download PDF" button uses
    # (api/pdf-render.js), called server-to-server instead of PDFShift (whose
    # free monthly credits ran out; see downloadPDF()'s comment in
    # index.html). This is an internal hop to a sibling Vercel function, not
    # a third party, so the incoming request's own Bearer token is forwarded
    # as-is -- both functions verify against the same JWT_SECRET.
    try:
        pdf_resp = requests.post(
            request.host_url.rstrip('/') + '/api/pdf/render2',
            json={'html': html, 'filename': fname},
            headers={'Authorization': request.headers.get('Authorization', ''), 'Content-Type': 'application/json'},
            timeout=40
        )
        if pdf_resp.status_code != 200:
            try:
                err_detail = pdf_resp.json().get('error') or pdf_resp.text[:300]
            except Exception:
                err_detail = pdf_resp.text[:300]
            return jsonify({'error': f'PDF generation failed ({pdf_resp.status_code}): {err_detail}'}), 502
        pdf_b64 = base64.b64encode(pdf_resp.content).decode('ascii')
    except requests.exceptions.Timeout:
        return jsonify({'error': 'PDF generation timed out — please try again.'}), 504
    except Exception as e:
        return jsonify({'error': f'PDF generation failed: {str(e)[:200]}'}), 500

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
            'toRecipients': [{'emailAddress': {'address': a}} for a in to_list],
            'from': {'emailAddress': {'address': sender_email}},
            'attachments': [{
                '@odata.type': '#microsoft.graph.fileAttachment',
                'name': fname,
                'contentType': 'application/pdf',
                'contentBytes': pdf_b64,
            }] + graph_extra_attachments,
        },
        'saveToSentItems': True,
    }
    if cc_list:
        payload['message']['ccRecipients'] = [{'emailAddress': {'address': a}} for a in cc_list]

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
        'quote_id': qid, 'sent_by': claims['user_id'], 'sent_to': ', '.join(to_list),
        'sent_cc': ', '.join(cc_list) or None, 'subject': subject,
        'version_number': next_customer_version,
        'title': quote.get('title'), 'customer': quote.get('customer'),
        'currency': quote.get('currency'), 'exchange_rate': quote.get('exchange_rate'),
        'quote_data': quote.get('quote_data'), 'terms_data': quote.get('terms_data'),
        'vendor_data': quote.get('vendor_data'),
    }).execute()

    quote_update = {'customer_version': next_customer_version}
    if quote.get('status') not in ('awarded', 'lost', 'cancelled'):
        quote_update['status'] = 'sent'
    sb.table('quotes').update(quote_update).eq('id', qid).execute()

    log_note = f"v{next_customer_version} sent to {', '.join(to_list)}"
    if cc_list: log_note += f" (cc: {', '.join(cc_list)})"
    _log_activity(qid, claims['user_id'], 'emailed_to_customer', log_note)

    return jsonify({'ok': True, 'sent_from': sender_email, 'customer_version': next_customer_version})

# ── CRM: Deals pipeline ──────────────────────────────────────────────────────
# A lightweight, configurable sales pipeline that sits BEFORE a formal Quote
# exists (see migration-crm-deals.sql for the table shapes). Routed here
# (rather than a new api/deals.py) because Vercel's plan caps this project's
# deployment at exactly 12 Python serverless functions and that cap is
# already reached — see the same reasoning already applied to
# api/projects.py (which also hosts the unrelated "testimonials" routes).
# Deal-to-quote conversion deliberately does NOT duplicate quote-creation
# logic here: the frontend calls the existing POST /api/quotes exactly like
# "New Quote" does, then PATCHes the deal with the resulting quote id.

DEFAULT_DEAL_STAGES = [
    {'name': 'Lead',        'is_won': False, 'is_lost': False},
    {'name': 'Qualified',   'is_won': False, 'is_lost': False},
    {'name': 'Quote Sent',  'is_won': False, 'is_lost': False},
    {'name': 'Negotiation', 'is_won': False, 'is_lost': False},
    {'name': 'Won',         'is_won': True,  'is_lost': False},
    {'name': 'Lost',        'is_won': False, 'is_lost': True},
]

def _can_view_all_deals(claims):
    """Deliberately reuses the exact same users.can_view_all_quotes flag as
    quotes (not a separate can_view_all_deals column) -- per the agreed v1
    scope, deal visibility mirrors quote visibility exactly, and a company
    admin shouldn't have to manage two nearly-identical toggles per person."""
    return _can_view_all_quotes(claims)

def _log_deal_activity(deal_id, company_id, actor_id, type_, body=None):
    """Best-effort activity/notes entry for a deal's timeline. Never raises --
    a logging failure must not block the actual action."""
    try:
        sb.table('deal_activity').insert({
            'deal_id': deal_id, 'company_id': company_id, 'actor_id': actor_id,
            'type': type_, 'body': body or None,
        }).execute()
    except Exception:
        traceback.print_exc()

def _ensure_default_stages(company_id):
    """Lazily seeds a company's default stage set the first time its pipeline
    is touched, instead of requiring a signup-time bootstrap step. Idempotent:
    if stages already exist (even a partial/customized set from before),
    nothing is created."""
    existing = sb.table('deal_stages').select('id').eq('company_id', company_id).limit(1).execute()
    if existing.data:
        return
    for i, s in enumerate(DEFAULT_DEAL_STAGES):
        sb.table('deal_stages').insert({
            'company_id': company_id, 'name': s['name'], 'sort_order': i,
            'is_won': s['is_won'], 'is_lost': s['is_lost'],
        }).execute()

def _deal_suggestion(deal, stage_by_id):
    """Deterministic (not LLM-based, by design for v1 -- see conversation with
    Nish: ship the pipeline first, keep the first AI-ish feature cheap/fast/
    free) next-best-action nudge shown as a small badge on a deal card.
    Returns None when there's nothing worth surfacing."""
    if deal.get('status') != 'open':
        return None
    stage = stage_by_id.get(deal.get('stage_id')) or {}
    last_activity = deal.get('last_activity_at')
    days_since = None
    if last_activity:
        try:
            dt = datetime.fromisoformat(last_activity.replace('Z', '+00:00'))
            days_since = (datetime.now(dt.tzinfo) - dt).days
        except Exception:
            days_since = None
    close_date = deal.get('expected_close_date')
    if close_date:
        try:
            cd = datetime.fromisoformat(close_date).date()
            if cd < datetime.now().date():
                return f"Expected close date ({close_date}) has passed — update or close this deal"
        except Exception:
            pass
    if days_since is not None:
        if days_since >= 14:
            return f"No activity in {days_since} days — follow up or mark this deal lost"
        if days_since >= 7:
            return f"No activity in {days_since} days — time to follow up"
        if stage.get('sort_order', 0) == 0 and days_since >= 3:
            return "Still a new lead — reach out to qualify it"
    return None

def _deal_row_out(deal, stage_by_id):
    d = dict(deal)
    d['suggestion'] = _deal_suggestion(deal, stage_by_id)
    return d

# ── Stages ───────────────────────────────────────────────────────────────────
@app.route('/api/deals/stages', methods=['GET'])
def list_deal_stages():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    _ensure_default_stages(claims['company_id'])
    rows = sb.table('deal_stages').select('*').eq('company_id', claims['company_id'])\
        .order('sort_order').execute()
    return jsonify({'stages': rows.data or []})

@app.route('/api/deals/stages', methods=['POST'])
def create_deal_stage():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    name = (d.get('name') or '').strip()
    if not name: return jsonify({'error': 'Stage name is required'}), 400
    existing = sb.table('deal_stages').select('sort_order').eq('company_id', claims['company_id'])\
        .order('sort_order', desc=True).limit(1).execute()
    next_order = (existing.data[0]['sort_order'] + 1) if existing.data else 0
    row = sb.table('deal_stages').insert({
        'company_id': claims['company_id'], 'name': name, 'sort_order': next_order,
        'is_won': bool(d.get('is_won')), 'is_lost': bool(d.get('is_lost')),
    }).execute()
    return jsonify({'stage': row.data[0]}), 201

@app.route('/api/deals/stages/<sid>', methods=['PUT'])
def update_deal_stage(sid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403
    row = sb.table('deal_stages').select('id').eq('id', sid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Not found'}), 404
    d = request.json or {}
    patch = {}
    for k in ('name', 'sort_order', 'is_won', 'is_lost'):
        if k in d: patch[k] = d[k]
    if not patch: return jsonify({'error': 'Nothing to update'}), 400
    sb.table('deal_stages').update(patch).eq('id', sid).execute()
    return jsonify({'ok': True})

@app.route('/api/deals/stages/<sid>', methods=['DELETE'])
def delete_deal_stage(sid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403
    row = sb.table('deal_stages').select('id').eq('id', sid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Not found'}), 404
    in_use = sb.table('deals').select('id', count='exact').eq('stage_id', sid).execute()
    if (in_use.count or 0) > 0:
        return jsonify({'error': f'{in_use.count} deal(s) are still in this stage — move them first.'}), 400
    sb.table('deal_stages').delete().eq('id', sid).execute()
    return jsonify({'ok': True})

# ── Insights: single-call dashboard summary ─────────────────────────────────
# Originally the Insights page fired 5-6 parallel calls to 5 different Vercel
# Python functions (quotes.py, deals via quotes.py, projects.py, fsm_contracts.py,
# fsm_tickets.py, fsm_pm.py). Each is an independent serverless function with
# its own cold start — several cold at once (rather than one, shared) is what
# made the page "take ages". This single endpoint does the same aggregation
# server-side, inside one already-warm function (quotes.py gets hit on every
# app boot via loadQuotes()), with lean column selects instead of `select("*")`
# on tables that aren't needed in full for a dashboard summary.
def _insights_parse_dt(s):
    if not s: return None
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00')).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None

# Deliberately NOT just has_page_access(claims, 'insightsFinance') --
# 'insightsFinance' didn't exist before this feature shipped, so every role
# created before today has 'insights': true/false but no 'insightsFinance'
# key at all in its stored permissions dict. has_page_access() would treat
# that absence as False (hidden), which would silently strip finance data
# from every existing role's Insights view the moment this deploys, with no
# admin action taken. Instead: if a role has never had its own
# 'insightsFinance' value explicitly set, it inherits whatever 'insights'
# already granted it -- unchanged behavior for every pre-existing role.
# An admin opts a role OUT of finance visibility by explicitly unchecking
# the new "Insights — Finance & Loans" box (see index.html Roles &
# Permissions), which is the only thing that writes an explicit False here.
def _has_insights_finance_access(claims):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    if 'insightsFinance' in rp:
        return bool(rp['insightsFinance'])
    return bool(rp.get('insights'))

@app.route('/api/insights/summary', methods=['GET'])
def insights_summary():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    payload = _build_insights_payload(claims)
    if not _has_insights_finance_access(claims):
        payload = _strip_finance_sections(payload)
    return jsonify(payload)

# Shared by the Insights "Payables" section above and the daily payables
# reminder cron below (run_payable_reminders()) -- turns a list of quote rows
# (id/title/customer/vendor_data) into the flat list of individual vendor-
# payable line items, each carrying its own amount (incl. the same 5% VAT
# the quote editor itself calculates), target date, and status. Pure
# function, no query -- callers do their own scoping (per-user for the
# Insights payload, company-wide for the reminder email).
def _aggregate_payable_items(payable_quotes):
    items = []
    for q in payable_quotes:
        vd = q.get('vendor_data') or []
        if isinstance(vd, str):
            try:
                vd = json.loads(vd)
            except Exception:
                vd = []
        for v in vd:
            payable = _numf(v.get('payable'))
            if payable <= 0:
                continue
            items.append({
                'quote_id': q.get('id'),
                'quote_title': q.get('title'),
                'customer': q.get('customer'),
                'vendor': v.get('vendor') or 'Vendor',
                'amount': round(payable * 1.05, 2),
                'date': v.get('date') or None,
                'status': v.get('status') or 'pending',
            })
    return items

# Pulled out of insights_summary() so the public share-link endpoint below
# (/api/insights/public) can produce the exact same payload for an
# unauthenticated viewer -- it builds a synthetic admin-level `claims` dict
# (see insights_public()) and calls this directly, rather than duplicating
# every section's query/aggregation logic.
def _build_insights_payload(claims):
    company_id = claims['company_id']
    out = {}

    # ── Sales & Pipeline (Quotes) ────────────────────────────────────────
    # 'quotes' below carries the minimal per-quote fields (title/customer/
    # status/value/date) so the frontend can apply the same date-range
    # filter (All time / This month / This quarter / This year / etc)
    # client-side, exactly like the Zoho Sales section does with its own
    # invoice list -- no extra round trip needed when the user just changes
    # the range dropdown. title/customer are included so a KPI card (e.g.
    # "Awarded value") can drill down to the actual quotes behind it. The
    # top-level aggregates below remain the all-time totals, used for the
    # very first render before any range filter is applied.
    # Cancelled quotes are excluded entirely -- their value must not count
    # toward any total, count, pipeline figure or win rate.
    qq = sb.table('quotes').select('title,customer,status,total_sell,total_gp,created_at').eq('company_id', company_id).neq('status', 'cancelled')
    if not _can_view_all_quotes(claims):
        qq = qq.eq('created_by', claims['user_id'])
    quotes = qq.execute().data or []
    awarded = [q for q in quotes if q.get('status') == 'awarded']
    lost = [q for q in quotes if q.get('status') == 'lost']
    draft_q = [q for q in quotes if q.get('status') == 'draft']
    pending_q = [q for q in quotes if q.get('status') == 'sent']
    open_q = draft_q + pending_q
    closed_count = len(awarded) + len(lost)
    awarded_value = sum(_numf(q.get('total_sell')) for q in awarded)
    awarded_gp = sum(_numf(q.get('total_gp')) for q in awarded)
    out['sales'] = {
        'total_quotes': len(quotes),
        'awarded_count': len(awarded),
        'awarded_value': awarded_value,
        'pending_value': sum(_numf(q.get('total_sell')) for q in pending_q),
        'pending_count': len(pending_q),
        'draft_value': sum(_numf(q.get('total_sell')) for q in draft_q),
        'draft_count': len(draft_q),
        'open_pipeline_value': sum(_numf(q.get('total_sell')) for q in open_q),
        'open_quote_count': len(open_q),
        'win_rate_pct': round(len(awarded) / closed_count * 100) if closed_count else 0,
        'closed_count': closed_count,
        'avg_margin_pct': round(awarded_gp / awarded_value * 1000) / 10 if awarded_value else 0,
        # title/customer included so the UI can drill a KPI card (e.g.
        # "Awarded value") down to the actual quotes behind it, not just
        # recompute the aggregate.
        'quotes': [{
            'title': q.get('title'),
            'customer': q.get('customer'),
            'status': q.get('status'),
            'total_sell': _numf(q.get('total_sell')),
            'total_gp': _numf(q.get('total_gp')),
            'created_at': q.get('created_at'),
        } for q in quotes],
    }

    # ── Deals ────────────────────────────────────────────────────────────
    # is_won/is_lost live on deal_stages, not deals itself — a deal's
    # outcome is derived from which stage it's currently sitting in.
    if has_page_access(claims, 'deals'):
        dq = sb.table('deals').select('value,stage_id').eq('company_id', company_id)
        if not _can_view_all_deals(claims):
            dq = dq.eq('owner_id', claims['user_id'])
        deals = dq.execute().data or []
        stage_rows = sb.table('deal_stages').select('id,is_won,is_lost').eq('company_id', company_id).execute().data or []
        stage_by_id = {s['id']: s for s in stage_rows}
        def _stage(d):
            return stage_by_id.get(d.get('stage_id')) or {}
        open_deals = [d for d in deals if not _stage(d).get('is_won') and not _stage(d).get('is_lost')]
        won_deals = [d for d in deals if _stage(d).get('is_won')]
        out['deals'] = {
            'open_count': len(open_deals),
            'open_value': sum(_numf(d.get('value')) for d in open_deals),
            'won_count': len(won_deals),
        }

    # ── Zoho Sales (org-wide invoices synced via /api/integrations/sync-sales) ──
    # Distinct from Financial Health below, which only covers invoices tied to
    # a tracked Project -- this covers every invoice in the Zoho Books org,
    # broken down by customer, for a true company-wide sales total with
    # invoice-level drill-down. Only shown if the company has actually
    # connected Zoho (checked directly against company_integrations rather
    # than importing api/integrations.py's _connected_providers(), since each
    # Vercel route file has to be fully self-contained -- see the note at the
    # top of api/integrations.py's ZohoAdapter).
    zc = sb.table('company_integrations').select('status').eq('company_id', company_id).eq('provider', 'zoho').execute().data
    if zc and zc[0].get('status') == 'connected':
        inv_rows = sb.table('zoho_invoices').select(
            'id,invoice_number,invoice_date,due_date,status,total,balance,customer_name,synced_at'
        ).eq('company_id', company_id).order('invoice_date', desc=True).execute().data or []
        by_customer = {}
        for r in inv_rows:
            name = (r.get('customer_name') or '').strip() or 'Unknown customer'
            c = by_customer.setdefault(name, {'customer': name, 'invoiced': 0.0, 'collected': 0.0, 'count': 0})
            total = _numf(r.get('total'))
            balance = _numf(r.get('balance'))
            c['invoiced'] += total
            c['collected'] += (total - balance)
            c['count'] += 1
        customers = sorted(by_customer.values(), key=lambda c: c['invoiced'], reverse=True)
        synced_ats = [r['synced_at'] for r in inv_rows if r.get('synced_at')]
        out['zoho_sales'] = {
            'total_invoiced': sum(c['invoiced'] for c in customers),
            'total_collected': sum(c['collected'] for c in customers),
            'invoice_count': len(inv_rows),
            'customers': customers,
            'invoices': [{
                'id': r.get('id'),
                'invoice_number': r.get('invoice_number'),
                'invoice_date': r.get('invoice_date'),
                'due_date': r.get('due_date'),
                'status': r.get('status'),
                'total': _numf(r.get('total')),
                'balance': _numf(r.get('balance')),
                'customer_name': (r.get('customer_name') or '').strip() or 'Unknown customer',
            } for r in inv_rows],
            'last_synced_at': max(synced_ats) if synced_ats else None,
        }

    # ── Zoho Payables (org-wide vendor bills synced via /api/integrations/
    # sync-payables) ─────────────────────────────────────────────────────
    # Mirrors Zoho Sales above but for Accounts Payable: every vendor bill
    # in the Zoho Books org (not just ones tied to a tracked Project),
    # sourced straight from Zoho -- distinct from the manual, quote-linked
    # 'payables' section (Vendor payment tracker) below, which only covers
    # vendor amounts a user typed into a specific Awarded quote. This is
    # what actually answers "what do we owe, and what's due soon" without
    # depending on anyone having entered anything by hand. current/overdue
    # mirrors Zoho's own Accounts Payable dashboard widget (split on
    # due_date vs today); due_within_10d is QTcal's own addition, since
    # that's the actual planning question ("what clears in the next 10
    # days") that Zoho's stock widget doesn't answer directly.
    if zc and zc[0].get('status') == 'connected':
        # project_id IS NULL scopes this to rows written by our own org-wide
        # sync (sync_payables() above). zoho_bills also carries per-project
        # rows from the older Financial Health sync (fetch_bills()) -- since
        # migration-bill-split-projects.sql widened the table's unique
        # constraint to (company_id, zoho_bill_id, project_id), a single
        # real-world bill can have BOTH a project-scoped row (that project's
        # attributed share) and our org-wide row (the full bill) at the same
        # time. Not filtering here would double-count every such bill.
        bill_rows = sb.table('zoho_bills').select(
            'id,bill_number,bill_date,due_date,status,total,balance,vendor_name,synced_at'
        ).eq('company_id', company_id).is_('project_id', 'null').order('due_date').execute().data or []
        now = datetime.utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        in10 = today + timedelta(days=10)
        unpaid = [b for b in bill_rows if _numf(b.get('balance')) > 0]
        def _due(b):
            return _insights_parse_dt(b.get('due_date'))
        overdue = [b for b in unpaid if _due(b) and _due(b) < today]
        overdue_ids = {b.get('id') for b in overdue}
        current = [b for b in unpaid if b.get('id') not in overdue_ids]
        due_soon = [b for b in unpaid if _due(b) and today <= _due(b) <= in10]
        due_soon.sort(key=lambda b: b.get('due_date') or '')
        by_vendor = {}
        for b in bill_rows:
            name = (b.get('vendor_name') or '').strip() or 'Unknown vendor'
            v = by_vendor.setdefault(name, {'vendor': name, 'billed': 0.0, 'outstanding': 0.0, 'count': 0})
            v['billed'] += _numf(b.get('total'))
            v['outstanding'] += _numf(b.get('balance'))
            v['count'] += 1
        vendors = sorted(by_vendor.values(), key=lambda v: v['outstanding'], reverse=True)
        synced_ats = [b['synced_at'] for b in bill_rows if b.get('synced_at')]
        def _bill_row(b):
            return {
                'id': b.get('id'), 'bill_number': b.get('bill_number'), 'bill_date': b.get('bill_date'),
                'due_date': b.get('due_date'), 'status': b.get('status'), 'total': _numf(b.get('total')),
                'balance': _numf(b.get('balance')),
                'vendor_name': (b.get('vendor_name') or '').strip() or 'Unknown vendor',
            }
        out['zoho_payables'] = {
            'total_unpaid': sum(_numf(b.get('balance')) for b in unpaid),
            'total_current': sum(_numf(b.get('balance')) for b in current),
            'total_overdue': sum(_numf(b.get('balance')) for b in overdue),
            'due_within_10d_total': sum(_numf(b.get('balance')) for b in due_soon),
            'due_within_10d_count': len(due_soon),
            'due_within_10d': [_bill_row(b) for b in due_soon],
            'bill_count': len(bill_rows),
            'total_billed': sum(_numf(b.get('total')) for b in bill_rows),
            'vendors': vendors,
            'bills': [_bill_row(b) for b in bill_rows],
            'last_synced_at': max(synced_ats) if synced_ats else None,
        }

    # ── Loans (admin-picked Chart-of-Accounts liability accounts) ──────────
    # Only shown once an admin has actually picked at least one account via
    # /api/integrations/loan-accounts -- see migration-add-loan-accounts.sql
    # for why this can't be auto-detected from Zoho. current_balance is
    # Zoho's own ledger balance (authoritative); total_borrowed/total_repaid
    # are computed at sync time from that account's full transaction history
    # (see sync_loans() in api/integrations.py) -- not fabricated here.
    loan_rows = sb.table('loan_accounts').select(
        'id,account_name,account_type,current_balance,total_repaid,total_borrowed,last_synced_at'
    ).eq('company_id', company_id).eq('is_tracked', True).order('account_name').execute().data or []
    if loan_rows:
        synced_ats = [r['last_synced_at'] for r in loan_rows if r.get('last_synced_at')]
        out['loans'] = {
            'accounts': loan_rows,
            'total_outstanding': sum(_numf(r.get('current_balance')) for r in loan_rows),
            'total_borrowed': sum(_numf(r.get('total_borrowed')) for r in loan_rows),
            'total_repaid': sum(_numf(r.get('total_repaid')) for r in loan_rows),
            'last_synced_at': max(synced_ats) if synced_ats else None,
        }

    # ── CFO summary snapshot (built by /api/integrations/sync-cfo) ─────────
    cfo_row = sb.table('cfo_snapshots').select('payload,synced_at').eq('company_id', company_id).execute().data
    if cfo_row:
        out['cfo'] = {**(cfo_row[0].get('payload') or {}), 'last_synced_at': cfo_row[0].get('synced_at')}

    # ── Salaries (admin-picked Zoho expense accounts; per month, per employee) ─
    sal_acc = sb.table('salary_accounts').select('last_synced_at').eq('company_id', company_id).eq('is_tracked', True).execute().data or []
    if sal_acc:
        sal_rows = sb.table('salary_payments').select('month,employee,amount').eq('company_id', company_id).execute().data or []
        by_month = {}
        for r in sal_rows:
            mth = r.get('month')
            if not mth: continue
            m_ = by_month.setdefault(mth, {})
            m_[r.get('employee') or 'Unspecified'] = m_.get(r.get('employee') or 'Unspecified', 0) + _numf(r.get('amount'))
        months = [{'month': k, 'total': sum(v.values()),
                   'employees': sorted(({'name': n, 'amount': a} for n, a in v.items()), key=lambda e: -e['amount'])}
                  for k, v in sorted(by_month.items(), reverse=True)]
        synced = [r['last_synced_at'] for r in sal_acc if r.get('last_synced_at')]
        out['salaries'] = {'months': months, 'last_synced_at': max(synced) if synced else None}

    # ── Payables (Vendor payment tracker, aggregated across awarded quotes) ─
    # The per-quote "Vendor payment tracker" (Quote editor -> Vendors tab)
    # lets a user record what's owed to each supplier on that PO -- vendor
    # name, payable amount, a target payment date, and a pending/partial/paid
    # status. It's manually entered, not synced from Zoho (Zoho Books has no
    # vendor-bill/AP object wired into this app -- see the Loans section's
    # own comment above for the analogous limitation on the liability side).
    # Only awarded quotes are counted here: an un-awarded quote's vendor
    # entries aren't a real commitment yet. Amount includes the same 5% VAT
    # the quote editor itself calculates (payable*1.05).
    pyq = sb.table('quotes').select('id,title,customer,status,vendor_data').eq('company_id', company_id).eq('status', 'awarded')
    if not _can_view_all_quotes(claims):
        pyq = pyq.eq('created_by', claims['user_id'])
    payable_items = _aggregate_payable_items(pyq.execute().data or [])
    if payable_items:
        payable_items.sort(key=lambda r: (r['status'] == 'paid', r['date'] or '9999-12-31'))
        out['payables'] = {
            'total_payable': sum(r['amount'] for r in payable_items),
            'total_outstanding': sum(r['amount'] for r in payable_items if r['status'] != 'paid'),
            'total_paid': sum(r['amount'] for r in payable_items if r['status'] == 'paid'),
            'items': payable_items,
        }

    # ── Bank Reconciliation (every bank/card account, synced monthly) ──────
    # Only shown once a sync has actually run (Company Profile -> Bank
    # Reconciliation -> Sync Now). accounts = current snapshot (books balance,
    # bank-feed balance where available, live uncategorized count). months =
    # per-account, per-month reconciled/not-reconciled history, derived from
    # Zoho's own reconciliation records -- see sync_bank_reconciliation() in
    # api/integrations.py.
    bank_accounts = sb.table('bank_account_snapshot').select(
        'zoho_account_id,account_name,account_type,bank_name,balance,bank_balance,uncategorized_transactions,last_synced_at'
    ).eq('company_id', company_id).order('account_name').execute().data or []
    if bank_accounts:
        bank_months = sb.table('bank_reconciliation_months').select(
            'zoho_account_id,account_name,month,is_reconciled,closing_balance'
        ).eq('company_id', company_id).order('month').execute().data or []
        synced_ats = [a['last_synced_at'] for a in bank_accounts if a.get('last_synced_at')]
        out['bank_recon'] = {
            'accounts': bank_accounts,
            'months': bank_months,
            'last_synced_at': max(synced_ats) if synced_ats else None,
        }

    # ── Financial Health (Project Performance, synced from Zoho Books) ────
    if _has_feature(claims, 'project_performance'):
        pq = sb.table('projects').select(
            'name,customer,status,revenue_forecast,forecast_gp_pct,invoiced_value,'
            'collected_value,net_cash_position,health_status,project_manager_id,salesperson_id,last_synced_at'
        ).eq('company_id', company_id)
        if not (claims.get('role') == 'admin' or claims.get('can_manage_project_performance')):
            uid = claims.get('user_id')
            pq = pq.or_(f'project_manager_id.eq.{uid},salesperson_id.eq.{uid}')
        rows = pq.execute().data or []
        active = [r for r in rows if r.get('status') == 'active']
        total_value = sum(_numf(r.get('revenue_forecast')) for r in active)
        weighted_gp = (sum(_numf(r.get('forecast_gp_pct')) * _numf(r.get('revenue_forecast')) for r in active) / total_value) if total_value else 0.0
        at_risk = [r for r in rows if r.get('health_status') in ('at_risk', 'critical')]
        financial_synced_ats = [r['last_synced_at'] for r in rows if r.get('last_synced_at')]
        out['financial'] = {
            'active_projects': len(active),
            'total_active_value': total_value,
            'portfolio_gp_pct': round(weighted_gp * 10) / 10,
            'projects_at_risk': len([r for r in rows if r.get('health_status') == 'at_risk']),
            'critical_projects': len([r for r in rows if r.get('health_status') == 'critical']),
            'unbilled_value': sum(_numf(r.get('revenue_forecast')) - _numf(r.get('invoiced_value')) for r in rows),
            # Scoped to Project Performance's tracked projects only -- NOT the
            # same universe as zoho_sales/Business Performance's "Outstanding"
            # (which covers every Zoho invoice org-wide). Two genuinely
            # different, correctly-scoped figures -- see index.html/insights-app.html
            # labels for the "(tracked projects)" vs "(org-wide)" disambiguation.
            'outstanding_receivables': sum(_numf(r.get('invoiced_value')) - _numf(r.get('collected_value')) for r in rows),
            'cash_exposure': sum(min(0.0, _numf(r.get('net_cash_position'))) for r in rows),
            'at_risk_projects': [{'name': r.get('name'), 'customer': r.get('customer'), 'health_status': r.get('health_status')} for r in at_risk[:6]],
            'last_synced_at': max(financial_synced_ats) if financial_synced_ats else None,
            # Full per-project rows so the UI can drill any KPI card down to
            # its contributing projects client-side (active list, receivables
            # sorted by balance, unbilled sorted by gap, cash exposure) rather
            # than the backend needing a dedicated endpoint per KPI.
            'projects': [{
                'name': r.get('name'), 'customer': r.get('customer'), 'status': r.get('status'),
                'health_status': r.get('health_status'),
                'revenue_forecast': _numf(r.get('revenue_forecast')),
                'forecast_gp_pct': _numf(r.get('forecast_gp_pct')),
                'invoiced_value': _numf(r.get('invoiced_value')),
                'collected_value': _numf(r.get('collected_value')),
                'net_cash_position': _numf(r.get('net_cash_position')),
            } for r in rows],
        }

    # ── AMC & Field Service ─────────────────────────────────────────────
    if _has_feature(claims, 'fsm_module') and has_page_access(claims, 'fsm'):
        contracts = sb.table('fsm_contracts').select('contract_number,contract_type,end_date').eq('company_id', company_id).execute().data or []
        now = datetime.utcnow()
        in30, in60, in90 = now + timedelta(days=30), now + timedelta(days=60), now + timedelta(days=90)
        expiring = []
        for c in contracts:
            ed = _insights_parse_dt(c.get('end_date'))
            if ed and now <= ed <= in90:
                expiring.append((ed, c))
        expiring.sort(key=lambda t: t[0])
        exp30 = len([1 for ed, c in expiring if ed <= in30])
        exp60 = len([1 for ed, c in expiring if in30 < ed <= in60])
        exp90 = len(expiring) - exp30 - exp60

        tickets = (sb.table('fsm_tickets').select('ticket_number,subject,status,priority,sla_resolution_due_at')
                   .eq('company_id', company_id).eq('is_deleted', False).execute().data or [])
        open_statuses = {'new', 'acknowledged', 'assigned', 'accepted', 'travelling', 'on_site',
                          'diagnosis', 'waiting_customer', 'waiting_parts', 'waiting_approval'}
        open_tickets = [t for t in tickets if t.get('status') in open_statuses]
        by_priority = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        for t in open_tickets:
            if t.get('priority') in by_priority:
                by_priority[t['priority']] += 1
        now_iso = now.isoformat()
        sla_breaches = [t for t in open_tickets if t.get('sla_resolution_due_at') and str(t['sla_resolution_due_at']) < now_iso]
        sla_breaches.sort(key=lambda t: t.get('sla_resolution_due_at') or '')

        pm = sb.table('fsm_pm_schedules').select('is_active,next_due_date').eq('company_id', company_id).execute().data or []
        in7 = now + timedelta(days=7)
        pm_due = [s for s in pm if s.get('is_active', True) and (d := _insights_parse_dt(s.get('next_due_date'))) and d <= in7]

        out['fsm'] = {
            'contracts_expiring_90d': len(expiring),
            'expiring_30d': exp30, 'expiring_60d': exp60, 'expiring_90d': exp90,
            'upcoming_renewals': [{'contract_number': c.get('contract_number'), 'contract_type': c.get('contract_type'),
                                    'end_date': c.get('end_date')} for ed, c in expiring[:6]],
            'open_tickets': len(open_tickets),
            'by_priority': by_priority,
            'sla_breaches': len(sla_breaches),
            'pm_due_7d': len(pm_due),
            # Full (untruncated) lists for the "tap a KPI -> contributing
            # records" drill-down -- kept separate from upcoming_renewals
            # above (which stays capped at 6 for the existing inline preview
            # list on desktop) since a drill sheet should show everything
            # that makes up the count, not just a preview.
            'all_expiring_contracts': [{'contract_number': c.get('contract_number'), 'contract_type': c.get('contract_type'),
                                         'end_date': c.get('end_date')} for ed, c in expiring],
            'sla_breach_list': [{'ticket_number': t.get('ticket_number'), 'subject': t.get('subject'),
                                  'priority': t.get('priority'), 'sla_resolution_due_at': t.get('sla_resolution_due_at')} for t in sla_breaches],
        }

    return out


# ── Payables reminder (daily cron) ──────────────────────────────────────────
# The Vendor payment tracker is manually entered per quote, easy to lose
# track of once there are several awarded quotes each with their own vendor
# payment dates -- the whole point of this job is to surface "what's due"
# without anyone having to remember to open Insights and check. Same
# CRON_SECRET pattern as api/fsm_notify.py's run-checks and api/integrations.
# py's run-auto-sync; same MS Graph sendMail approach as _send_review_
# notification above. Sends at most one digest per company per day (tracked
# in payable_reminder_state), re-sending the next day for as long as an item
# stays unpaid -- there's no separate per-item dedup, since the exact point
# is to keep reminding until someone marks it paid.
PAYABLE_REMINDER_WARNING_DAYS = 3

def _fetch_company_payable_items(sb_client, company_id):
    rows = sb_client.table('quotes').select('id,title,customer,status,vendor_data') \
        .eq('company_id', company_id).eq('status', 'awarded').execute().data or []
    return _aggregate_payable_items(rows)

def _send_payable_reminder_email(to_email, overdue_items, due_soon_items):
    """Best-effort -- a failed/misconfigured mail send should never crash the
    whole cron run for every other company."""
    if not MS_CLIENT_SECRET or not to_email:
        return False
    try:
        token = get_ms_token()
        if not token:
            return False
        def _rows_html(items):
            return ''.join(
                f"<tr><td style='padding:4px 10px;border-bottom:1px solid #eee'>{it['vendor']}</td>"
                f"<td style='padding:4px 10px;border-bottom:1px solid #eee;color:#667085'>{it['quote_title'] or ''}</td>"
                f"<td style='padding:4px 10px;border-bottom:1px solid #eee;text-align:right'>AED {it['amount']:,.2f}</td>"
                f"<td style='padding:4px 10px;border-bottom:1px solid #eee'>{it['date'] or ''}</td></tr>"
                for it in items
            )
        sections = ''
        if overdue_items:
            sections += (f"<h3 style='color:#b91c1c;margin:18px 0 8px'>Overdue ({len(overdue_items)})</h3>"
                         f"<table style='border-collapse:collapse;width:100%;font-size:13px'>{_rows_html(overdue_items)}</table>")
        if due_soon_items:
            sections += (f"<h3 style='color:#b45309;margin:18px 0 8px'>Due within {PAYABLE_REMINDER_WARNING_DAYS} days ({len(due_soon_items)})</h3>"
                         f"<table style='border-collapse:collapse;width:100%;font-size:13px'>{_rows_html(due_soon_items)}</table>")
        body_html = f"""
        <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1a1a1a;max-width:640px;margin:0 auto;padding:20px">
            <p style="line-height:1.6">Here's what needs attention on your vendor payables:</p>
            {sections}
            <p style="color:#667085;font-size:12px;margin-top:22px;line-height:1.5">From each quote's Vendor payment tracker (Quote editor → Vendors tab). Mark a payable "paid" there once it's settled to stop these reminders for it. Full list: Insights dashboard → Payables.</p>
        </body></html>
        """
        payload = {
            'message': {
                'subject': f"Payables reminder — {len(overdue_items)} overdue, {len(due_soon_items)} due soon",
                'body': {'contentType': 'HTML', 'content': body_html},
                'toRecipients': [{'emailAddress': {'address': to_email}}],
                'from': {'emailAddress': {'address': SENDER_EMAIL}},
            },
            'saveToSentItems': True,
        }
        r = requests.post(
            f'https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail',
            json=payload, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            timeout=15
        )
        return r.status_code == 202
    except Exception:
        traceback.print_exc()
        return False

@app.route('/api/insights/run-payable-reminders', methods=['GET', 'POST'])
def run_payable_reminders():
    auth = request.headers.get('Authorization', '')
    provided = auth[7:] if auth.startswith('Bearer ') else ''
    provided = provided or request.args.get('secret') or request.headers.get('X-Cron-Secret') or ''
    if not CRON_SECRET or provided != CRON_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    today = datetime.utcnow().date()
    today_iso = today.isoformat()
    warn_by_iso = (today + timedelta(days=PAYABLE_REMINDER_WARNING_DAYS)).isoformat()
    companies = sb.table('companies').select('id').execute().data or []
    results = []
    for co in companies:
        cid = co['id']
        items = _fetch_company_payable_items(sb, cid)
        relevant = [it for it in items if it['status'] != 'paid' and it['date']]
        overdue = [it for it in relevant if it['date'] < today_iso]
        due_soon = [it for it in relevant if today_iso <= it['date'] <= warn_by_iso]
        if not overdue and not due_soon:
            continue
        state = sb.table('payable_reminder_state').select('last_sent_date').eq('company_id', cid).execute().data
        if state and state[0].get('last_sent_date') == today_iso:
            continue  # already sent today -- try again tomorrow if still unpaid
        admins = sb.table('users').select('email').eq('company_id', cid).eq('role', 'admin').execute().data or []
        admin_emails = [a['email'] for a in admins if a.get('email')]
        sent = sum(1 for email in admin_emails if _send_payable_reminder_email(email, overdue, due_soon))
        if sent:
            sb.table('payable_reminder_state').upsert({
                'company_id': cid, 'last_sent_date': today_iso, 'updated_at': datetime.utcnow().isoformat(),
            }, on_conflict='company_id').execute()
            results.append({'company_id': cid, 'overdue': len(overdue), 'due_soon': len(due_soon), 'emails_sent': sent})
    return jsonify({'companies_processed': len(companies), 'results': results})


# ── Insights public share link ──────────────────────────────────────────────
# Lets an admin turn on a URL that opens the Insights dashboard with no login
# at all -- for a phone home-screen shortcut or an office TV display where
# typing a password every time isn't practical. The link is permanent,
# built from the company's own id (see create_insights_share_link() below)
# rather than a separate rotating secret -- an earlier version used a
# regenerable random token, but the user explicitly asked for a fixed link
# instead, after being told this means a leaked link can only be shut off by
# disabling sharing entirely (re-enabling gives back the SAME url, since
# it's derived from company_id, not reissued). Anyone holding the link sees
# the full company-wide Insights dashboard (no per-user filtering) with no
# further gate, so this remains an explicit, admin-only opt-in, not a default.
@app.route('/api/insights/share-link', methods=['GET'])
def get_insights_share_link():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims.get('role') != 'admin': return jsonify({'error': 'Admin only'}), 403
    row = sb.table('companies').select('insights_share_enabled').eq('id', claims['company_id']).execute().data
    enabled = bool(row[0].get('insights_share_enabled')) if row else False
    return jsonify({'enabled': enabled})

@app.route('/api/insights/share-link', methods=['POST'])
def create_insights_share_link():
    # Per the user's explicit choice (chat, Sept 2026): the link is now
    # permanent -- built from the company's own id, which never changes --
    # rather than a separate rotating secret token. This trades away the
    # "revoke this one link, issue a fresh one" ability: disabling and
    # re-enabling produces the SAME url, since it's derived from company_id,
    # not a regenerated secret. The company_id itself (a random UUID) is
    # still not realistically guessable, but the earlier hashed-token design
    # additionally let a leaked link be invalidated without losing sharing
    # entirely -- that's gone now. Admin was told this plainly before opting
    # for it over keeping the rotating-token design.
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims.get('role') != 'admin': return jsonify({'error': 'Admin only'}), 403
    sb.table('companies').update({'insights_share_enabled': True}).eq('id', claims['company_id']).execute()
    return jsonify({'ok': True})

@app.route('/api/insights/share-link', methods=['DELETE'])
def revoke_insights_share_link():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims.get('role') != 'admin': return jsonify({'error': 'Admin only'}), 403
    sb.table('companies').update({'insights_share_enabled': False}).eq('id', claims['company_id']).execute()
    return jsonify({'ok': True})


# A handful of Insights sections carry the company's most sensitive
# financial data -- actual bank/loan balances, vendor payment obligations,
# and project-level margins/cash exposure. Two independent callers strip
# these out of the payload with this same helper:
#   1. insights_public() (below) -- always, unconditionally. The share link
#      is intentionally permanent and requires no login or token rotation
#      (see create_insights_share_link()/revoke_insights_share_link():
#      revoking and re-sharing is the only "rotation" available), which is a
#      real exposure if the link ever leaks. Rather than add the login/
#      token/expiry the mobile redesign spec originally called for (which
#      would break the "one permanent link, no sign-in" UX this was
#      deliberately built for), the mitigation is on the content side.
#   2. insights_summary() (below) -- for a signed-in user whose role lacks
#      the 'insightsFinance' RBAC page key (see RBAC_PAGE_KEYS in
#      api/auth.py), e.g. a sales or service role that should see the
#      Sales/Pipeline and Zoho Sales figures but not the company's bank
#      balances or project margins.
# Revenue-side sections (Sales & Pipeline, Zoho Sales, Business Performance)
# and operational sections (AMC & Field Service) are not considered
# sensitive in the same way and are never stripped by this helper.
_FINANCE_SECTIONS = ('loans', 'bank_recon', 'payables', 'zoho_payables', 'financial', 'salaries', 'cfo')

def _strip_finance_sections(payload, keep=()):
    # `keep` lets a caller deliberately exempt specific sections from the
    # blanket strip -- used by insights_public() below to expose the org-wide
    # Zoho Payables widget on the public share link (per explicit product
    # decision) while still hiding Loans, Bank Reconciliation, the manual
    # per-quote Payables tracker, and the Financial Health section there.
    hidden_any = False
    for key in _FINANCE_SECTIONS:
        if key in keep:
            continue
        if payload.pop(key, None) is not None:
            hidden_any = True
    payload['finance_hidden'] = hidden_any
    return payload

@app.route('/api/insights/public', methods=['GET'])
def insights_public():
    # "token" here is the company's own id (see create_insights_share_link()
    # above) -- validated as a UUID before hitting the DB so a malformed
    # value 404s cleanly instead of erroring on the query.
    token = (request.args.get('token') or '').strip()
    if not token: return jsonify({'error': 'Missing token'}), 400
    try:
        uuid.UUID(token)
    except ValueError:
        return jsonify({'error': 'This link is invalid or has been revoked.'}), 404
    row = sb.table('companies').select('id,name,insights_share_enabled,features')\
        .eq('id', token).execute().data
    if not row or not row[0].get('insights_share_enabled'):
        return jsonify({'error': 'This link is invalid or has been revoked.'}), 404
    company = row[0]
    # role='admin' gives the same full, company-wide (not per-user-filtered)
    # view an admin gets when opening Insights normally -- the right default
    # for a shared dashboard link that isn't tied to any one person. Content
    # is then narrowed for the public viewer -- see _strip_hidden_sections_
    # for_share() above.
    fake_claims = {
        'company_id': company['id'], 'role': 'admin', 'user_id': None,
        'features': company.get('features') or {}, 'role_permissions': None,
    }
    payload = _build_insights_payload(fake_claims)
    payload = _strip_finance_sections(payload, keep=('zoho_payables', 'salaries', 'cfo'))
    # zoho_sales isn't in _FINANCE_SECTIONS (it's revenue-side, and a
    # signed-in sales role is meant to see it -- see the comment above
    # _FINANCE_SECTIONS). But it also carries the AR figures this public,
    # unauthenticated link should never expose: outstanding/overdue invoice
    # balances, plus every customer name and invoice amount in the org. Only
    # this anonymous endpoint strips it; insights_summary()'s role-based
    # gating for signed-in users is unchanged.
    payload.pop('zoho_sales', None)
    payload['company_name'] = company.get('name')
    return jsonify(payload)


# ── Quote-to-invoice reconciliation (Business Performance Dashboard) ────────
# QTCal quotes/projects and Zoho invoices have no reliable existing link (see
# migration-add-quote-invoice-matches.sql's header comment for the full
# rationale). Rather than guess an "awarded-but-uninvoiced" figure from
# nothing, this suggests candidate matches by customer-name overlap + amount
# closeness + date proximity -- mirrors the confidence-scoring approach
# api/projects.py already uses for PO-line-to-quoted-line matching
# (_match_baseline_line) -- and an admin confirms or rejects each one before
# it counts toward any total.
_QIM_WORD_RE = re.compile(r'[a-z0-9]+')

def _qim_tokens(s):
    return set(_QIM_WORD_RE.findall((s or '').lower()))

def _qim_score(quote, invoice):
    """Returns (score 0..1, basis dict). All three signals are weighted so a
    strong customer-name match carries the most weight (free-text customer
    names are the only thing connecting these two systems at all), amount
    plausibility second (an invoice can legitimately be a partial-invoice
    fraction of the quote, so this rewards "positive amount up to slightly
    over the quote total" rather than requiring an exact match), and date
    proximity last (a loose sanity check, not a hard requirement)."""
    q_tokens, i_tokens = _qim_tokens(quote.get('customer')), _qim_tokens(invoice.get('customer_name'))
    overlap = (len(q_tokens & i_tokens) / len(q_tokens | i_tokens)) if (q_tokens and i_tokens) else 0.0

    q_total, i_total = _numf(quote.get('total_sell')), _numf(invoice.get('total'))
    amount_ratio, amount_score = None, 0.0
    if q_total > 0 and i_total > 0:
        amount_ratio = i_total / q_total
        # A partial invoice (a fraction of the quote) is still a plausible
        # match -- score it by how much of the quote it covers. An invoice
        # noticeably larger than the quote is more likely an unrelated job.
        amount_score = amount_ratio if amount_ratio <= 1.05 else max(0.0, 1.0 - (amount_ratio - 1.05))

    days_apart, date_score = None, 0.0
    q_date, i_date = _insights_parse_dt(quote.get('created_at')), _insights_parse_dt(invoice.get('invoice_date'))
    if q_date and i_date:
        days_apart = abs((i_date - q_date).days)
        date_score = max(0.0, 1.0 - days_apart / 180.0)  # falls to 0 past ~6 months apart

    score = overlap * 0.5 + amount_score * 0.35 + date_score * 0.15
    basis = {'customer_overlap': round(overlap, 2), 'amount_ratio': round(amount_ratio, 2) if amount_ratio is not None else None, 'days_apart': days_apart}
    return score, basis

_QIM_SUGGEST_THRESHOLD = 0.35
_QIM_MAX_PER_QUOTE = 3

@app.route('/api/insights/quote-invoice-matches/generate', methods=['POST'])
def generate_quote_invoice_matches():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims.get('role') != 'admin': return jsonify({'error': 'Admin only'}), 403
    company_id = claims['company_id']

    quotes = sb.table('quotes').select('id,customer,total_sell,created_at')\
        .eq('company_id', company_id).eq('status', 'awarded').execute().data or []
    invoices = sb.table('zoho_invoices').select('id,customer_name,total,invoice_date')\
        .eq('company_id', company_id).execute().data or []
    if not quotes or not invoices:
        return jsonify({'created': 0, 'considered_quotes': len(quotes), 'candidate_invoices': len(invoices)})

    existing = sb.table('quote_invoice_matches').select('quote_id,zoho_invoice_id,status')\
        .eq('company_id', company_id).execute().data or []
    existing_pairs = {(m['quote_id'], m['zoho_invoice_id']) for m in existing}
    # An invoice already confirmed against one quote is spoken for -- never
    # suggest it for another, or the same revenue could be confirmed twice.
    confirmed_invoice_ids = {m['zoho_invoice_id'] for m in existing if m['status'] == 'confirmed'}
    candidate_invoices = [i for i in invoices if i['id'] not in confirmed_invoice_ids]

    to_insert = []
    for q in quotes:
        scored = []
        for inv in candidate_invoices:
            if (q['id'], inv['id']) in existing_pairs:
                continue  # already suggested/confirmed/rejected this exact pair before
            score, basis = _qim_score(q, inv)
            if score >= _QIM_SUGGEST_THRESHOLD:
                scored.append((score, inv, basis))
        scored.sort(key=lambda t: t[0], reverse=True)
        for score, inv, basis in scored[:_QIM_MAX_PER_QUOTE]:
            to_insert.append({
                'company_id': company_id, 'quote_id': q['id'], 'zoho_invoice_id': inv['id'],
                'match_score': round(score, 4), 'match_basis': basis, 'status': 'suggested',
            })

    if to_insert:
        sb.table('quote_invoice_matches').insert(to_insert).execute()
    return jsonify({'created': len(to_insert), 'considered_quotes': len(quotes), 'candidate_invoices': len(candidate_invoices)})

@app.route('/api/insights/quote-invoice-matches', methods=['GET'])
def list_quote_invoice_matches():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    company_id = claims['company_id']
    q = sb.table('quote_invoice_matches').select('*').eq('company_id', company_id)
    status = (request.args.get('status') or '').strip()
    if status in ('suggested', 'confirmed', 'rejected'):
        q = q.eq('status', status)
    rows = q.order('created_at', desc=True).execute().data or []

    quote_ids = list({r['quote_id'] for r in rows})
    invoice_ids = list({r['zoho_invoice_id'] for r in rows})
    quotes_by_id = {r['id']: r for r in (sb.table('quotes').select('id,title,customer,total_sell,quote_ref')
        .in_('id', quote_ids).execute().data or [])} if quote_ids else {}
    invoices_by_id = {r['id']: r for r in (sb.table('zoho_invoices').select('id,invoice_number,invoice_date,total,balance,customer_name')
        .in_('id', invoice_ids).execute().data or [])} if invoice_ids else {}

    return jsonify({'matches': [{
        'id': r['id'], 'status': r['status'], 'match_score': r.get('match_score'), 'match_basis': r.get('match_basis'),
        'created_at': r.get('created_at'), 'quote': quotes_by_id.get(r['quote_id']), 'invoice': invoices_by_id.get(r['zoho_invoice_id']),
    } for r in rows]})

@app.route('/api/insights/quote-invoice-matches/<mid>/confirm', methods=['POST'])
def confirm_quote_invoice_match(mid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims.get('role') != 'admin': return jsonify({'error': 'Admin only'}), 403
    company_id = claims['company_id']
    row = sb.table('quote_invoice_matches').select('*').eq('id', mid).eq('company_id', company_id).execute().data
    if not row: return jsonify({'error': 'Not found'}), 404
    m = row[0]
    sb.table('quote_invoice_matches').update({
        'status': 'confirmed', 'confirmed_by': claims['user_id'], 'confirmed_at': datetime.utcnow().isoformat(),
    }).eq('id', mid).execute()
    # Enforce "an invoice counts once": any other still-pending suggestion
    # against this same invoice is no longer valid now that it's spoken for.
    sb.table('quote_invoice_matches').update({'status': 'rejected'})\
        .eq('zoho_invoice_id', m['zoho_invoice_id']).eq('status', 'suggested').neq('id', mid).execute()
    return jsonify({'ok': True})

@app.route('/api/insights/quote-invoice-matches/<mid>/reject', methods=['POST'])
def reject_quote_invoice_match(mid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if claims.get('role') != 'admin': return jsonify({'error': 'Admin only'}), 403
    company_id = claims['company_id']
    existing = sb.table('quote_invoice_matches').select('id').eq('id', mid).eq('company_id', company_id).execute().data
    if not existing: return jsonify({'error': 'Not found'}), 404
    sb.table('quote_invoice_matches').update({'status': 'rejected'}).eq('id', mid).execute()
    return jsonify({'ok': True})


# ── Deals ────────────────────────────────────────────────────────────────────
@app.route('/api/deals', methods=['GET'])
def list_deals():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    _ensure_default_stages(claims['company_id'])
    q = sb.table('deals').select('*').eq('company_id', claims['company_id'])
    if not _can_view_all_deals(claims):
        q = q.eq('owner_id', claims['user_id'])
    rows = q.order('updated_at', desc=True).execute()
    stages = sb.table('deal_stages').select('*').eq('company_id', claims['company_id']).execute()
    stage_by_id = {s['id']: s for s in (stages.data or [])}
    deals_out = [_deal_row_out(r, stage_by_id) for r in (rows.data or [])]
    return jsonify({'deals': deals_out, 'stages': stages.data or []})

@app.route('/api/deals', methods=['POST'])
def create_deal():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json or {}
    title = (d.get('title') or '').strip()
    if not title: return jsonify({'error': 'Deal title is required'}), 400
    _ensure_default_stages(claims['company_id'])
    stage_id = d.get('stage_id')
    if not stage_id:
        first = sb.table('deal_stages').select('id').eq('company_id', claims['company_id'])\
            .order('sort_order').limit(1).execute()
        stage_id = first.data[0]['id'] if first.data else None
    row = sb.table('deals').insert({
        'company_id': claims['company_id'], 'created_by': claims['user_id'],
        'owner_id': d.get('owner_id') or claims['user_id'],
        'title': title,
        'customer_id': d.get('customer_id') or None,
        'customer_name': d.get('customer_name') or '',
        'value': d.get('value') or 0,
        'currency': d.get('currency') or 'AED',
        'stage_id': stage_id,
        'expected_close_date': d.get('expected_close_date') or None,
        'notes': d.get('notes') or '',
    }).execute()
    deal = row.data[0]
    _log_deal_activity(deal['id'], claims['company_id'], claims['user_id'], 'created', f"Deal \"{title}\" created")
    return jsonify({'deal': deal}), 201

def _get_deal_or_403(did, claims):
    row = sb.table('deals').select('*').eq('id', did).eq('company_id', claims['company_id']).execute()
    if not row.data: return None, (jsonify({'error': 'Not found'}), 404)
    deal = row.data[0]
    if deal.get('owner_id') != claims['user_id'] and not _can_view_all_deals(claims):
        return None, (jsonify({'error': 'Forbidden'}), 403)
    return deal, None

@app.route('/api/deals/<did>', methods=['GET'])
def get_deal(did):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    deal, err = _get_deal_or_403(did, claims)
    if err: return err
    activity = sb.table('deal_activity').select('*').eq('deal_id', did)\
        .order('created_at', desc=True).execute()
    stages = sb.table('deal_stages').select('*').eq('company_id', claims['company_id']).execute()
    stage_by_id = {s['id']: s for s in (stages.data or [])}
    return jsonify({'deal': _deal_row_out(deal, stage_by_id), 'activity': activity.data or []})

@app.route('/api/deals/<did>', methods=['PUT'])
def update_deal(did):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    deal, err = _get_deal_or_403(did, claims)
    if err: return err
    d = request.json or {}
    patch = {}
    for k in ('title', 'customer_id', 'customer_name', 'value', 'currency', 'expected_close_date',
              'notes', 'owner_id', 'converted_quote_id'):
        if k in d: patch[k] = d[k]

    touched = False
    if 'stage_id' in d and d['stage_id'] != deal.get('stage_id'):
        patch['stage_id'] = d['stage_id']
        stages = sb.table('deal_stages').select('*').eq('id', d['stage_id']).execute()
        new_stage = stages.data[0] if stages.data else {}
        old_stages = sb.table('deal_stages').select('name').eq('id', deal.get('stage_id')).execute()
        old_name = old_stages.data[0]['name'] if old_stages.data else '—'
        _log_deal_activity(did, claims['company_id'], claims['user_id'], 'stage_change',
                            f"Moved from \"{old_name}\" to \"{new_stage.get('name','')}\"")
        touched = True
        # Auto-close the deal if the new stage is a designated won/lost stage,
        # so status and stage never silently disagree with each other.
        if new_stage.get('is_won'):
            patch['status'] = 'won'
        elif new_stage.get('is_lost'):
            patch['status'] = 'lost'

    if 'status' in d and d['status'] in ('open', 'won', 'lost') and d['status'] != deal.get('status'):
        patch['status'] = d['status']
        if d['status'] == 'lost' and d.get('lost_reason'):
            patch['lost_reason'] = d['lost_reason']
        _log_deal_activity(did, claims['company_id'], claims['user_id'],
                            d['status'] if d['status'] in ('won', 'lost') else 'note',
                            f"Marked {d['status']}" + (f" — {d.get('lost_reason')}" if d.get('lost_reason') else ''))
        touched = True

    if 'converted_quote_id' in d and d['converted_quote_id']:
        _log_deal_activity(did, claims['company_id'], claims['user_id'], 'converted',
                            'Converted to a Quote')
        touched = True

    if not patch:
        return jsonify({'error': 'Nothing to update'}), 400
    if touched or any(k in patch for k in ('title', 'value', 'notes')):
        patch['last_activity_at'] = datetime.utcnow().isoformat()
    patch['updated_at'] = datetime.utcnow().isoformat()
    sb.table('deals').update(patch).eq('id', did).execute()
    return jsonify({'ok': True})

@app.route('/api/deals/<did>', methods=['DELETE'])
def delete_deal(did):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    deal, err = _get_deal_or_403(did, claims)
    if err: return err
    sb.table('deals').delete().eq('id', did).execute()
    return jsonify({'ok': True})

@app.route('/api/deals/<did>/notes', methods=['POST'])
def add_deal_note(did):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    deal, err = _get_deal_or_403(did, claims)
    if err: return err
    d = request.json or {}
    body = (d.get('body') or '').strip()
    if not body: return jsonify({'error': 'Note text is required'}), 400
    _log_deal_activity(did, claims['company_id'], claims['user_id'], 'note', body)
    sb.table('deals').update({'last_activity_at': datetime.utcnow().isoformat()}).eq('id', did).execute()
    return jsonify({'ok': True}), 201

if __name__ == '__main__':
    app.run(debug=True)
