"""
api/proposals.py — Proposals module (milestone M1: foundation)

Separate Proposals register + dedicated Proposal AI settings. Preview
generation, regeneration and full composition arrive in later milestones --
nothing here calls a generative model. See QTCal_Proposal_Builder_Design_
Specification.md. The older single-quote generator (api/proposal.py,
'proposal_builder' flag) is untouched.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""
from flask import Flask, request, jsonify, g
import os, re, json, hashlib, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta

from api.auth import verify_token, get_sb

app = Flask(__name__)


def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))


def is_admin(claims):
    return claims.get('role') == 'admin'


@app.before_request
def gate():
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'unauthorized'}), 401
    if not (claims.get('features') or {}).get('proposals'):
        return jsonify({'error': 'Proposals is not enabled for this company.'}), 403
    if not has_page_access(claims, 'proposals'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    g.claims = claims
    return None


# ── Numbering: PR-YYYY-000123, atomic via proposal_next_number() ──────────────
def _next_reference(sb, company_id):
    year = datetime.utcnow().year
    r = sb.rpc('proposal_next_number', {'p_company_id': company_id, 'p_year': year}).execute()
    seq = r.data
    if isinstance(seq, list):
        seq = seq[0].get('proposal_next_number') if seq else 1
    return f'PR-{year}-{str(seq).zfill(6)}'


def _stable_hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


# ── Customer-safe source projection from a quote ──────────────────────────────
# Cost, margin, supplier discount and Reg-Cost are NEVER copied: the projection
# is built field-by-field from an allowlist, not by deleting from a full row.
def _quote_terms(quote):
    """The quotation's own terms & conditions lines (payment, delivery, warranty, validity ...)."""
    td = quote.get('terms_data') or []
    if isinstance(td, str):
        try: td = json.loads(td)
        except Exception: td = []
    out = []
    for t in td if isinstance(td, list) else []:
        if isinstance(t, dict):
            if t.get('on') is False or t.get('include') is False: continue
            t = t.get('text') or t.get('t') or ''
        t = str(t or '').strip()
        if t: out.append(t[:400])
    return out[:30]


def _safe_quote_projection(quote, option_idx, can_commercial):
    qd = quote.get('quote_data') or []
    opt = qd[option_idx] if 0 <= option_idx < len(qd) else (qd[0] if qd else {})
    lines, n = [], 0
    pt = quote.get('pricing_type') or 'markup'
    for sec in (opt.get('sections') or []):
        for it in (sec.get('items') or []):
            if not (it.get('brand') or it.get('model') or it.get('desc')):
                continue
            n += 1
            line = {
                'source_line_id': it.get('id') or f'line-{n}',
                'section': sec.get('name') or '',
                'kind': 'equipment',
                'manufacturer': it.get('brand') or '',
                'model': it.get('model') or '',
                'description': it.get('desc') or '',
                'quantity': it.get('qty') or 1,
                'unit': it.get('uom') or 'Pcs',
                'inclusion': 'included',
            }
            if can_commercial:
                cost = float(it.get('cost') or 0)
                cad = cost * (1 - float(it.get('disc') or 0) / 100) * (1 - float(it.get('discAdd') or 0) / 100)
                pct = float(it.get('margin') or 0.2)
                up = round(cad / (1 - min(0.95, max(0, pct))) if pt == 'margin' else cad * (1 + pct))
                line['unit_selling_price'] = up
                line['line_total'] = up * (it.get('qty') or 1)
            lines.append(line)
    snap = {
        'source_kind': 'quotation',
        'quote_ref': quote.get('quote_ref') or '',
        'option_label': opt.get('label') or '',
        'option_index': option_idx if 0 <= option_idx < len(qd) else 0,
        'customer': quote.get('customer') or opt.get('customer') or '',
        'project': quote.get('title') or '',
        'lines': lines,
        'visibility': 'commercial' if can_commercial else 'technical',
        'terms': _quote_terms(quote),
    }
    if can_commercial:
        sub = sum(l.get('line_total', 0) for l in lines)
        vat = float(opt.get('vatRate') or 0) if opt.get('vatEnabled', True) else 0
        snap['commercials'] = {'currency': quote.get('currency') or 'AED', 'subtotal': sub,
                               'vat_rate': vat, 'vat': sub * vat / 100, 'total': sub * (1 + vat / 100)}
    return snap


def _can_commercial(claims):
    return has_page_access(claims, 'quotes')


def _quote_visible(claims, quote):
    if quote.get('company_id') != claims['company_id']:
        return False
    if is_admin(claims) or claims.get('role_permissions') is None:
        return True
    # Role-restricted users: own quotes only (conservative).
    return quote.get('created_by') == claims.get('user_id')


def _public(p):
    return {k: p.get(k) for k in ('id', 'reference', 'title', 'customer_name', 'source_kind', 'quote_id', 'quote_ref',
                                  'instruction', 'owner_id', 'lifecycle', 'stage', 'lock_version', 'archived_at',
                                  'selected_preview_id', 'created_at', 'updated_at')}


# ── Register ──────────────────────────────────────────────────────────────────
@app.route('/api/proposals', methods=['GET'])
def list_proposals():
    sb, claims = get_sb(), g.claims
    search = (request.args.get('search') or '').strip().replace('%', ' ').replace(',', ' ')
    archived = request.args.get('archived') == '1'
    try: limit = min(100, max(1, int(request.args.get('limit', 25))))
    except Exception: limit = 25
    try: offset = max(0, int(request.args.get('offset', 0)))
    except Exception: offset = 0
    q = sb.table('proposals').select('*', count='exact').eq('company_id', claims['company_id'])\
        .eq('lifecycle', 'archived' if archived else 'active')
    if search:
        q = q.or_(f'reference.ilike.%{search}%,title.ilike.%{search}%,customer_name.ilike.%{search}%,quote_ref.ilike.%{search}%')
    rows = q.order('updated_at', desc=True).order('id', desc=True).range(offset, offset + limit - 1).execute()
    return jsonify({'proposals': [_public(r) for r in (rows.data or [])], 'total': rows.count or 0})


@app.route('/api/proposals/quotes', methods=['GET'])
def search_quotes():
    """Source picker: authorized quotes only."""
    sb, claims = get_sb(), g.claims
    search = (request.args.get('search') or '').strip().replace('%', ' ').replace(',', ' ')
    q = sb.table('quotes').select('id,quote_ref,title,customer,status,created_by,updated_at')\
        .eq('company_id', claims['company_id']).neq('status', 'cancelled')
    if not is_admin(claims) and claims.get('role_permissions') is not None:
        q = q.eq('created_by', claims.get('user_id'))
    if search:
        q = q.or_(f'quote_ref.ilike.%{search}%,customer.ilike.%{search}%,title.ilike.%{search}%')
    rows = q.order('updated_at', desc=True).limit(15).execute().data or []
    return jsonify({'quotes': [{k: r.get(k) for k in ('id', 'quote_ref', 'title', 'customer', 'status')} for r in rows]})


@app.route('/api/proposals', methods=['POST'])
def create_proposal():
    sb, claims = get_sb(), g.claims
    d = request.json or {}
    src = d.get('source') or {}
    if src.get('kind') == 'upload':
        return _create_from_upload(sb, claims, d, src)
    if src.get('kind') != 'quotation' or not src.get('id'):
        return jsonify({'error': 'Choose a quotation as the source. (BOM sources arrive in a later release.)'}), 400
    qr = sb.table('quotes').select('*').eq('id', src['id']).eq('company_id', claims['company_id']).execute().data
    if not qr or not _quote_visible(claims, qr[0]):
        return jsonify({'error': 'Quotation not found.'}), 404
    quote = qr[0]
    if quote.get('status') == 'cancelled':
        return jsonify({'error': 'This quotation is cancelled.'}), 400
    try: opt_idx = int(src.get('option_index', 0))
    except Exception: opt_idx = 0
    snap = _safe_quote_projection(quote, opt_idx, _can_commercial(claims))
    if not snap['lines']:
        return jsonify({'error': 'This quotation has no equipment or service lines to build a proposal from.'}), 422
    ref = _next_reference(sb, claims['company_id'])
    row = sb.table('proposals').insert({
        'company_id': claims['company_id'], 'reference': ref,
        'title': (d.get('title') or '').strip()[:200] or (quote.get('title') or snap['customer'] or None),
        'customer_name': snap['customer'], 'source_kind': 'quotation',
        'quote_id': quote['id'], 'quote_ref': quote.get('quote_ref'),
        'instruction': (d.get('instruction') or '').strip()[:2000] or None,
        'owner_id': claims.get('user_id'), 'stage': 'draft',
    }).execute().data[0]
    sb.table('proposal_source_snapshots').insert({
        'company_id': claims['company_id'], 'proposal_id': row['id'], 'safe_source': snap,
        'source_hash': _stable_hash(snap), 'visibility': snap['visibility'], 'created_by': claims.get('user_id'),
    }).execute()
    return jsonify({'proposal': _public(row)}), 201



def _customer_in_master(sb, claims, name):
    name = (name or '').strip()
    if not name: return False
    try:
        safe = name.replace('%', '').replace(',', ' ').replace('(', ' ').replace(')', ' ').replace('*', ' ').strip()
        if not safe: return False
        rows = sb.table('customers').select('name,company_name').eq('company_id', claims['company_id'])\
            .or_(f'name.ilike.{safe},company_name.ilike.{safe}').limit(10).execute().data or []
        w = name.lower()
        return any((r.get('name') or '').strip().lower() == w or (r.get('company_name') or '').strip().lower() == w for r in rows)
    except Exception as e:
        print('customer master check failed (allowing):', e)
        return True


def _fnum(x, default=0.0):
    try: return float(x)
    except Exception: return default


def _create_from_upload(sb, claims, d, src):
    """Proposal from an uploaded BOQ (PDF/Word/Excel). The lines were extracted by
    /extract-boq and reviewed/edited by the user before arriving here."""
    customer = str(d.get('customer_name') or '').strip()[:160]
    if not _customer_in_master(sb, claims, customer):
        return jsonify({'error': 'Select a valid customer from your Customer Master (add the customer first if needed).', 'code': 'CUSTOMER_REQUIRED'}), 422
    use_prices = bool(src.get('use_prices')) and _can_commercial(claims)
    lines = []
    for i, l in enumerate((src.get('lines') or [])[:150], 1):
        if not isinstance(l, dict): continue
        q = _fnum(l.get('quantity'), 1) or 1
        row = {'source_line_id': f'u{i}', 'section': str(l.get('section') or '')[:100], 'kind': 'equipment',
               'manufacturer': str(l.get('manufacturer') or '')[:80], 'model': str(l.get('model') or '')[:120],
               'description': str(l.get('description') or '')[:800], 'quantity': q, 'unit': str(l.get('unit') or 'Pcs')[:20], 'inclusion': 'included'}
        if not (row['manufacturer'] or row['model'] or row['description']): continue
        if use_prices:
            up = _fnum(l.get('unit_price'), 0) or (_fnum(l.get('total'), 0) / q if l.get('total') else 0)
            row['unit_selling_price'] = round(up, 2); row['line_total'] = round(up * q, 2)
        lines.append(row)
    if not lines:
        return jsonify({'error': 'The BOQ has no lines.'}), 422
    title = str(d.get('title') or '').strip()[:200] or None
    snap = {'source_kind': 'bom', 'quote_ref': '', 'option_label': '', 'option_index': 0, 'customer': customer,
            'project': title or '', 'lines': lines, 'visibility': 'commercial' if use_prices else 'technical'}
    if use_prices:
        sub = sum(l.get('line_total', 0) for l in lines)
        vr = _fnum(src.get('vat_rate'), 5)
        snap['commercials'] = {'currency': str(src.get('currency') or 'AED')[:8], 'subtotal': sub, 'vat_rate': vr,
                               'vat': sub * vr / 100, 'total': sub * (1 + vr / 100)}
    ref = _next_reference(sb, claims['company_id'])
    row = sb.table('proposals').insert({
        'company_id': claims['company_id'], 'reference': ref, 'title': title or customer or None, 'customer_name': customer,
        'source_kind': 'bom', 'quote_id': None, 'quote_ref': None,
        'instruction': (str(d.get('instruction') or '').strip()[:2000] or None),
        'owner_id': claims.get('user_id'), 'stage': 'draft',
    }).execute().data[0]
    sb.table('proposal_source_snapshots').insert({
        'company_id': claims['company_id'], 'proposal_id': row['id'], 'safe_source': snap,
        'source_hash': _stable_hash(snap), 'visibility': snap['visibility'], 'created_by': claims.get('user_id'),
    }).execute()
    return jsonify({'proposal': _public(row)}), 201


def _load(sb, claims, pid):
    r = sb.table('proposals').select('*').eq('id', pid).eq('company_id', claims['company_id']).execute().data
    return r[0] if r else None


@app.route('/api/proposals/<pid>', methods=['GET'])
def get_proposal(pid):
    sb, claims = get_sb(), g.claims
    p = _load(sb, claims, pid)
    if not p: return jsonify({'error': 'Not found'}), 404
    snap = sb.table('proposal_source_snapshots').select('id,safe_source,visibility,created_at')\
        .eq('proposal_id', pid).order('created_at', desc=True).limit(1).execute().data
    src = snap[0] if snap else None
    if src and src['visibility'] == 'commercial' and not _can_commercial(claims):
        s = dict(src['safe_source'])  # technical-only reader: strip priced content
        s.pop('commercials', None)
        s['lines'] = [{k: v for k, v in l.items() if k not in ('unit_selling_price', 'line_total')} for l in s.get('lines', [])]
        src = {**src, 'safe_source': s}
    return jsonify({'proposal': _public(p), 'source': src, 'source_changed': _source_changed(sb, claims, p, snap[0] if snap else None)})


def _source_changed(sb, claims, p, snap):
    """True when the live quotation no longer matches the snapshot this proposal was built on."""
    try:
        if not snap or not p.get('quote_id'): return False
        qr = sb.table('quotes').select('*').eq('id', p['quote_id']).eq('company_id', claims['company_id']).execute().data
        if not qr: return False
        ss = snap.get('safe_source') or {}
        # snapshot rows fetched in get_proposal omit the hash; recompute from the stored projection
        cur = _safe_quote_projection(qr[0], int(ss.get('option_index', 0) or 0), ss.get('visibility') == 'commercial')
        a = {k: v for k, v in cur.items() if k != 'option_index' and not (k == 'terms' and 'terms' not in ss)}
        b = {k: v for k, v in ss.items() if k != 'option_index'}
        return _stable_hash(a) != _stable_hash(b)
    except Exception:
        return False


@app.route('/api/proposals/<pid>/refresh-source', methods=['POST'])
def refresh_source(pid):
    """Re-capture the quotation as a new snapshot. Existing document versions are untouched."""
    sb, claims = get_sb(), g.claims
    p = _load(sb, claims, pid)
    if not p: return jsonify({'error': 'Not found'}), 404
    if p['lifecycle'] != 'active': return jsonify({'error': 'Restore this proposal first.'}), 409
    qr = sb.table('quotes').select('*').eq('id', p.get('quote_id') or '').eq('company_id', claims['company_id']).execute().data if p.get('quote_id') else []
    if not qr or not _quote_visible(claims, qr[0]): return jsonify({'error': 'Source quotation not available.'}), 404
    old = sb.table('proposal_source_snapshots').select('safe_source').eq('proposal_id', pid).order('created_at', desc=True).limit(1).execute().data
    oidx = int(((old[0]['safe_source'] if old else {}) or {}).get('option_index', 0) or 0)
    snap = _safe_quote_projection(qr[0], oidx, _can_commercial(claims))
    if not snap['lines']: return jsonify({'error': 'The quotation now has no lines.'}), 422
    sb.table('proposal_source_snapshots').insert({
        'company_id': claims['company_id'], 'proposal_id': pid, 'safe_source': snap,
        'source_hash': _stable_hash(snap), 'visibility': snap['visibility'], 'created_by': claims.get('user_id'),
    }).execute()
    now = datetime.utcnow().isoformat()
    sb.table('proposals').update({'updated_at': now, 'customer_name': snap['customer'] or p.get('customer_name')}).eq('id', pid).execute()
    return jsonify({'ok': True})


@app.route('/api/proposals/<pid>', methods=['PATCH'])
def patch_proposal(pid):
    sb, claims = get_sb(), g.claims
    p = _load(sb, claims, pid)
    if not p: return jsonify({'error': 'Not found'}), 404
    d = request.json or {}
    if int(d.get('expected_lock_version', p['lock_version'])) != p['lock_version']:
        return jsonify({'error': 'Someone else changed this proposal. Reload and try again.', 'code': 'REVISION_CONFLICT'}), 409
    upd = {'lock_version': p['lock_version'] + 1, 'updated_at': datetime.utcnow().isoformat()}
    if 'title' in d: upd['title'] = (str(d['title']).strip()[:200] or None)
    if 'instruction' in d: upd['instruction'] = (str(d['instruction']).strip()[:2000] or None)
    r = sb.table('proposals').update(upd).eq('id', pid).eq('lock_version', p['lock_version']).execute().data
    if not r: return jsonify({'error': 'Revision conflict', 'code': 'REVISION_CONFLICT'}), 409
    return jsonify({'proposal': _public(r[0])})


@app.route('/api/proposals/<pid>/archive', methods=['POST'])
def archive_proposal(pid):
    sb, claims = get_sb(), g.claims
    if not _load(sb, claims, pid): return jsonify({'error': 'Not found'}), 404
    r = sb.table('proposals').update({'lifecycle': 'archived', 'archived_at': datetime.utcnow().isoformat(),
                                      'updated_at': datetime.utcnow().isoformat()}).eq('id', pid).execute().data
    return jsonify({'proposal': _public(r[0])})


@app.route('/api/proposals/<pid>/restore', methods=['POST'])
def restore_proposal(pid):
    sb, claims = get_sb(), g.claims
    if not _load(sb, claims, pid): return jsonify({'error': 'Not found'}), 404
    r = sb.table('proposals').update({'lifecycle': 'active', 'archived_at': None,
                                      'updated_at': datetime.utcnow().isoformat()}).eq('id', pid).execute().data
    return jsonify({'proposal': _public(r[0])})


# ── Settings → Proposal AI (independent from general QTCal AI) ────────────────
# Credentials are server-side environment variables, referenced by NAME only
# (never returned or stored as values). Only an allowlisted set of names is
# accepted so a profile cannot point at unrelated secrets.
PROVIDERS = {
    'openai':    {'label': 'OpenAI',    'cred_default': 'OPENAI_API_KEY'},
    'anthropic': {'label': 'Anthropic', 'cred_default': 'ANTHROPIC_API_KEY'},
    'google':    {'label': 'Google',    'cred_default': 'GEMINI_API_KEY'},
}
_CRED_OK = re.compile(r'^(OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY|PROPOSAL_AI_[A-Z0-9_]{1,40})$')
DEFAULT_IMAGE_PROFILE = {'provider': 'openai', 'model': 'gpt-image-2', 'credential_ref': 'OPENAI_API_KEY', 'quality': 'high'}
DEFAULT_IMAGE_FALLBACK = {'provider': 'google', 'model': '', 'credential_ref': 'GEMINI_API_KEY', 'quality': 'high'}
DEFAULT_TEXT_PROFILE = {'provider': 'anthropic', 'model': '', 'credential_ref': 'ANTHROPIC_API_KEY'}
DEFAULT_LIMITS = {'monthly_budget_usd': None, 'max_concurrent_jobs': 2, 'max_images_per_package': 4,
                  'max_schema_repairs': 2, 'max_layout_repairs': 2}


def _require_settings_admin():
    if not is_admin(g.claims) or not has_page_access(g.claims, 'proposalAi'):
        return jsonify({'error': 'Only company administrators can manage Proposal AI settings.'}), 403
    return None


def _clean_profile(p, kind, required):
    if not p:
        if required: return None, f'{kind} profile is required.'
        return None, None
    prov = (p.get('provider') or '').lower()
    if prov not in PROVIDERS: return None, f'{kind}: unknown provider.'
    model = (p.get('model') or '').strip()[:100]
    if not model: return None, f'{kind}: choose a model.'
    cred = (p.get('credential_ref') or PROVIDERS[prov]['cred_default']).strip()
    if not _CRED_OK.match(cred): return None, f'{kind}: credential reference "{cred}" is not allowed.'
    out = {'provider': prov, 'model': model, 'credential_ref': cred}
    if kind.startswith('image'):
        out['quality'] = p.get('quality') if p.get('quality') in ('standard', 'high') else 'high'
    return out, None


def _active_settings(sb, company_id):
    r = sb.table('company_proposal_ai_settings_versions').select('*').eq('company_id', company_id).eq('is_active', True).execute().data
    return r[0] if r else None


def _status(s):
    def ok(p): return bool(p and p.get('model') and os.environ.get(p.get('credential_ref') or '', ''))
    if not s: return {'text': 'not_configured', 'image': 'not_configured'}
    return {'text': 'ready' if ok(s.get('text_profile')) else 'credential_missing',
            'image': 'ready' if ok(s.get('image_profile')) else 'credential_missing'}


@app.route('/api/proposals/ai-settings', methods=['GET'])
def get_ai_settings():
    err = _require_settings_admin()
    if err: return err
    s = _active_settings(get_sb(), g.claims['company_id'])
    return jsonify({'settings': s, 'status': _status(s), 'providers': {k: v['label'] for k, v in PROVIDERS.items()},
                    'defaults': {'text_profile': DEFAULT_TEXT_PROFILE, 'image_profile': DEFAULT_IMAGE_PROFILE,
                                 'image_fallback': DEFAULT_IMAGE_FALLBACK, 'limits': DEFAULT_LIMITS},
                    'credentials_present': {n: bool(os.environ.get(n)) for n in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GEMINI_API_KEY')}})


@app.route('/api/proposals/ai-settings', methods=['PATCH'])
def save_ai_settings():
    err = _require_settings_admin()
    if err: return err
    sb, claims = get_sb(), g.claims
    d = request.json or {}
    cur = _active_settings(sb, claims['company_id'])
    if cur and int(d.get('expected_version', cur['version'])) != cur['version']:
        return jsonify({'error': 'Settings changed since you opened them. Reload and try again.', 'code': 'REVISION_CONFLICT'}), 409
    text, e1 = _clean_profile(d.get('text_profile'), 'text', True)
    img, e2 = _clean_profile(d.get('image_profile'), 'image', True)
    tfb, e3 = _clean_profile(d.get('text_fallback'), 'text fallback', False)
    ifb, e4 = _clean_profile(d.get('image_fallback'), 'image fallback', False)
    for e in (e1, e2, e3, e4):
        if e: return jsonify({'error': e, 'code': 'PROPOSAL_AI_CONFIG_INVALID'}), 422
    lim = dict(DEFAULT_LIMITS)
    for k, v in (d.get('limits') or {}).items():
        if k in lim and (v is None or isinstance(v, (int, float))): lim[k] = v
    ver = (cur['version'] if cur else 0) + 1
    if cur:
        sb.table('company_proposal_ai_settings_versions').update({'is_active': False}).eq('id', cur['id']).execute()
    row = sb.table('company_proposal_ai_settings_versions').insert({
        'company_id': claims['company_id'], 'version': ver, 'text_profile': text, 'image_profile': img,
        'text_fallback': tfb, 'image_fallback': ifb, 'limits': lim, 'is_active': True, 'created_by': claims.get('user_id'),
    }).execute().data[0]
    return jsonify({'settings': row, 'status': _status(row)})


def _http_ok(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


# ── M3: preview packages (write-up + image) ───────────────────────────────────
# One proposal_previews row is both the package and its generation job.
# Flow (works on serverless): client POSTs /previews (row = queued, returns at
# once) -> client POSTs /previews/<id>/run (does the work, up to maxDuration)
# -> client polls GET /previews. A run that dies leaves the row 'running';
# the list endpoint marks it failed after STALE_MINUTES so it never hangs.
# Only the customer-safe snapshot ever reaches a model: no cost/margin/supplier
# discount, and prices are deliberately NOT sent (not needed for a visual).
STALE_MINUTES = 8
MAX_PREVIEWS_PER_PROPOSAL = 25
PREVIEW_BUCKET = 'company-assets'


class GenError(Exception):
    pass


def _post_json(url, headers, body, timeout):
    data = json.dumps(body).encode()
    h = {'Content-Type': 'application/json', **headers}
    req = urllib.request.Request(url, data=data, headers=h, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode())
            msg = (msg.get('error') or {}).get('message') if isinstance(msg.get('error'), dict) else (msg.get('error') or msg)
        except Exception:
            msg = ''
        raise GenError(f'provider returned {e.code}: {str(msg)[:200]}')
    except Exception as e:
        raise GenError(f'could not reach provider: {str(e)[:150]}')


def _brief_from_snapshot(safe_source, customer, project, instruction):
    lines = []
    for l in (safe_source.get('lines') or []):
        lines.append({'section': l.get('section'), 'item': (str(l.get('manufacturer') or '') + ' ' + str(l.get('model') or '')).strip(),
                      'description': (l.get('description') or '')[:240], 'qty': l.get('quantity')})
    return {'customer': customer, 'project': project, 'equipment': lines[:60], 'extra_guidance': instruction or ''}


TEXT_SYSTEM = (
    "You are a pre-sales solutions writer for an audio-visual systems integrator. From the equipment list you are given, "
    "write a short customer-facing proposal narrative and a prompt for an illustrative room visual.\n"
    "Rules: use ONLY the equipment provided; never invent products, quantities, specifications, performance figures, "
    "certifications or prices; do not mention cost, margin or discounts; plain professional English; no marketing hype.\n"
    "For the image prompt: describe ONE photorealistic, well-lit architectural visualisation of the space showing the listed "
    "equipment types in plausible positions (displays, cameras, microphones, speakers, control surfaces, etc.). If the list covers "
    "several room types, depict the most representative room (the package that repeats most), and state exact per-room counts of each "
    "device type (e.g. 'one video bar below the display, one touch console on the table, one wireless dongle'); do not add any "
    "device that is not in the list. Screens must show abstract or blank content - no readable text, no logos, no brand names, "
    "no people's faces. Keep it under 150 words.\n"
    "Return ONLY a JSON object: {\"headline\": str, \"summary\": str (2-3 sentences), "
    "\"sections\": [{\"title\": str, \"body\": str}] (2-4 sections, e.g. Understanding, Proposed solution, How it works), "
    "\"highlights\": [str] (3-5 short bullets drawn from the equipment list), \"image_prompt\": str}."
)


def _extract_json(text):
    text = (text or '').strip()
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        raise GenError('the writing model did not return JSON')
    return json.loads(m.group(0))


def _call_text(profile, brief):
    secret = os.environ.get(profile['credential_ref'], '')
    if not secret:
        raise GenError(f"server variable {profile['credential_ref']} is not set")
    user = 'Project brief (JSON):\n' + json.dumps(brief, ensure_ascii=False)
    if profile['provider'] == 'anthropic':
        r = _post_json('https://api.anthropic.com/v1/messages',
                       {'x-api-key': secret, 'anthropic-version': '2023-06-01'},
                       {'model': profile['model'], 'max_tokens': 2200, 'system': TEXT_SYSTEM,
                        'messages': [{'role': 'user', 'content': user}]}, 90)
        txt = ''.join(b.get('text', '') for b in (r.get('content') or []) if b.get('type') == 'text')
    elif profile['provider'] == 'openai':
        r = _post_json('https://api.openai.com/v1/chat/completions', {'Authorization': 'Bearer ' + secret},
                       {'model': profile['model'], 'messages': [{'role': 'system', 'content': TEXT_SYSTEM},
                                                                {'role': 'user', 'content': user}]}, 90)
        txt = (((r.get('choices') or [{}])[0]).get('message') or {}).get('content') or ''
    else:
        raise GenError('text generation supports Anthropic or OpenAI profiles')
    out = _extract_json(txt)
    if not out.get('image_prompt') or not out.get('summary'):
        raise GenError('the writing model returned an incomplete result')
    out['sections'] = [{'title': str(s.get('title', ''))[:120], 'body': str(s.get('body', ''))[:1500]}
                       for s in (out.get('sections') or [])[:5] if isinstance(s, dict)]
    out['highlights'] = [str(h)[:200] for h in (out.get('highlights') or [])[:6]]
    out['headline'] = str(out.get('headline', ''))[:200]
    out['summary'] = str(out['summary'])[:1200]
    out['image_prompt'] = str(out['image_prompt'])[:1500]
    return out


def _call_image(profile, prompt, timeout=230):
    import base64
    secret = os.environ.get(profile['credential_ref'], '')
    if not secret:
        raise GenError(f"server variable {profile['credential_ref']} is not set")
    if profile['provider'] == 'openai':
        q = 'high' if profile.get('quality', 'high') == 'high' else 'medium'
        r = _post_json('https://api.openai.com/v1/images/generations', {'Authorization': 'Bearer ' + secret},
                       {'model': profile['model'], 'prompt': prompt, 'size': '1536x1024', 'quality': q, 'n': 1}, timeout)
        b64 = (((r.get('data') or [{}])[0]).get('b64_json'))
        if not b64:
            raise GenError('the image model returned no image')
        return base64.b64decode(b64)
    if profile['provider'] == 'google':
        r = _post_json('https://generativelanguage.googleapis.com/v1beta/models/' + urllib.parse.quote(profile['model']) + ':generateContent',
                       {'x-goog-api-key': secret},
                       {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'responseModalities': ['IMAGE']}}, timeout)
        for c in (r.get('candidates') or []):
            for part in ((c.get('content') or {}).get('parts') or []):
                inl = part.get('inlineData') or part.get('inline_data')
                if inl and inl.get('data'):
                    return base64.b64decode(inl['data'])
        raise GenError('the image model returned no image')
    raise GenError('image generation supports OpenAI or Google profiles')


def _norm_key(s):
    return re.sub(r'[\s\-_]+', '', (s or '').strip().lower())


def _reference_images(sb, company_id, safe_source, limit=4):
    """Catalogue product photos for the quote's models, used as visual references.
    Best-effort: any failure just means no references."""
    out = []
    try:
        keys = []
        for l in (safe_source.get('lines') or []):
            k = _norm_key(l.get('model'))
            if k and k not in keys: keys.append(k)
        if not keys: return out
        rows = sb.table('products').select('model_key,image_url').eq('company_id', company_id)\
            .in_('model_key', keys[:40]).not_.is_('image_url', 'null').execute().data or []
        by = {r['model_key']: r['image_url'] for r in rows if r.get('image_url')}
        for k in keys:
            u = by.get(k)
            if not u: continue
            try:
                with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'qtcal'}), timeout=15) as r:
                    b = r.read(4_000_001)
                    ct = (r.headers.get('Content-Type') or '').split(';')[0].strip()
                if len(b) <= 4_000_000 and ct in ('image/png', 'image/jpeg', 'image/webp'):
                    out.append((ct, b))
            except Exception:
                continue
            if len(out) >= limit: break
    except Exception:
        return []
    return out


def _refs_for_models(sb, company_id, models, limit=4):
    """Like _reference_images but for an explicit list of model names (one image slot)."""
    return _reference_images(sb, company_id, {'lines': [{'model': m} for m in (models or [])]}, limit)


def _call_image_edit_openai(profile, prompt, refs, timeout=110):
    """gpt-image edits endpoint with reference photos (multipart)."""
    import base64, uuid
    secret = os.environ.get(profile['credential_ref'], '')
    if not secret: raise GenError(f"server variable {profile['credential_ref']} is not set")
    boundary = uuid.uuid4().hex
    parts = []
    def field(n, v):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode())
    field('model', profile['model']); field('prompt', prompt); field('size', '1536x1024')
    field('quality', 'high' if profile.get('quality', 'high') == 'high' else 'medium'); field('n', '1')
    for i, (ct, b) in enumerate(refs):
        ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}[ct]
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="ref{i}.{ext}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + b + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    req = urllib.request.Request('https://api.openai.com/v1/images/edits', data=b''.join(parts), method='POST',
                                 headers={'Authorization': 'Bearer ' + secret, 'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise GenError(f'provider returned {e.code} on reference edit')
    except Exception as e:
        raise GenError(f'could not reach provider: {str(e)[:120]}')
    b64 = (((j.get('data') or [{}])[0]).get('b64_json'))
    if not b64: raise GenError('the image model returned no image')
    return base64.b64decode(b64)


def _preview_public(sb, v):
    out = {k: v.get(k) for k in ('id', 'proposal_id', 'seq', 'status', 'instruction', 'writeup', 'image_provider', 'image_model',
                                  'text_model', 'error', 'stage_detail', 'created_at', 'started_at', 'finished_at')}
    out['image_url'] = None
    if v.get('image_path'):
        try:
            from api.auth import _signed_url
            out['image_url'] = _signed_url(PREVIEW_BUCKET, v['image_path'], 3600)
        except Exception:
            out['image_url'] = None
    return out


def _sweep_stale(sb, pid):
    cutoff = (datetime.utcnow() - timedelta(minutes=STALE_MINUTES)).isoformat()
    sb.table('proposal_previews').update({'status': 'failed', 'error': 'Generation timed out. Please try again.',
                                          'finished_at': datetime.utcnow().isoformat()})\
        .eq('proposal_id', pid).in_('status', ['queued', 'running']).lt('created_at', cutoff).execute()


@app.route('/api/proposals/<pid>/previews', methods=['GET'])
def list_previews(pid):
    sb, claims = get_sb(), g.claims
    p = _load(sb, claims, pid)
    if not p: return jsonify({'error': 'Not found'}), 404
    _sweep_stale(sb, pid)
    rows = sb.table('proposal_previews').select('*').eq('proposal_id', pid).order('seq', desc=True).limit(MAX_PREVIEWS_PER_PROPOSAL).execute().data or []
    return jsonify({'previews': [_preview_public(sb, r) for r in rows], 'selected_preview_id': p.get('selected_preview_id')})


@app.route('/api/proposals/<pid>/previews', methods=['POST'])
def create_preview(pid):
    sb, claims = get_sb(), g.claims
    p = _load(sb, claims, pid)
    if not p: return jsonify({'error': 'Not found'}), 404
    if p['lifecycle'] != 'active':
        return jsonify({'error': 'Restore this proposal before generating previews.'}), 409
    s = _active_settings(sb, claims['company_id'])
    st = _status(s)
    if st['text'] != 'ready' or st['image'] != 'ready':
        return jsonify({'error': 'Proposal AI is not ready. An administrator must finish Settings → Proposal AI '
                                 f"(text: {st['text'].replace('_', ' ')}, image: {st['image'].replace('_', ' ')}).",
                        'code': 'PROPOSAL_AI_NOT_CONFIGURED'}), 503
    _sweep_stale(sb, pid)
    lim = (s.get('limits') or {})
    maxc = int(lim.get('max_concurrent_jobs') or 2)
    busy = sb.table('proposal_previews').select('id', count='exact').eq('company_id', claims['company_id'])\
        .in_('status', ['queued', 'running']).execute().count or 0
    if busy >= maxc:
        return jsonify({'error': 'Other previews are still generating. Try again in a minute.', 'code': 'TOO_MANY_JOBS'}), 429
    last = sb.table('proposal_previews').select('seq').eq('proposal_id', pid).order('seq', desc=True).limit(1).execute().data
    seq = (last[0]['seq'] if last else 0) + 1
    if seq > MAX_PREVIEWS_PER_PROPOSAL:
        return jsonify({'error': f'This proposal already has {MAX_PREVIEWS_PER_PROPOSAL} previews.'}), 422
    snap = sb.table('proposal_source_snapshots').select('id').eq('proposal_id', pid).order('created_at', desc=True).limit(1).execute().data
    d = request.json or {}
    row = sb.table('proposal_previews').insert({
        'company_id': claims['company_id'], 'proposal_id': pid, 'seq': seq, 'status': 'queued',
        'instruction': (str(d.get('instruction') or '').strip()[:1500] or None),
        'snapshot_id': snap[0]['id'] if snap else None, 'created_by': claims.get('user_id'),
        'settings_version': s['version'],
    }).execute().data[0]
    return jsonify({'preview': _preview_public(sb, row)}), 201


@app.route('/api/proposals/<pid>/previews/<vid>/run', methods=['POST'])
def run_preview(pid, vid):
    sb, claims = get_sb(), g.claims
    p = _load(sb, claims, pid)
    if not p: return jsonify({'error': 'Not found'}), 404
    now = datetime.utcnow().isoformat()
    # Atomic claim: only a queued row can start, so double-clicks/retries never double-bill.
    claimed = sb.table('proposal_previews').update({'status': 'running', 'started_at': now, 'stage_detail': 'writing'})\
        .eq('id', vid).eq('proposal_id', pid).eq('company_id', claims['company_id']).eq('status', 'queued').execute().data
    if not claimed:
        cur = sb.table('proposal_previews').select('*').eq('id', vid).eq('proposal_id', pid).eq('company_id', claims['company_id']).execute().data
        if not cur: return jsonify({'error': 'Not found'}), 404
        return jsonify({'preview': _preview_public(sb, cur[0])}), 202
    v = claimed[0]

    def fail(msg):
        r = sb.table('proposal_previews').update({'status': 'failed', 'error': msg[:400], 'stage_detail': None,
                                                  'finished_at': datetime.utcnow().isoformat()}).eq('id', vid).execute().data
        return jsonify({'preview': _preview_public(sb, r[0])}), 200

    try:
        s = _active_settings(sb, claims['company_id'])
        if not s: return fail('Proposal AI is not configured.')
        sn = sb.table('proposal_source_snapshots').select('safe_source').eq('id', v['snapshot_id']).execute().data if v.get('snapshot_id') else []
        if not sn: return fail('The source snapshot is missing.')
        brief = _brief_from_snapshot(sn[0]['safe_source'], p.get('customer_name'), p.get('title'),
                                     ' '.join(x for x in (p.get('instruction'), v.get('instruction')) if x))
        # 1) write-up (primary, then fallback)
        text_used, writeup, terr = None, None, None
        for prof in (s.get('text_profile'), s.get('text_fallback')):
            if not prof: continue
            try:
                writeup = _call_text(prof, brief); text_used = prof; break
            except (GenError, ValueError) as e:
                terr = str(e)
        if not writeup: return fail(f'Write-up failed: {terr}')
        sb.table('proposal_previews').update({'stage_detail': 'rendering', 'writeup': {k: writeup[k] for k in ('headline', 'summary', 'sections', 'highlights')},
                                              'image_prompt': writeup['image_prompt'], 'text_provider': text_used['provider'],
                                              'text_model': text_used['model']}).eq('id', vid).execute()
        # 2) image (primary, then fallback)
        img, img_used, ierr = None, None, None
        refs = _reference_images(sb, claims['company_id'], sn[0]['safe_source'])
        for prof in (s.get('image_profile'), s.get('image_fallback')):
            if not prof or not prof.get('model'): continue
            try:
                if refs and prof['provider'] == 'openai':
                    try:
                        img = _call_image_edit_openai(prof, 'Use the attached product photos as the exact visual reference for the matching '
                                                      'equipment (shape, colour, proportions). ' + writeup['image_prompt'], refs)
                    except GenError:
                        img = None  # reference path failed -> plain generation below
                if img is None:
                    img = _call_image(prof, writeup['image_prompt'], 110 if refs else 230)
                img_used = prof; break
            except GenError as e:
                ierr = str(e)
        if not img: return fail(f'Image failed: {ierr}')
        path = f"{claims['company_id']}/proposals/{pid}/{vid}.png"
        try:
            sb.storage.from_(PREVIEW_BUCKET).upload(path, img, {'content-type': 'image/png'})
        except Exception:
            return fail('The image was generated but could not be saved to storage.')
        r = sb.table('proposal_previews').update({'status': 'ready', 'image_path': path, 'image_provider': img_used['provider'],
                                                  'image_model': img_used['model'], 'stage_detail': None, 'error': None,
                                                  'finished_at': datetime.utcnow().isoformat()}).eq('id', vid).execute().data
        return jsonify({'preview': _preview_public(sb, r[0])})
    except Exception as e:
        return fail(f'Unexpected error: {str(e)[:200]}')


@app.route('/api/proposals/<pid>/previews/<vid>/select', methods=['POST'])
def select_preview(pid, vid):
    """'Use this preview': records the exact package the user chose."""
    sb, claims = get_sb(), g.claims
    p = _load(sb, claims, pid)
    if not p: return jsonify({'error': 'Not found'}), 404
    if p['lifecycle'] != 'active': return jsonify({'error': 'Restore this proposal first.'}), 409
    v = sb.table('proposal_previews').select('id,status').eq('id', vid).eq('proposal_id', pid).eq('company_id', claims['company_id']).execute().data
    if not v: return jsonify({'error': 'Not found'}), 404
    if v[0]['status'] != 'ready': return jsonify({'error': 'Only a finished preview can be selected.'}), 422
    now = datetime.utcnow().isoformat()
    r = sb.table('proposals').update({'selected_preview_id': vid, 'selected_at': now, 'stage': 'preview_selected',
                                      'updated_at': now, 'lock_version': p['lock_version'] + 1})\
        .eq('id', pid).eq('lock_version', p['lock_version']).execute().data
    if not r: return jsonify({'error': 'Someone else changed this proposal. Reload and try again.', 'code': 'REVISION_CONFLICT'}), 409
    return jsonify({'proposal': _public(r[0]), 'selected_preview_id': vid})


# ── M4/M6: immutable document versions + approval ─────────────────────────────
# A version freezes the selected preview (write-up + image) and the source lines
# it was built on. Later quotation edits, regenerated previews or setting changes
# never alter an existing version -- a new version is created instead.
DOC_PUBLIC = ('id', 'proposal_id', 'version', 'preview_id', 'snapshot_id', 'options', 'status', 'submitted_at',
              'decided_at', 'decision_note', 'created_at')


def _doc_public(d):
    return {k: d.get(k) for k in DOC_PUBLIC}


def _doc_full(sb, claims, d):
    out = _doc_public(d)
    c = dict(d.get('content') or {})
    img = c.get('image_path')
    c['image_url'] = None
    if img:
        try:
            from api.auth import _signed_url
            c['image_url'] = _signed_url(PREVIEW_BUCKET, img, 3600)
        except Exception:
            pass
    if c.get('v') in (2, 3):
        for k, im in (c.get('images') or {}).items():
            try:
                from api.auth import _signed_url
                im['url'] = _signed_url(PREVIEW_BUCKET, im['path'], 3600) if im.get('path') else None
            except Exception:
                im['url'] = None
            try:
                from api.auth import _signed_url
                im['source_url'] = _signed_url(PREVIEW_BUCKET, im['source_path'], 3600) if im.get('source_path') else None
            except Exception:
                im['source_url'] = None
    if not _can_commercial(claims):  # technical-only reader never gets prices
        c.pop('commercials', None)
        c['lines'] = [{k: v for k, v in l.items() if k not in ('unit_selling_price', 'line_total')} for l in c.get('lines', [])]
    out['content'] = c
    return out


@app.route('/api/proposals/<pid>/documents', methods=['GET'])
def list_documents(pid):
    sb, claims = get_sb(), g.claims
    if not _load(sb, claims, pid): return jsonify({'error': 'Not found'}), 404
    rows = sb.table('proposal_documents').select('id,proposal_id,version,preview_id,snapshot_id,options,status,submitted_at,decided_at,decision_note,created_at')\
        .eq('proposal_id', pid).order('version', desc=True).limit(50).execute().data or []
    return jsonify({'documents': [_doc_public(r) for r in rows]})


@app.route('/api/proposals/<pid>/documents/<did>', methods=['GET'])
def get_document(pid, did):
    sb, claims = get_sb(), g.claims
    if not _load(sb, claims, pid): return jsonify({'error': 'Not found'}), 404
    r = sb.table('proposal_documents').select('*').eq('id', did).eq('proposal_id', pid).eq('company_id', claims['company_id']).execute().data
    if not r: return jsonify({'error': 'Not found'}), 404
    return jsonify({'document': _doc_full(sb, claims, r[0])})


@app.route('/api/proposals/<pid>/documents', methods=['POST'])
def create_document(pid):
    sb, claims = get_sb(), g.claims
    p = _load(sb, claims, pid)
    if not p: return jsonify({'error': 'Not found'}), 404
    if p['lifecycle'] != 'active': return jsonify({'error': 'Restore this proposal first.'}), 409
    d = request.json or {}
    vid = d.get('preview_id') or p.get('selected_preview_id')
    if not vid: return jsonify({'error': 'Choose a preview with "Use this preview" first.'}), 422
    pv = sb.table('proposal_previews').select('*').eq('id', vid).eq('proposal_id', pid).eq('company_id', claims['company_id']).execute().data
    if not pv or pv[0]['status'] != 'ready': return jsonify({'error': 'That preview is not available.'}), 422
    pv = pv[0]
    snap = sb.table('proposal_source_snapshots').select('*').eq('proposal_id', pid).order('created_at', desc=True).limit(1).execute().data
    if not snap: return jsonify({'error': 'Source snapshot missing.'}), 422
    snap = snap[0]
    opts_in = d.get('options') or {}
    show_prices = bool(opts_in.get('show_prices')) and _can_commercial(claims) and snap['visibility'] == 'commercial'
    options = {'show_prices': show_prices, 'include_terms': bool(opts_in.get('include_terms', True)),
               'cover_note': str(opts_in.get('cover_note') or '')[:1500]}
    ss = snap['safe_source']
    lines = ss.get('lines') or []
    if not show_prices:
        lines = [{k: v for k, v in l.items() if k not in ('unit_selling_price', 'line_total')} for l in lines]
    content = {
        'reference': p['reference'], 'title': p.get('title') or '', 'customer': ss.get('customer') or p.get('customer_name') or '',
        'quote_ref': p.get('quote_ref') or '', 'project': ss.get('project') or '', 'date': datetime.utcnow().strftime('%Y-%m-%d'),
        'writeup': pv.get('writeup') or {}, 'image_path': pv.get('image_path'), 'lines': lines,
    }
    if show_prices and ss.get('commercials'): content['commercials'] = ss['commercials']
    last = sb.table('proposal_documents').select('version').eq('proposal_id', pid).order('version', desc=True).limit(1).execute().data
    ver = (last[0]['version'] if last else 0) + 1
    row = sb.table('proposal_documents').insert({
        'company_id': claims['company_id'], 'proposal_id': pid, 'version': ver, 'preview_id': vid, 'snapshot_id': snap['id'],
        'source_hash': snap.get('source_hash'), 'options': options, 'content': content, 'status': 'draft', 'created_by': claims.get('user_id'),
    }).execute().data[0]
    now = datetime.utcnow().isoformat()
    sb.table('proposals').update({'current_document_id': row['id'], 'stage': 'document_draft', 'updated_at': now}).eq('id', pid).execute()
    return jsonify({'document': _doc_public(row)}), 201


def _load_doc(sb, claims, pid, did):
    r = sb.table('proposal_documents').select('*').eq('id', did).eq('proposal_id', pid).eq('company_id', claims['company_id']).execute().data
    return r[0] if r else None


@app.route('/api/proposals/<pid>/documents/<did>/submit', methods=['POST'])
def submit_document(pid, did):
    sb, claims = get_sb(), g.claims
    p = _load(sb, claims, pid); doc = _load_doc(sb, claims, pid, did) if p else None
    if not doc: return jsonify({'error': 'Not found'}), 404
    if doc['status'] not in ('draft', 'rejected'): return jsonify({'error': 'This version is already submitted or approved.'}), 409
    now = datetime.utcnow().isoformat()
    r = sb.table('proposal_documents').update({'status': 'pending_approval', 'submitted_by': claims.get('user_id'), 'submitted_at': now,
                                               'decided_by': None, 'decided_at': None, 'decision_note': None}).eq('id', did).execute().data
    sb.table('proposals').update({'stage': 'pending_approval', 'updated_at': now}).eq('id', pid).execute()
    return jsonify({'document': _doc_public(r[0])})


@app.route('/api/proposals/<pid>/documents/<did>/decide', methods=['POST'])
def decide_document(pid, did):
    sb, claims = get_sb(), g.claims
    if not is_admin(claims): return jsonify({'error': 'Only an administrator can approve or reject proposals.'}), 403
    p = _load(sb, claims, pid); doc = _load_doc(sb, claims, pid, did) if p else None
    if not doc: return jsonify({'error': 'Not found'}), 404
    if doc['status'] != 'pending_approval': return jsonify({'error': 'This version is not awaiting approval.'}), 409
    d = request.json or {}
    approve = d.get('decision') == 'approve'
    now = datetime.utcnow().isoformat()
    r = sb.table('proposal_documents').update({'status': 'approved' if approve else 'rejected', 'decided_by': claims.get('user_id'),
                                               'decided_at': now, 'decision_note': str(d.get('note') or '')[:500] or None}).eq('id', did).execute().data
    sb.table('proposals').update({'stage': 'approved' if approve else 'changes_requested', 'updated_at': now}).eq('id', pid).execute()
    return jsonify({'document': _doc_public(r[0])})


@app.route('/api/proposals/ai-settings/verify', methods=['POST'])
def verify_ai_settings():
    """Checks credential presence and that each configured model is visible to
    that credential. Performs NO generation."""
    err = _require_settings_admin()
    if err: return err
    s = _active_settings(get_sb(), g.claims['company_id'])
    if not s: return jsonify({'error': 'Save a configuration first.', 'code': 'PROPOSAL_AI_NOT_CONFIGURED'}), 503
    results = {}
    for key in ('text_profile', 'image_profile', 'text_fallback', 'image_fallback'):
        p = s.get(key)
        if not p: continue
        secret = os.environ.get(p['credential_ref'], '')
        if not secret:
            results[key] = {'ok': False, 'message': f"Server environment variable {p['credential_ref']} is not set."}
            continue
        try:
            m = urllib.parse.quote(p['model'])
            if p['provider'] == 'openai':
                _http_ok('https://api.openai.com/v1/models/' + m, {'Authorization': 'Bearer ' + secret})
            elif p['provider'] == 'anthropic':
                _http_ok('https://api.anthropic.com/v1/models/' + m, {'x-api-key': secret, 'anthropic-version': '2023-06-01'})
            elif p['provider'] == 'google':
                _http_ok('https://generativelanguage.googleapis.com/v1beta/models/' + m, {'x-goog-api-key': secret})
            results[key] = {'ok': True, 'message': 'Model is available to this credential.'}
        except urllib.error.HTTPError as e:
            results[key] = {'ok': False, 'message': f'{PROVIDERS[p["provider"]]["label"]} returned {e.code} for model "{p["model"]}" - check the model name and account access.'}
        except Exception as e:
            results[key] = {'ok': False, 'message': f'Could not reach the provider: {str(e)[:120]}'}
    return jsonify({'results': results, 'version': s['version']})


# ── 4-step flow (content -> images -> review -> final) lives in its own module ──
import api.proposals_draft as _proposals_draft
_proposals_draft.register(app, globals())
