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
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'ignore')[:300]
            raise RuntimeError(f'Zoho API error {e.code}: {detail}')

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

    def _post(self, creds, path, body):
        params = urllib.parse.urlencode({'organization_id': creds['org_id']})
        url = f"{ZOHO_API}{path}?{params}"
        data = json.dumps(body).encode('utf-8')
        req = urllib.request.Request(url, data=data, method='POST', headers={
            'Authorization': 'Zoho-oauthtoken ' + self._access_token(creds),
            'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'ignore')[:300]
            raise RuntimeError(f'Zoho API error {e.code}: {detail}')

    def create_project(self, creds, name, customer_id, rate=None):
        # billing_type='fixed_cost_for_project' requires Zoho's `rate` field
        # at creation time (Zoho rejects the call with "Please enter rate for
        # this project" -- code 20007 -- otherwise). `rate` is what Zoho later
        # displays as "Total Project Cost", so this is the awarded quote's
        # contract value (projects.original_selling_price / revenue_forecast).
        body = {'project_name': str(name)[:100], 'customer_id': str(customer_id), 'billing_type': 'fixed_cost_for_project'}
        if rate:
            body['rate'] = float(rate)
        r = self._post(creds, '/projects', body)
        proj = r.get('project') or {}
        if not proj.get('project_id'):
            raise RuntimeError('Zoho did not return a project id: ' + json.dumps(r)[:200])
        return proj

    # ── Project Performance actuals fetch ───────────────────────────────────
    # Everything below is scoped to one Zoho Books project at a time (via the
    # zoho_project_id already stored on the local `projects` row) rather than
    # pulling the whole organisation and matching after the fact -- matches
    # what api/pp_sync.py needs per-project, and avoids pagination blowing up
    # on companies with a large multi-year transaction history.
    #
    # Field names below follow Zoho Books API v3's documented shape
    # (purchaseorder_id / bill_id / invoice_id / expense_id /
    # customerpayment_id are the standard v3 identifiers). This has not yet
    # been run against a live Zoho org from this environment -- no Zoho
    # credentials are available here, and this is production business data
    # that shouldn't be touched without Nish present. Treat the exact field
    # mapping as provisional until the first real "Sync Now" run in Phase 1
    # testing; _get()/_post() already surface the raw Zoho error body if a
    # field or endpoint doesn't match what's returned, so a bad assumption
    # here fails loudly rather than silently miscalculating cost.
    def _fetch_paginated(self, creds, path, params, list_key, max_pages=25):
        out, page = [], 1
        while page <= max_pages:  # safety cap: 25 pages x 200 = 5000 records per resource per project
            p = dict(params or {})
            p.update({'per_page': 200, 'page': page})
            data = self._get(creds, path, p)
            out.extend(data.get(list_key, []) or [])
            if not (data.get('page_context') or {}).get('has_more_page'):
                break
            page += 1
        return out

    def fetch_purchase_orders(self, creds, zoho_project_id):
        """Returns (po_dicts, line_dicts). Zoho lets each PO *line item* be
        assigned to a different project (line item -> More -> Project) --
        confirmed in Zoho's own API docs, line_items[].project_id /
        .project_name -- so a single PO can span multiple projects. Its
        header-level `total` is the sum across ALL of them, not just this
        one. When any line item carries a project_id, this scopes the PO's
        total/billed_total and the stored lines down to just the lines that
        belong to THIS project, instead of attributing the whole PO to
        whichever project's sync happens to run first. Falls back to the
        header total when no line carries a project_id at all (org isn't
        using per-line project assignment). Kept in sync by hand with the
        duplicate of this method in api/projects.py's _ZohoSync -- that one
        is what the actual sync engine calls; this copy exists for the same
        one-file-per-route reason as the rest of this adapter."""
        rows = self._fetch_paginated(creds, '/purchaseorders', {'project_id': zoho_project_id}, 'purchaseorders')
        pos, lines = [], []
        for r in rows:
            po_id = r.get('purchaseorder_id')
            if not po_id: continue
            try:
                detail = self._get(creds, f'/purchaseorders/{po_id}', {})
                all_lines = (detail.get('purchaseorder') or {}).get('line_items') or []
            except Exception:
                all_lines = []  # best-effort; PO header row still syncs on the fallback below

            lines_with_project = [li for li in all_lines if li.get('project_id')]
            if lines_with_project:
                matching = [li for li in all_lines if str(li.get('project_id') or '') == str(zoho_project_id)]
                po_total = sum(float(li.get('item_total') or 0) for li in matching)
                header_total = float(r.get('total') or 0)
                share = (po_total / header_total) if header_total else 0.0
                billed_total = float(r.get('total_invoiced_amount') or r.get('billed_amount') or 0) * share
            else:
                matching = all_lines
                po_total = r.get('total') or 0
                billed_total = r.get('total_invoiced_amount') or r.get('billed_amount') or 0

            pos.append({
                'zoho_purchase_order_id': po_id,
                'zoho_project_id': zoho_project_id,
                'po_number': r.get('purchaseorder_number'),
                'po_date': r.get('date'),
                'vendor_name': r.get('vendor_name'),
                'status': r.get('status'),
                'total': po_total,
                'billed_total': billed_total,
                'raw': r,
            })
            for li in matching:
                lines.append({
                    'zoho_purchase_order_id': po_id,
                    'zoho_purchase_order_line_id': li.get('line_item_id'),
                    'zoho_item_id': li.get('item_id'),
                    'item_name': li.get('name') or li.get('description'),
                    'quantity': li.get('quantity') or 0,
                    'rate': li.get('rate') or 0,
                    'total': li.get('item_total') or 0,
                    'raw': li,
                })
        return pos, lines

    def fetch_bills(self, creds, zoho_project_id):
        """Same rationale as fetch_purchase_orders above: Zoho lets each
        bill line item be assigned to its own project too, so a bill can
        span multiple projects. Scopes total/lines to just this project's
        share when line items carry a project_id; falls back to the header
        total otherwise. Kept in sync by hand with api/projects.py's
        _ZohoSync copy, which is what the sync engine actually calls."""
        rows = self._fetch_paginated(creds, '/bills', {'project_id': zoho_project_id}, 'bills')
        bills, lines = [], []
        for r in rows:
            bill_id = r.get('bill_id')
            if not bill_id: continue
            try:
                detail = self._get(creds, f'/bills/{bill_id}', {})
                all_lines = (detail.get('bill') or {}).get('line_items') or []
            except Exception:
                all_lines = []

            lines_with_project = [li for li in all_lines if li.get('project_id')]
            if lines_with_project:
                matching = [li for li in all_lines if str(li.get('project_id') or '') == str(zoho_project_id)]
                bill_total = sum(float(li.get('item_total') or 0) for li in matching)
            else:
                matching = all_lines
                bill_total = r.get('total') or 0

            bills.append({
                'zoho_bill_id': bill_id,
                'zoho_purchase_order_id': r.get('purchaseorder_id') or r.get('purchase_order_id'),
                'bill_number': r.get('bill_number'),
                'bill_date': r.get('date'),
                'vendor_name': r.get('vendor_name'),
                'status': r.get('status'),
                'total': bill_total,
                'raw': r,
            })
            for li in matching:
                lines.append({
                    'zoho_bill_id': bill_id,
                    'zoho_bill_line_id': li.get('line_item_id'),
                    'zoho_item_id': li.get('item_id'),
                    'item_name': li.get('name') or li.get('description'),
                    'quantity': li.get('quantity') or 0,
                    'rate': li.get('rate') or 0,
                    'total': li.get('item_total') or 0,
                    'raw': li,
                })
        return bills, lines

    def fetch_expenses(self, creds, zoho_project_id):
        rows = self._fetch_paginated(creds, '/expenses', {'project_id': zoho_project_id}, 'expenses')
        return [{
            'zoho_expense_id': r.get('expense_id'),
            'expense_account': r.get('account_name'),
            'vendor_name': r.get('vendor_name') or r.get('paid_through_account_name'),
            'expense_date': r.get('date'),
            'amount': r.get('total') or r.get('amount') or 0,
            'description': r.get('description'),
            'raw': r,
        } for r in rows if r.get('expense_id')]

    def fetch_invoices(self, creds, zoho_project_id):
        rows = self._fetch_paginated(creds, '/invoices', {'project_id': zoho_project_id}, 'invoices')
        return [{
            'zoho_invoice_id': r.get('invoice_id'),
            'invoice_number': r.get('invoice_number'),
            'invoice_date': r.get('date'),
            'due_date': r.get('due_date'),
            'status': r.get('status'),
            'total': r.get('total') or 0,
            'balance': r.get('balance') or 0,
            'raw': r,
        } for r in rows if r.get('invoice_id')]

    def fetch_payments(self, creds, invoice_ids):
        """Zoho Books payments aren't project-scoped directly -- they're
        applied to invoices. Called with the invoice IDs already synced for
        this project (fetch_invoices output) and fetches each invoice's
        applied-payments list."""
        out = []
        for inv_id in (invoice_ids or []):
            try:
                data = self._get(creds, f'/invoices/{inv_id}/payments', {})
            except Exception:
                continue
            for p in data.get('payments', []) or []:
                out.append({
                    'zoho_payment_id': p.get('payment_id'),
                    'zoho_invoice_id': inv_id,
                    'payment_date': p.get('date'),
                    'amount': p.get('amount_applied') or p.get('amount') or 0,
                    'raw': p,
                })
        return out

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

def _get_creds_for(company_id, provider):
    row = sb.table('company_integrations').select('credentials,status').eq('company_id', company_id).eq('provider', provider).execute()
    if not row.data:
        return None
    return row.data[0].get('credentials') or {}

def _mark_synced_for(company_id, provider):
    sb.table('company_integrations').update({'last_synced_at': 'now()'}).eq('company_id', company_id).eq('provider', provider).execute()

def _connected_providers(company_id):
    """Providers this company has actually connected (status='connected')."""
    rows = sb.table('company_integrations').select('provider,status').eq('company_id', company_id).eq('status', 'connected').execute()
    return [r['provider'] for r in (rows.data or []) if r['provider'] in ADAPTERS]

def _company_provider(company_id, kind):
    """kind is 'customer_sync_provider' or 'vendor_sync_provider' — the admin's
    explicit choice of which connected integration feeds that data type."""
    row = sb.table('companies').select(kind).eq('id', company_id).execute()
    if not row.data:
        return None
    return (row.data[0].get(kind) or '').strip().lower() or None

def _sync_contacts(company_id, provider, kind):
    """Shared by the admin-triggered Sync buttons and the auto-sync cron route.
    kind is 'customers' or 'vendors'. Returns (count_synced, error_or_None)."""
    adapter = ADAPTERS.get(provider)
    if not adapter:
        return 0, f'Unknown provider "{provider}"'
    creds = _get_creds_for(company_id, provider)
    if not creds or not adapter.is_configured(creds):
        return 0, f'No {PROVIDER_LABELS.get(provider, provider)} integration is connected yet.'

    try:
        contacts = adapter.fetch_customers(creds) if kind == 'customers' else adapter.fetch_vendors(creds)
    except Exception as e:
        return 0, f'{PROVIDER_LABELS.get(provider, provider)} sync failed: {str(e)[:250]}'

    rows = []
    for ct in contacts:
        if not ct.get('id'): continue
        row = {
            'company_id':          company_id,
            'name':                (ct.get('name') or '')[:500] or 'Unnamed contact',
            'email':               (ct.get('email') or '')[:500],
            'phone':               (ct.get('phone') or '')[:500],
            'source':              provider,
            'external_contact_id': ct['id'],
        }
        if kind == 'customers':
            row['company_name'] = (ct.get('company') or '')[:500]
        rows.append(row)

    if rows:
        table = 'customers' if kind == 'customers' else 'vendors'
        try:
            sb.table(table).upsert(rows, on_conflict='company_id,external_contact_id').execute()
        except Exception as e:
            return 0, f'Could not save synced {kind}: {str(e)[:300]}'

    _mark_synced_for(company_id, provider)
    return len(rows), None

# ── Sync configuration: which connected provider feeds each data type, plus the
# auto-sync toggle ───────────────────────────────────────────────────────────────
# This is the explicit admin choice (dropdowns + a checkbox in Settings →
# Integrations), not an auto-detected "whichever is connected" guess — set once
# here, then the Sync buttons on Customers/Vendors and the auto-sync cron route
# both just use it.
@app.route('/api/integrations/sync-config', methods=['GET'])
def get_sync_config():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    connected = _connected_providers(claims['company_id'])
    row = sb.table('companies').select('customer_sync_provider,vendor_sync_provider,auto_sync_enabled').eq('id', claims['company_id']).execute()
    cfg = row.data[0] if row.data else {}
    return jsonify({
        'connected_providers': [{'provider': p, 'label': PROVIDER_LABELS.get(p, p.title())} for p in connected],
        'customer_sync_provider': cfg.get('customer_sync_provider') or '',
        'vendor_sync_provider':   cfg.get('vendor_sync_provider') or '',
        'auto_sync_enabled':      bool(cfg.get('auto_sync_enabled')),
    })

@app.route('/api/integrations/sync-config', methods=['POST'])
def set_sync_config():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    d = request.json or {}
    connected = set(_connected_providers(claims['company_id']))
    update = {}
    for kind in ('customer_sync_provider', 'vendor_sync_provider'):
        if kind not in d: continue
        val = (d.get(kind) or '').strip().lower()
        if val and val not in connected:
            return jsonify({'error': f'"{PROVIDER_LABELS.get(val, val)}" is not connected yet — connect it first.'}), 400
        update[kind] = val or None
    if 'auto_sync_enabled' in d:
        update['auto_sync_enabled'] = bool(d.get('auto_sync_enabled'))
    if update:
        sb.table('companies').update(update).eq('id', claims['company_id']).execute()
    return jsonify({'ok': True})

# ── Generic sync: customers ─────────────────────────────────────────────────────
@app.route('/api/integrations/sync-customers', methods=['POST'])
def sync_customers():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    provider = _company_provider(claims['company_id'], 'customer_sync_provider')
    if not provider:
        return jsonify({'error': 'No customer sync provider configured. Set one in Settings → Integrations.'}), 400

    count, err = _sync_contacts(claims['company_id'], provider, 'customers')
    if err: return jsonify({'error': err}), 502
    return jsonify({'synced': count})

# ── Generic sync: vendors ───────────────────────────────────────────────────────
@app.route('/api/integrations/sync-vendors', methods=['POST'])
def sync_vendors():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    provider = _company_provider(claims['company_id'], 'vendor_sync_provider')
    if not provider:
        return jsonify({'error': 'No vendor sync provider configured. Set one in Settings → Integrations.'}), 400

    count, err = _sync_contacts(claims['company_id'], provider, 'vendors')
    if err: return jsonify({'error': err}), 502
    return jsonify({'synced': count})

# ── Auto-sync: run once daily via Vercel's own Cron Jobs (see the "crons" entry
# in vercel.json) ────────────────────────────────────────────────────────────────
# Not JWT-gated (there's no logged-in user when a cron pings this). Vercel
# automatically sends `Authorization: Bearer <CRON_SECRET>` on every cron
# invocation when a CRON_SECRET env var is set on the project — that's checked
# here. A `secret` query param / X-Cron-Secret header is also accepted as a
# fallback for manual testing (e.g. curling this directly). Only processes
# companies that have explicitly turned on "Enable automatic sync" in
# Settings → Integrations.
CRON_SECRET = os.environ.get('CRON_SECRET')

@app.route('/api/integrations/run-auto-sync', methods=['GET', 'POST'])
def run_auto_sync():
    auth = request.headers.get('Authorization', '')
    provided = auth[7:] if auth.startswith('Bearer ') else ''
    provided = provided or request.args.get('secret') or request.headers.get('X-Cron-Secret') or ''
    if not CRON_SECRET or provided != CRON_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    companies = sb.table('companies').select('id,customer_sync_provider,vendor_sync_provider').eq('auto_sync_enabled', True).execute()
    results = []
    for co in (companies.data or []):
        cid = co['id']
        for kind, field in (('customers', 'customer_sync_provider'), ('vendors', 'vendor_sync_provider')):
            provider = (co.get(field) or '').strip().lower()
            if not provider: continue
            count, err = _sync_contacts(cid, provider, kind)
            results.append({'company_id': cid, 'type': kind, 'provider': provider, 'synced': count, 'error': err})
    return jsonify({'companies_processed': len(companies.data or []), 'results': results})

# ── Create a Zoho Books project from an app project ─────────────────────────────
@app.route('/api/integrations/zoho/create-project', methods=['POST'])
def zoho_create_project():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not (claims.get('features') or {}).get('project_performance'):
        return jsonify({'error': 'Feature not enabled'}), 403
    pid = (request.json or {}).get('project_id')
    if not pid: return jsonify({'error': 'project_id is required'}), 400
    row = sb.table('projects').select('*').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Project not found'}), 404
    proj = row.data[0]
    if proj.get('zoho_project_id'):
        return jsonify({'error': 'Already linked to Zoho', 'zoho_project_id': proj['zoho_project_id']}), 409
    creds = _get_creds_for(claims['company_id'], 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected. Connect it in Integrations first.'}), 400
    customer_name = (proj.get('customer') or '').strip()
    if not customer_name:
        return jsonify({'error': 'Project has no customer set. Add a customer first.'}), 400
    match = sb.table('customers').select('external_contact_id,name').eq('company_id', claims['company_id']).ilike('name', customer_name).execute()
    if not match.data or not match.data[0].get('external_contact_id'):
        return jsonify({'error': f'Customer "{customer_name}" not found in the synced Zoho customer list. Sync customers or check the exact name.'}), 400
    # adapter.create_project() (and the _post()/_get() it calls) raise plain
    # RuntimeErrors on any Zoho API failure (bad auth, expired token, invalid
    # field, org mismatch, etc). Uncaught, that fell through to Flask's
    # default error handler, which returns a generic HTML 500 page instead
    # of JSON -- the frontend then choked trying to parse it as JSON, and
    # the *actual* Zoho error message (which _post()/_get() already capture)
    # never reached the user. Catching it here and returning it as JSON is
    # the fix -- this endpoint should never 500 with an opaque HTML page.
    # Zoho requires `rate` (-> "Total Project Cost") up front for a
    # fixed_cost_for_project project. This project came from an awarded
    # quote, so its contract value was already frozen onto the row by
    # _freeze_commercial_baseline() -- original_selling_price is the primary
    # source, revenue_forecast is the same figure kept as a fallback.
    rate = proj.get('original_selling_price') or proj.get('revenue_forecast')
    if not rate:
        return jsonify({'error': 'This project has no value set (original_selling_price/revenue_forecast) -- Zoho requires a project cost/rate to create a fixed-cost project.'}), 400
    try:
        zoho_proj = adapter.create_project(creds, proj['name'], match.data[0]['external_contact_id'], rate=rate)
    except Exception as e:
        return jsonify({'error': f'Zoho project creation failed: {str(e)[:400]}'}), 502
    # zoho_project_id is Zoho's internal id (required for every subsequent
    # Zoho API call -- POs/bills/expenses/invoices are all fetched by it).
    # zoho_project_no is the human-readable number staff actually use,
    # stored in Zoho as the cf_project_no custom field. It may not be set
    # yet at creation time (it's often filled in inside Zoho afterward) --
    # that's fine, a later sync/import picks it up once it exists.
    zoho_project_no = zoho_proj.get('cf_project_no') or None
    sb.table('projects').update({
        'zoho_project_id': zoho_proj['project_id'], 'zoho_project_no': zoho_project_no,
    }).eq('id', pid).eq('company_id', claims['company_id']).execute()
    # Mirror the link onto the originating quote too (if this project came
    # from an awarded quote) so the Zoho project number is visible right on
    # the quote itself, not only via the linked project.
    if proj.get('quotation_id'):
        sb.table('quotes').update({
            'zoho_project_id': zoho_proj['project_id'], 'zoho_project_no': zoho_project_no,
        }).eq('id', proj['quotation_id']).eq('company_id', claims['company_id']).execute()
    return jsonify({'ok': True, 'zoho_project_id': zoho_proj['project_id'], 'zoho_project_no': zoho_project_no})

# Note: a live "/api/integrations/search-customers" route used to live here,
# searching the connected provider (e.g. Zoho) directly on every keystroke.
# It's no longer called from anywhere in the frontend — customer search during
# quote creation now searches the local, synced `customers` table instead
# (see api/customers.py), which is faster and works even when nothing is
# connected. Removed as dead code.

if __name__ == '__main__':
    app.run(debug=True)
