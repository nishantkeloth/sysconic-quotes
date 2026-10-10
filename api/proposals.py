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
from datetime import datetime

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
        'customer': quote.get('customer') or opt.get('customer') or '',
        'project': quote.get('title') or '',
        'lines': lines,
        'visibility': 'commercial' if can_commercial else 'technical',
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
                                  'created_at', 'updated_at')}


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
    return jsonify({'proposal': _public(p), 'source': src})


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
