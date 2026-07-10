from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt
from supabase import create_client

import _zoho_adapter

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Adapter registry ────────────────────────────────────────────────────────────
# Each entry provides fetch_customers(creds), fetch_vendors(creds), and
# is_configured(creds)/test_connection(creds). Adding a new provider (e.g.
# QuickBooks, HubSpot) means writing one more _<provider>_adapter.py module
# with the same shape and registering it here — nothing else in this file
# needs to change.
ADAPTERS = {
    'zoho': _zoho_adapter,
}

PROVIDER_LABELS = {
    'zoho': 'Zoho Books',
}

def verify_token(req):
    auth = req.headers.get('Authorization','')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

def mask(value):
    value = str(value or '')
    if len(value) <= 4: return '••••'
    return '•' * (len(value) - 4) + value[-4:]

def is_admin(claims):
    return (claims or {}).get('role') == 'admin'

ADMIN_ONLY = {'error': 'Admin only — ask a company admin to manage integrations.'}

# ── List this company's configured integrations ────────────────────────────────
# Admin-only: connecting a system, choosing what feeds Customer/Vendor sync, and
# triggering sync are company configuration, not per-user actions — matches how
# Salesforce/HubSpot/Zendesk scope "Settings → Integrations" to admins/owners.
@app.route('/api/integrations', methods=['GET'])
def list_integrations():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    rows = sb.table('company_integrations').select('*').eq('company_id', claims['company_id']).execute()
    by_provider = {r['provider']: r for r in (rows.data or [])}

    out = []
    for provider, adapter in ADAPTERS.items():
        row = by_provider.get(provider)
        creds = (row or {}).get('credentials') or {}
        out.append({
            'provider': provider,
            'label': PROVIDER_LABELS.get(provider, provider.title()),
            'status': (row or {}).get('status', 'disconnected'),
            'last_synced_at': (row or {}).get('last_synced_at'),
            'fields': {k: (mask(v) if v else '') for k, v in creds.items()},
            'required_fields': getattr(adapter, 'REQUIRED_FIELDS', []),
        })
    return jsonify({'integrations': out})

# ── Connect (save + verify credentials) ─────────────────────────────────────────
@app.route('/api/integrations/connect', methods=['POST'])
def connect_integration():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    d = request.json or {}
    provider = (d.get('provider') or '').strip().lower()
    new_creds = d.get('credentials') or {}
    adapter = ADAPTERS.get(provider)
    if not adapter:
        return jsonify({'error': f'Unknown provider "{provider}"'}), 400

    # Merge with whatever's already saved so the UI can support "leave blank to
    # keep the existing value" when editing credentials.
    existing = sb.table('company_integrations').select('credentials').eq('company_id', claims['company_id']).eq('provider', provider).execute()
    creds = dict((existing.data[0].get('credentials') or {}) if existing.data else {})
    creds.update({k: v for k, v in new_creds.items() if v})

    if not adapter.is_configured(creds):
        return jsonify({'error': f'Missing required fields: {", ".join(adapter.REQUIRED_FIELDS)}'}), 400

    try:
        adapter.test_connection(creds)
    except Exception as e:
        # Save as 'error' status so the attempt is visible, but don't block retry
        sb.table('company_integrations').upsert({
            'company_id': claims['company_id'], 'provider': provider,
            'credentials': creds, 'status': 'error',
        }, on_conflict='company_id,provider').execute()
        return jsonify({'error': f'Could not connect to {PROVIDER_LABELS.get(provider, provider)}: {str(e)[:250]}'}), 502

    sb.table('company_integrations').upsert({
        'company_id': claims['company_id'], 'provider': provider,
        'credentials': creds, 'status': 'connected',
        'created_by': claims['user_id'],
    }, on_conflict='company_id,provider').execute()
    return jsonify({'ok': True})

# ── Disconnect ───────────────────────────────────────────────────────────────────
@app.route('/api/integrations/<provider>', methods=['DELETE'])
def disconnect_integration(provider):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403
    sb.table('company_integrations').delete().eq('company_id', claims['company_id']).eq('provider', provider).execute()
    return jsonify({'ok': True})

def _get_creds(claims, provider):
    row = sb.table('company_integrations').select('credentials,status').eq('company_id', claims['company_id']).eq('provider', provider).execute()
    if not row.data:
        return None
    return row.data[0].get('credentials') or {}

def _mark_synced(claims, provider):
    sb.table('company_integrations').update({'last_synced_at': 'now()'}).eq('company_id', claims['company_id']).eq('provider', provider).execute()

def _connected_providers(claims):
    """Providers this company has actually connected (status='connected')."""
    rows = sb.table('company_integrations').select('provider,status').eq('company_id', claims['company_id']).eq('status', 'connected').execute()
    return [r['provider'] for r in (rows.data or []) if r['provider'] in ADAPTERS]

def _company_provider(claims, kind):
    """kind is 'customer_sync_provider' or 'vendor_sync_provider' — the admin's
    explicit choice of which connected integration feeds that data type."""
    row = sb.table('companies').select(kind).eq('id', claims['company_id']).execute()
    if not row.data:
        return None
    return (row.data[0].get(kind) or '').strip().lower() or None

# ── Sync configuration: which connected provider feeds each data type ───────────
# This is the explicit admin choice (a dropdown in Team & Settings), not an
# auto-detected "whichever is connected" guess — set once here, then the Sync
# buttons on Customers/Vendors just use it.
@app.route('/api/integrations/sync-config', methods=['GET'])
def get_sync_config():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    connected = _connected_providers(claims)
    row = sb.table('companies').select('customer_sync_provider,vendor_sync_provider').eq('id', claims['company_id']).execute()
    cfg = row.data[0] if row.data else {}
    return jsonify({
        'connected_providers': [{'provider': p, 'label': PROVIDER_LABELS.get(p, p.title())} for p in connected],
        'customer_sync_provider': cfg.get('customer_sync_provider') or '',
        'vendor_sync_provider':   cfg.get('vendor_sync_provider') or '',
    })

@app.route('/api/integrations/sync-config', methods=['POST'])
def set_sync_config():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    d = request.json or {}
    connected = set(_connected_providers(claims))
    update = {}
    for kind in ('customer_sync_provider', 'vendor_sync_provider'):
        if kind not in d: continue
        val = (d.get(kind) or '').strip().lower()
        if val and val not in connected:
            return jsonify({'error': f'"{PROVIDER_LABELS.get(val, val)}" is not connected yet — connect it first.'}), 400
        update[kind] = val or None
    if update:
        sb.table('companies').update(update).eq('id', claims['company_id']).execute()
    return jsonify({'ok': True})

# ── Generic sync: customers ─────────────────────────────────────────────────────
@app.route('/api/integrations/sync-customers', methods=['POST'])
def sync_customers():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    provider = _company_provider(claims, 'customer_sync_provider')
    if not provider:
        return jsonify({'error': 'No customer sync provider configured. Set one in Team & Settings → Integrations.'}), 400
    adapter = ADAPTERS.get(provider)
    if not adapter:
        return jsonify({'error': f'Unknown provider "{provider}"'}), 400

    creds = _get_creds(claims, provider)
    if not creds or not adapter.is_configured(creds):
        return jsonify({'error': f'No {PROVIDER_LABELS.get(provider, provider)} integration is connected yet. Connect one in Team & Settings.'}), 400

    try:
        contacts = adapter.fetch_customers(creds)
    except Exception as e:
        return jsonify({'error': f'{PROVIDER_LABELS.get(provider, provider)} sync failed: {str(e)[:250]}'}), 502

    rows = []
    for ct in contacts:
        if not ct.get('id'): continue
        rows.append({
            'company_id':          claims['company_id'],
            'name':                (ct.get('name') or '')[:500] or 'Unnamed contact',
            'company_name':        (ct.get('company') or '')[:500],
            'email':               (ct.get('email') or '')[:500],
            'phone':               (ct.get('phone') or '')[:500],
            'source':              provider,
            'external_contact_id': ct['id'],
        })
    if rows:
        try:
            sb.table('customers').upsert(rows, on_conflict='company_id,external_contact_id').execute()
        except Exception as e:
            return jsonify({'error': f'Could not save synced customers: {str(e)[:300]}'}), 502

    _mark_synced(claims, provider)
    return jsonify({'synced': len(rows)})

# ── Generic sync: vendors ───────────────────────────────────────────────────────
@app.route('/api/integrations/sync-vendors', methods=['POST'])
def sync_vendors():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    provider = _company_provider(claims, 'vendor_sync_provider')
    if not provider:
        return jsonify({'error': 'No vendor sync provider configured. Set one in Team & Settings → Integrations.'}), 400
    adapter = ADAPTERS.get(provider)
    if not adapter:
        return jsonify({'error': f'Unknown provider "{provider}"'}), 400

    creds = _get_creds(claims, provider)
    if not creds or not adapter.is_configured(creds):
        return jsonify({'error': f'No {PROVIDER_LABELS.get(provider, provider)} integration is connected yet. Connect one in Team & Settings.'}), 400

    try:
        contacts = adapter.fetch_vendors(creds)
    except Exception as e:
        return jsonify({'error': f'{PROVIDER_LABELS.get(provider, provider)} sync failed: {str(e)[:250]}'}), 502

    rows = []
    for ct in contacts:
        if not ct.get('id'): continue
        rows.append({
            'company_id':          claims['company_id'],
            'name':                (ct.get('name') or '')[:500] or 'Unnamed contact',
            'email':               (ct.get('email') or '')[:500],
            'phone':               (ct.get('phone') or '')[:500],
            'source':              provider,
            'external_contact_id': ct['id'],
        })
    if rows:
        try:
            sb.table('vendors').upsert(rows, on_conflict='company_id,external_contact_id').execute()
        except Exception as e:
            return jsonify({'error': f'Could not save synced vendors: {str(e)[:300]}'}), 502

    _mark_synced(claims, provider)
    return jsonify({'synced': len(rows)})

# ── Live customer search (used by the quote editor's Zoho-style search icon) ──
@app.route('/api/integrations/search-customers', methods=['GET'])
def search_customers():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    # Not admin-gated — any team member can look up customers while building a
    # quote. It just uses whichever provider the admin configured for customer
    # sync (Team & Settings → Integrations); if none is configured yet, this
    # quietly returns no matches rather than erroring on every keystroke.
    provider = (request.args.get('provider') or '').strip().lower() or _company_provider(claims, 'customer_sync_provider')
    if not provider:
        return jsonify({'customers': []})
    adapter = ADAPTERS.get(provider)
    if not adapter: return jsonify({'customers': []})

    search = (request.args.get('search') or '').strip().lower()
    if len(search) < 2:
        return jsonify({'customers': []})

    creds = _get_creds(claims, provider)
    if not creds or not adapter.is_configured(creds):
        return jsonify({'customers': []})

    try:
        contacts = adapter.fetch_customers(creds)
    except Exception:
        return jsonify({'error': f'{PROVIDER_LABELS.get(provider, provider)} is unreachable right now'}), 502

    def rank(ct):
        name = (ct['name'] or '').lower(); comp = (ct['company'] or '').lower(); mail = (ct['email'] or '').lower()
        if name.startswith(search): return 0
        if any(w.startswith(search) for w in name.split()): return 1
        if search in name or search in comp: return 2
        if search in mail: return 3
        return None

    matches = []
    for ct in contacts:
        r = rank(ct)
        if r is not None: matches.append((r, ct['name'].lower(), ct))
    matches.sort(key=lambda t: (t[0], t[1]))
    return jsonify({'customers': [m[2] for m in matches[:10]]})

if __name__ == '__main__':
    app.run(debug=True)
