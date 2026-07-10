from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt, json, time
import urllib.request, urllib.error, urllib.parse
from supabase import create_client

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Zoho Books adapter ───────────────────────────────────────────────────────────
# Defined inline (not as a separate module) because Vercel's Python builder only
# bundles the single entrypoint file per route — a sibling `import _zoho_adapter`
# fails at runtime with ModuleNotFoundError since the second file never gets
# packaged. Every other adapter should follow the same pattern: a small class
# below, registered in ADAPTERS, with fetch_customers/fetch_vendors/
# is_configured/test_connection methods and credentials passed as a parameter
# (never read from global env vars) so each company can use its own account.
ZOHO_ACCOUNTS = 'https://accounts.zoho.com'
ZOHO_API      = 'https://www.zohoapis.com/books/v3'

class ZohoAdapter:
    REQUIRED_FIELDS = ['client_id', 'client_secret', 'refresh_token', 'org_id']

    def __init__(self):
        self._tok_cache = {}  # keyed by refresh_token, so warm instances don't collide across companies

    def is_configured(self, creds):
        creds = creds or {}
        return all(creds.get(f) for f in self.REQUIRED_FIELDS)

    def _access_token(self, creds):
        key = creds['refresh_token']
        cached = self._tok_cache.get(key)
        if cached and time.time() < cached['exp'] - 60:
            return cached['value']
        params = urllib.parse.urlencode({
            'refresh_token': creds['refresh_token'],
            'client_id':     creds['client_id'],
            'client_secret': creds['client_secret'],
            'grant_type':    'refresh_token',
        }).encode('utf-8')
        req = urllib.request.Request(ZOHO_ACCOUNTS + '/oauth/v2/token', data=params, method='POST')
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if 'access_token' not in data:
            raise RuntimeError('Zoho token refresh failed: ' + json.dumps(data)[:200])
        self._tok_cache[key] = {'value': data['access_token'], 'exp': time.time() + int(data.get('expires_in', 3600))}
        return data['access_token']

    def _get(self, creds, path, params):
        params = dict(params or {})
        params['organization_id'] = creds['org_id']
        url = f"{ZOHO_API}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={'Authorization': 'Zoho-oauthtoken ' + self._access_token(creds)})
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode('utf-8'))

    def _fetch_contacts(self, creds, contact_type):
        out, page = [], 1
        while page <= 10:  # safety cap: 10 pages x 200 = 2000 contacts
            data = self._get(creds, '/contacts', {'contact_type': contact_type, 'per_page': 200,
                                                   'page': page, 'sort_column': 'contact_name'})
            for ct in data.get('contacts', []):
                out.append({
                    'id':      ct.get('contact_id'),
                    'name':    ct.get('contact_name') or ct.get('company_name') or '',
                    'company': ct.get('company_name') or '',
                    'email':   ct.get('email') or '',
                    'phone':   ct.get('phone') or ct.get('mobile') or '',
                })
            if not (data.get('page_context') or {}).get('has_more_page'):
                break
            page += 1
        return out

    def fetch_customers(self, creds):
        return self._fetch_contacts(creds, 'customer')

    def fetch_vendors(self, creds):
        return self._fetch_contacts(creds, 'vendor')

    def test_connection(self, creds):
        contacts = self.fetch_customers(creds)
        return {'ok': True, 'sample_count': len(contacts)}

# ── Adapter registry ────────────────────────────────────────────────────────────
# Each entry provides fetch_customers(creds), fetch_vendors(creds), and
# is_configured(creds)/test_connection(creds). Adding a new provider (e.g.
# QuickBooks, HubSpot) means writing one more class above with the same shape
# and registering it here — nothing else in this file needs to change.
ADAPTERS = {
    'zoho': ZohoAdapter(),
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
