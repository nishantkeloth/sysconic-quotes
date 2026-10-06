from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt, json, time
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, date, timedelta
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

    def find_customers(self, creds, name):
        """Live search of Zoho customers by name (contains). Used before
        creating so we link to an existing contact instead of duplicating."""
        data = self._get(creds, '/contacts', {'contact_type': 'customer', 'contact_name_contains': str(name)[:100],
                                               'per_page': 25})
        return [{
            'id':      ct.get('contact_id'),
            'name':    ct.get('contact_name') or ct.get('company_name') or '',
            'company': ct.get('company_name') or '',
            'email':   ct.get('email') or '',
            'phone':   ct.get('phone') or ct.get('mobile') or '',
        } for ct in data.get('contacts', []) if ct.get('contact_id')]

    def create_customer(self, creds, name, company_name=None, email=None, phone=None, trn=None):
        """Pushes a brand-new customer to Zoho Books as a Contact (contact_type
        'customer'), used when a Deal is created with a customer name that
        doesn't already exist in the synced Customer Master (see
        push_customer_to_zoho() below). Deliberately minimal -- Deals only
        collect a plain customer name, not the full Zoho "New Customer" form
        (Business/Individual type, Primary Contact first/last name, Customer
        Number, etc, as seen in Nish's Zoho org) -- so this only sets
        contact_name/company_name/email/phone and leaves everything else to
        Zoho's own defaults. customer_sub_type defaults to 'business' since
        this app's customers are virtually always companies, not individuals.
        Zoho auto-assigns the Customer Number; we never set it ourselves.
        Raises RuntimeError on any Zoho API failure (via _post()) -- caller
        decides whether that should block anything or just be surfaced."""
        body = {
            'contact_name': str(name)[:200],
            'company_name': str(company_name or name)[:200],
            'contact_type': 'customer',
            'customer_sub_type': 'business',
        }
        if email: body['email'] = str(email)[:200]
        if phone: body['phone'] = str(phone)[:50]
        if trn:
            body['tax_reg_no'] = str(trn)[:50]
        try:
            r = self._post(creds, '/contacts', body)
        except RuntimeError as e:
            # Non-GCC / VAT-less Zoho orgs reject tax_reg_no; retry without it
            # rather than blocking customer creation.
            if trn and 'tax_reg_no' in str(e):
                body.pop('tax_reg_no', None)
                r = self._post(creds, '/contacts', body)
            else:
                raise
        contact = r.get('contact') or {}
        if not contact.get('contact_id'):
            raise RuntimeError('Zoho did not return a contact id: ' + json.dumps(r)[:200])
        return contact

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

    def _put(self, creds, path, body):
        params = urllib.parse.urlencode({'organization_id': creds['org_id']})
        url = f"{ZOHO_API}{path}?{params}"
        data = json.dumps(body).encode('utf-8')
        req = urllib.request.Request(url, data=data, method='PUT', headers={
            'Authorization': 'Zoho-oauthtoken ' + self._access_token(creds),
            'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'ignore')[:300]
            raise RuntimeError(f'Zoho API error {e.code}: {detail}')

    def create_project(self, creds, name, customer_id, rate=None, cost_budget=None, revenue_budget=None):
        # billing_type='fixed_cost_for_project' requires Zoho's `rate` field
        # at creation time (Zoho rejects the call with "Please enter rate for
        # this project" -- code 20007 -- otherwise). `rate` is what Zoho later
        # displays as "Total Project Cost", so this is the awarded quote's
        # contract value (projects.original_selling_price / revenue_forecast).
        #
        # Cost Budget / Revenue Budget (Zoho's UI labels) are a separate
        # concept from `rate`, confirmed against Zoho's own API reference:
        #   budget_type='total_project_cost'  -- activates money-based budgeting
        #   budget_amount        -- Revenue Budget (what you expect to earn)
        #   cost_budget_amount   -- Cost Budget (what you expect to spend)
        # (An earlier guess of cost_budget/revenue_budget as the field names
        # was wrong -- Zoho silently ignored them rather than erroring.)
        body = {'project_name': str(name)[:100], 'customer_id': str(customer_id), 'billing_type': 'fixed_cost_for_project'}
        if rate:
            body['rate'] = float(rate)
        if cost_budget or revenue_budget:
            body['budget_type'] = 'total_project_cost'
        if revenue_budget:
            body['budget_amount'] = float(revenue_budget)
        if cost_budget:
            body['cost_budget_amount'] = float(cost_budget)
        r = self._post(creds, '/projects', body)
        proj = r.get('project') or {}
        if not proj.get('project_id'):
            raise RuntimeError('Zoho did not return a project id: ' + json.dumps(r)[:200])
        # Zoho doesn't reliably have cf_project_no (the human-readable
        # Project No, e.g. "PRJ100193") populated in the immediate create
        # response -- it gets auto-assigned moments later on Zoho's side.
        # Without this, the frontend was stuck showing the long internal
        # project_id until someone ran a separate Sync later. Poll briefly
        # (a few seconds, well inside Vercel's function budget) so the real
        # Project No comes back in the same request whenever possible; a
        # later sync still backfills it if Zoho is slower than this.
        if not proj.get('cf_project_no'):
            pid = proj['project_id']
            for wait in (1, 2):
                time.sleep(wait)
                try:
                    fresh = self._get(creds, f'/projects/{pid}', {})
                    fresh_proj = fresh.get('project') or {}
                    if fresh_proj.get('cf_project_no'):
                        proj = fresh_proj
                        break
                except Exception:
                    pass
        return proj

    # ── Estimate (Quotation) replication ─────────────────────────────────────
    # Fired alongside create_project() by the "Create Project & Quotation in
    # Zoho" button -- one click, two Zoho objects. Estimates and Projects are
    # separate, unlinked objects in Zoho Books (no native FK between them);
    # both just reference the same customer_id, so a rep sees them side by
    # side under that customer.
    def _tax_id_for_rate(self, creds, rate_pct):
        """Match a QTcal VAT rate (e.g. 5) against this org's configured Zoho
        tax list by percentage. Zoho's line items want a tax_id reference,
        not a raw percentage -- this is the resolution step. Returns None
        (leave the line untaxed) rather than raising if no match is found;
        wrong is worse than blocked, but an unmatched tax rate shouldn't
        block the whole estimate from being created -- it's a one-click fix
        inside Zoho afterward."""
        try:
            data = self._get(creds, '/settings/taxes', {})
            for t in (data.get('taxes') or []):
                if abs(float(t.get('tax_percentage') or 0) - float(rate_pct or 0)) < 0.01:
                    return t.get('tax_id')
        except Exception:
            pass
        return None

    def _salesperson_id_for_name(self, creds, name):
        """Best-effort match of QTcal's free-text 'Prepared by' name against
        this org's fixed Zoho salesperson dropdown (case-insensitive,
        substring-tolerant since the two systems don't share an identity --
        e.g. QTcal 'Rajesh' vs Zoho 'Rajesh Kumar'). None on no match --
        Zoho just leaves Salesperson blank, never a hard failure."""
        if not name: return None
        try:
            data = self._get(creds, '/settings/salespersons', {})
            needle = name.strip().lower()
            if not needle: return None
            for sp in (data.get('salespersons') or []):
                sp_name = (sp.get('salesperson_name') or '').strip().lower()
                if sp_name and (sp_name == needle or sp_name in needle or needle in sp_name):
                    return sp.get('salesperson_id')
        except Exception:
            pass
        return None

    def _line_item_custom_field_ids(self, creds):
        """Discovers this org's exact label text for the 'Brand' and 'Model
        No' custom fields. CONFIRMED 2026-09-02 by capturing the actual
        request Zoho's own web app sends when a user types Brand/Model
        directly into an ad-hoc estimate/quote line item (no catalog item
        selected): item_custom_fields entries are matched by LABEL TEXT
        only -- {'label': 'Brand', 'value': ...} -- not by customfield_id,
        even though these are technically Item-module custom fields
        (Settings > Custom Fields > Items in Zoho Books; they show up in
        every transaction's line-item table because they're configured
        there with "Show when creating transactions" + "Include in
        Modules: Quote" etc.). Every customfield_id-based shape tried
        before this discovery was silently dropped by the API. This lookup
        just gets the correctly-cased label per org (defensive against a
        renamed field) -- falls back to the literal 'Brand'/'Model No'
        text at the call site if not found. Cached on the adapter instance
        for the lifetime of one request."""
        if getattr(self, '_li_cf_cache', None) is not None:
            return self._li_cf_cache
        ids = {}
        try:
            data = self._get(creds, '/settings/customfields', {'entity': 'item'})
            raw = data.get('customfields') or {}
            fields = raw.get('item') if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            for f in (fields or []):
                if not isinstance(f, dict):
                    continue
                label_raw = (f.get('label') or f.get('field_name') or f.get('placeholder') or '')
                label = label_raw.strip().lower()
                if label == 'brand':
                    ids['brand_label'] = label_raw
                elif label in ('model no', 'model no.', 'model number', 'model'):
                    ids['model_label'] = label_raw
        except Exception:
            pass
        self._li_cf_cache = ids
        return ids

    def _header_custom_field_ids(self, creds):
        """Same idea as _line_item_custom_field_ids() but for header-level
        (entity=estimate) custom fields -- specifically 'Profit Margin' (an
        AED amount) and 'Profit Margin %', seen in Nish's org as fields on
        the New Estimate screen (Profit Margin is even marked required
        there). IDs are per-org and matched by label, cached per request.
        Best-effort: if the labels don't match what's guessed here, the
        values are just omitted and estimate creation still proceeds --
        matches the Brand/Model line-item mapping's fallback behavior."""
        if getattr(self, '_hdr_cf_cache', None) is not None:
            return self._hdr_cf_cache
        ids = {}
        try:
            data = self._get(creds, '/settings/customfields', {'entity': 'estimate'})
            fields = data.get('customfields') or data.get('customfieldsettings') or data.get('custom_fields') or []
            for f in fields:
                label = (f.get('label') or f.get('field_name') or f.get('placeholder') or '').strip().lower()
                fid = f.get('customfield_id') or f.get('field_id') or f.get('customfield_id_formatted')
                if not fid: continue
                if label in ('profit margin %', 'profit margin(%)', 'profit margin percentage'):
                    ids['profit_margin_pct'] = fid
                elif label == 'profit margin':
                    ids['profit_margin'] = fid
        except Exception:
            pass
        self._hdr_cf_cache = ids
        return ids

    def create_estimate(self, creds, customer_id, reference_number, quote_date, line_items,
                         salesperson_name=None, total_gp=None, margin_pct=None, zoho_project_id=None):
        """line_items: [{'description': str, 'rate': float, 'quantity': float,
        'tax_rate': float|None, 'brand': str|None, 'model': str|None}, ...]
        -- already-computed sell prices from QTcal's own calc_item(),
        post-discount, so Zoho's own per-line Discount is deliberately left
        at 0 rather than trying to reverse-engineer an equivalent single
        percentage from QTcal's two-layer discount. Brand/Model are pushed
        into the org's Brand/Model No item-table custom fields (see below
        for the exact request shape). total_gp (QTcal's "Total GP", ts-tc
        in AED) and margin_pct
        (fraction, e.g. 0.167) map the same way onto the header-level
        Profit Margin / Profit Margin % custom fields when found.
        zoho_project_id links this estimate to the Zoho Project created
        alongside it -- Zoho's Estimates API documents a native `project_id`
        field for exactly this (project-based billing/tracking/reporting),
        confirmed against Zoho's own API reference. Passed straight through
        (unlike Brand/Model/Profit Margin, this isn't a custom field, so no
        org-specific discovery step is needed).
        Brand/Model use the `item_custom_fields` key per line item, with
        entries shaped as {'label': <exact field label>, 'value': ...} --
        NOT {'customfield_id': ..., 'value': ...} the way header-level
        `custom_fields` works. Confirmed 2026-09-02 by capturing the actual
        request Zoho's own web app sends when a user types Brand/Model
        directly into an ad-hoc line (no catalog item) in the browser
        network tab -- every customfield_id-based shape tried before this
        was silently dropped by the API even though it's individually
        well-formed, presumably because these fields are technically owned
        by the Items module and the estimate/quote context only recognizes
        them by label, not by the Item module's own customfield_id."""
        cf_ids = self._line_item_custom_field_ids(creds) if any(li.get('brand') or li.get('model') for li in line_items) else {}
        zoho_items = []
        for li in line_items:
            item = {
                'name': (li.get('description') or 'Item')[:200],
                'description': li.get('description') or '',
                'rate': float(li.get('rate') or 0),
                'quantity': float(li.get('quantity') or 1),
                'discount': 0,
            }
            item_custom_fields = []
            if li.get('brand'):
                item_custom_fields.append({'label': cf_ids.get('brand_label') or 'Brand', 'value': li['brand']})
            if li.get('model'):
                item_custom_fields.append({'label': cf_ids.get('model_label') or 'Model No', 'value': li['model']})
            if item_custom_fields:
                item['item_custom_fields'] = item_custom_fields
            if li.get('tax_rate'):
                tax_id = self._tax_id_for_rate(creds, li['tax_rate'])
                if tax_id:
                    item['tax_id'] = tax_id
            zoho_items.append(item)

        body = {
            'customer_id': str(customer_id),
            'reference_number': (reference_number or '')[:100],
            'line_items': zoho_items,
        }
        if quote_date:
            body['date'] = quote_date
        if zoho_project_id:
            body['project_id'] = str(zoho_project_id)
        if salesperson_name:
            sp_id = self._salesperson_id_for_name(creds, salesperson_name)
            if sp_id:
                body['salesperson_id'] = sp_id
        if total_gp is not None or margin_pct is not None:
            hdr_ids = self._header_custom_field_ids(creds)
            header_custom_fields = []
            if total_gp is not None and hdr_ids.get('profit_margin'):
                header_custom_fields.append({'customfield_id': hdr_ids['profit_margin'], 'value': round(float(total_gp), 2)})
            if margin_pct is not None and hdr_ids.get('profit_margin_pct'):
                header_custom_fields.append({'customfield_id': hdr_ids['profit_margin_pct'], 'value': round(float(margin_pct) * 100, 1)})
            if header_custom_fields:
                body['custom_fields'] = header_custom_fields

        r = self._post(creds, '/estimates', body)
        est = r.get('estimate') or {}
        if not est.get('estimate_id'):
            raise RuntimeError('Zoho did not return an estimate id: ' + json.dumps(r)[:200])
        return est

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
            detail = {}
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

            # See api/projects.py's _ZohoSync.fetch_bills for the rationale --
            # Zoho's bill detail carries the PO link as 'purchaseorder_ids'
            # (array), not a singular field. Kept in sync by hand.
            po_ids = (detail.get('bill') or {}).get('purchaseorder_ids') or []
            po_id = (po_ids[0] if po_ids else None) or r.get('purchaseorder_id') or r.get('purchase_order_id')

            bills.append({
                'zoho_bill_id': bill_id,
                'zoho_purchase_order_id': po_id,
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

    def fetch_all_invoices(self, creds):
        """Org-wide invoice pull for the Insights 'Zoho Sales' widget --
        deliberately NOT filtered by project_id, unlike fetch_invoices()
        above, so it captures every sale regardless of whether it's tied to
        a tracked Project. Shares the same zoho_invoices table (see
        migration-add-zoho-sales-sync.sql): rows written here never set
        project_id, so an invoice already linked to a project by the
        Project Performance sync keeps that link untouched on upsert, while
        a plain sale with no project gets a project_id of NULL. customer_name/
        customer_id are pulled out of the raw payload since that's how the
        Insights breakdown groups totals per customer."""
        rows = self._fetch_paginated(creds, '/invoices', {}, 'invoices')
        return [{
            'zoho_invoice_id': r.get('invoice_id'),
            'invoice_number': r.get('invoice_number'),
            'invoice_date': r.get('date'),
            'due_date': r.get('due_date'),
            'status': r.get('status'),
            'total': r.get('total') or 0,
            'balance': r.get('balance') or 0,
            'customer_name': r.get('customer_name') or '',
            'customer_id': r.get('customer_id') or '',
            'raw': r,
        } for r in rows if r.get('invoice_id')]

    def fetch_all_bills(self, creds):
        """Org-wide vendor-bill pull for the Insights 'Payables' widget --
        mirrors fetch_all_invoices() above but for Zoho Books Bills
        (Accounts Payable), not filtered by project_id, so it captures every
        vendor bill regardless of whether it's tied to a tracked Project.
        Shares the same zoho_bills table as the per-project fetch_bills()
        above (see migration-add-zoho-payables-sync.sql): rows written here
        never set project_id, so a bill already linked to a Project by the
        Financial Health sync keeps that link untouched on upsert, while a
        plain bill with no project gets project_id = NULL. Unlike
        fetch_bills(), this doesn't fetch each bill's line-item detail --
        Insights only needs the header-level total/balance/due_date, not
        per-line cost categorization."""
        rows = self._fetch_paginated(creds, '/bills', {}, 'bills')
        return [{
            'zoho_bill_id': r.get('bill_id'),
            'bill_number': r.get('bill_number'),
            'bill_date': r.get('date'),
            'due_date': r.get('due_date'),
            'status': r.get('status'),
            'total': r.get('total') or 0,
            'balance': r.get('balance') or 0,
            'vendor_name': r.get('vendor_name') or '',
            'vendor_id': r.get('vendor_id') or '',
            'raw': r,
        } for r in rows if r.get('bill_id')]

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

    # ── Loans (Insights "Loans" section) ────────────────────────────────────
    # Zoho Books has no dedicated "loan" object -- a loan is just a Chart of
    # Accounts entry, usually typed other_current_liability or
    # long_term_liability, named whatever the bookkeeper called it. Other
    # liabilities (VAT payable, accrued expenses...) share those same types,
    # so there's no reliable way to auto-detect "this is a loan" -- an admin
    # picks which accounts are loans (see /api/integrations/loan-accounts).
    # This method just lists liability-type accounts with their live balance
    # for that picker; it does not decide which ones are loans.
    def fetch_liability_accounts(self, creds):
        rows = self._fetch_paginated(
            creds, '/chartofaccounts',
            {'showbalance': 'true', 'filter_by': 'AccountType.Liability'},
            'chartofaccounts', max_pages=5,
        )
        return [{
            'zoho_account_id': a.get('account_id'),
            'account_name': a.get('account_name') or '',
            'account_type': a.get('account_type') or '',
            'current_balance': a.get('current_balance') or 0,
            'is_active': a.get('is_active', True),
        } for a in rows if a.get('account_id')]

    def fetch_bank_chart_accounts(self, creds):
        """The Chart-of-Accounts entries for bank/cash accounts -- distinct
        from fetch_bank_accounts() above (the Banking module's own list,
        used for balances/uncategorized counts). On some Zoho organizations
        the same physical bank account has a different account_id in the
        Banking module vs. Chart of Accounts; this lets bank_vs_zoho_report()
        resolve the correct Chart-of-Accounts id by name when a direct
        lookup with the Banking id returns no ledger transactions."""
        rows = self._fetch_paginated(
            creds, '/chartofaccounts',
            {'filter_by': 'AccountType.Asset'},
            'chartofaccounts', max_pages=5,
        )
        return [{
            'zoho_account_id': a.get('account_id'),
            'account_name': a.get('account_name') or '',
            'account_type': a.get('account_type') or '',
        } for a in rows if (a.get('account_type') or '') in ('bank', 'cash')]

    def fetch_income_accounts(self, creds):
        """Used by the loan-transaction anomaly review (POST /api/
        integrations/loan-accounts/review) to check whether loan proceeds
        may have been booked as revenue instead of to a loan account."""
        rows = self._fetch_paginated(
            creds, '/chartofaccounts',
            {'filter_by': 'AccountType.Income'},
            'chartofaccounts', max_pages=5,
        )
        return [{
            'zoho_account_id': a.get('account_id'),
            'account_name': a.get('account_name') or '',
            'account_type': a.get('account_type') or '',
            'is_involved_in_transaction': a.get('is_involved_in_transaction', False),
        } for a in rows if a.get('account_id')]

    def fetch_profit_and_loss(self, creds, from_date, to_date):
        """Zoho Books Profit & Loss report for a date range, flattened to
        {section_name_lower: {'total': x, 'lines': [(name, total), ...]}}.
        Parsed defensively: sections are matched by (lower-cased) name."""
        data = self._get(creds, '/reports/profitandloss', {'from_date': from_date, 'to_date': to_date, 'cash_based': 'false'})
        out = {}
        def num(v):
            try: return float(v)
            except Exception: return 0.0
        def lines(node, acc):
            for ch in (node.get('account_transactions') or []):
                nm = (ch.get('name') or '').strip()
                if ch.get('account_transactions'):
                    lines(ch, acc)
                elif nm:
                    acc.append((nm, num(ch.get('total'))))
        for sec in (data.get('profitandloss') or []):
            nm = (sec.get('name') or '').strip().lower()
            if not nm: continue
            acc = []
            lines(sec, acc)
            out[nm] = {'total': num(sec.get('total')), 'lines': acc}
        return out

    def fetch_expense_accounts(self, creds):
        """Expense-type Chart-of-Accounts entries -- data source for the
        Salary account picker."""
        rows = self._fetch_paginated(creds, '/chartofaccounts', {'filter_by': 'AccountType.Expense'},
                                     'chartofaccounts', max_pages=5)
        # Payroll is often booked per employee on liability accounts
        # ("<Name> - Salary Payable"), so those are offered too.
        rows += self._fetch_paginated(creds, '/chartofaccounts', {'filter_by': 'AccountType.Liability'},
                                      'chartofaccounts', max_pages=5)
        return [{
            'zoho_account_id': a.get('account_id'),
            'account_name': a.get('account_name') or '',
            'account_type': a.get('account_type') or '',
        } for a in rows if a.get('account_id')]

    # ── Bank Reconciliation tracker ─────────────────────────────────────────
    def fetch_bank_accounts(self, creds):
        """Every bank/credit-card account, via Zoho's dedicated Banking
        module (not the Chart of Accounts one) -- account_type here
        unambiguously identifies these as bank/card accounts, so unlike
        loans no manual picker is needed. bank_balance reflects the
        connected bank feed and is only meaningful if feeds are active for
        that account (Zoho returns 0 otherwise, not distinguishable from a
        genuinely zero balance -- flagged to the caller via is_feeds_active
        so the UI doesn't claim a false balance-match)."""
        rows = self._fetch_paginated(creds, '/bankaccounts', {}, 'bankaccounts', max_pages=5)
        return [{
            'zoho_account_id': a.get('account_id'),
            'account_name': a.get('account_name') or '',
            'account_type': a.get('account_type') or '',
            'bank_name': a.get('bank_name') or '',
            'balance': a.get('balance') or 0,
            'bank_balance': a.get('bank_balance') or 0,
            'is_feeds_active': bool(a.get('is_feeds_active')),
            'uncategorized_transactions': a.get('uncategorized_transactions') or 0,
            'is_active': a.get('is_active', True),
        } for a in rows if a.get('account_id')]

    def fetch_bank_reconciliations(self, creds, account_id):
        """Zoho's own reconciliation history for one bank/card account --
        each record is one completed (or draft) "Reconcile Account" run the
        user already does in Zoho whenever a bank statement comes in.
        Field names are defensively read (start_date/end_date/status/
        save_option) since Zoho's public docs don't show a full example for
        this endpoint's response -- code that consumes this treats an
        unexpected shape as informational, never as a false "reconciled"."""
        out, page = [], 1
        while page <= 10:  # a company reconciling monthly won't exceed this for years
            data = self._get(creds, f'/bankaccounts/{account_id}/reconciliations', {'page': page, 'per_page': 200})
            batch = data.get('reconciliations') or data.get('bankreconciliations') or []
            out.extend(batch)
            if not batch or not (data.get('page_context') or {}).get('has_more_page'):
                break
            page += 1
        return out

    def fetch_bank_transactions(self, creds, account_id, date_start=None, date_end=None):
        """Zoho's own Banking module transactions for one account -- distinct
        from the Chart-of-Accounts ledger (fetch_account_transactions below).
        These are the lines Zoho itself received from a bank feed or a
        manually-imported bank statement (Banking -> Import Statement), each
        carrying Zoho's own status: uncategorized, matched, categorized,
        excluded, or manually_added. For a company that imports its bank
        statement into Zoho each month (confirmed for this company), this
        *is* the independent bank-side record -- QTcal doesn't need its own
        upload/parsing step, since Zoho already has it and already tracks
        which lines are matched to a books entry vs. not."""
        params = {'account_id': account_id, 'sort_column': 'date'}
        if date_start: params['date_start'] = date_start
        if date_end: params['date_end'] = date_end
        return self._fetch_paginated(creds, '/banktransactions', params, 'banktransactions', max_pages=25)

    def fetch_account_transactions(self, creds, account_id, extra_params=None):
        """Every transaction Zoho has ever posted against one Chart-of-
        Accounts entry, fully paginated -- used to compute a loan account's
        lifetime total_repaid. debit_or_credit is standard double-entry: for
        a liability account, a CREDIT increases the balance (money borrowed)
        and a DEBIT decreases it (a repayment or other reduction). Summing
        every debit here, then deriving total_borrowed = current_balance +
        total_repaid elsewhere, is exact ledger arithmetic (it always equals
        the sum of every credit ever posted) -- not an estimate."""
        # filter_by is passed explicitly (rather than relying on Zoho's
        # default) because some accounts return an empty list without it --
        # seen in practice on an account whose balance came from a Zoho
        # "Opening Balance" entry rather than a dated journal/expense.
        params = {'account_id': account_id, 'filter_by': 'TransactionType.All', 'sort_column': 'transaction_date'}
        params.update(extra_params or {})
        return self._fetch_paginated(
            creds, '/chartofaccounts/accounttransactions', params,
            'transactions', max_pages=25,
        )

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

def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))

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

# ── Org-wide sales sync: powers the Insights "Zoho Sales" widget ────────────────
# Distinct from the per-project invoice sync in pp_sync.py/projects.py -- this
# pulls EVERY invoice in the Zoho Books org, not just ones tied to a tracked
# Project, so Insights can show a true company-wide sales total broken down by
# customer. Shares the same zoho_invoices table (see
# migration-add-zoho-sales-sync.sql): rows written here never include a
# project_id key at all, so upserting an invoice that's already linked to a
# project (by the other sync) leaves that link completely untouched -- only
# customer_name/customer_id/total/balance/status/raw get refreshed.
@app.route('/api/integrations/sync-sales', methods=['POST'])
def sync_sales():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    company_id = claims['company_id']
    creds = _get_creds_for(company_id, 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected. Connect it in Settings → Integrations first.'}), 400

    try:
        invoices = adapter.fetch_all_invoices(creds)
    except Exception as e:
        return jsonify({'error': f'Zoho sales sync failed: {str(e)[:300]}'}), 502

    for inv in invoices:
        inv['company_id'] = company_id
        # Deliberately no project_id key here -- see comment above.

    if invoices:
        try:
            sb.table('zoho_invoices').upsert(invoices, on_conflict='company_id,zoho_invoice_id').execute()
        except Exception as e:
            return jsonify({'error': f'Could not save synced invoices: {str(e)[:300]}'}), 502

    return jsonify({'synced': len(invoices)})

# ── Org-wide payables sync: powers the Insights "Zoho Payables" widget ──────────
# Mirrors sync_sales above but for vendor Bills (Accounts Payable). Pulls
# EVERY bill in the Zoho Books org, not just ones tied to a tracked Project,
# so Insights can show a true company-wide "what do we owe, and what's due
# soon" total. Shares the same zoho_bills table used by the per-project
# Financial Health sync (see migration-add-zoho-payables-sync.sql): rows
# written here never include a project_id key, so upserting a bill that's
# already linked to a project leaves that link untouched -- only
# vendor_name/total/balance/due_date/status/raw get refreshed.
@app.route('/api/integrations/sync-payables', methods=['POST'])
def sync_payables():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(claims): return jsonify(ADMIN_ONLY), 403

    company_id = claims['company_id']
    creds = _get_creds_for(company_id, 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected. Connect it in Settings → Integrations first.'}), 400

    try:
        bills = adapter.fetch_all_bills(creds)
    except Exception as e:
        return jsonify({'error': f'Zoho payables sync failed: {str(e)[:300]}'}), 502

    for b in bills:
        b['company_id'] = company_id
        # Deliberately no project_id key here -- see comment above.

    if bills:
        try:
            # Plain upsert with on_conflict='company_id,zoho_bill_id' doesn't
            # work against this table: migration-bill-split-projects.sql
            # widened zoho_bills' unique constraint to (company_id,
            # zoho_bill_id, project_id) so a bill split across multiple
            # projects can keep one row per project. That breaks a 2-column
            # ON CONFLICT target outright (Postgres error 42P10 -- "no
            # unique or exclusion constraint matching the ON CONFLICT
            # specification"), which is what was silently failing every
            # org-wide payables sync up to now. It also can't be fixed by
            # just widening our on_conflict to match: our rows always have
            # project_id = NULL (never set, see above), and standard SQL
            # treats every NULL as distinct from every other NULL for
            # uniqueness purposes, so ON CONFLICT would never actually
            # detect a "conflict" against a prior NULL-project row anyway --
            # it would just INSERT a fresh duplicate every single sync.
            # Delete-then-insert sidesteps both problems: this is already a
            # full org-wide pull every time (not incremental), so wiping our
            # own NULL-project rows for this company and reinserting is
            # exactly equivalent to an upsert, doesn't touch any other
            # project's scoped rows for the same bill_id (those keep their
            # own project_id, untouched), and also correctly drops rows for
            # any bill that's since been deleted/voided in Zoho.
            sb.table('zoho_bills').delete().eq('company_id', company_id).is_('project_id', 'null').execute()
            sb.table('zoho_bills').insert(bills).execute()
        except Exception as e:
            return jsonify({'error': f'Could not save synced bills: {str(e)[:300]}'}), 502

    return jsonify({'synced': len(bills)})

# ── Loans (Insights "Loans" section) ────────────────────────────────────────────
# See ZohoAdapter.fetch_liability_accounts/fetch_account_transactions above and
# migration-add-loan-accounts.sql for the full rationale: Zoho has no "loan"
# object, so an admin explicitly picks which Chart-of-Accounts liability
# entries are loans (list_zoho_liability_accounts below is that picker's data
# source), and only picked ("tracked") accounts are ever synced or shown.
@app.route('/api/integrations/zoho-liability-accounts', methods=['GET'])
def list_zoho_liability_accounts():
    """Live, unsaved list -- lets the admin picker show current Zoho balances
    without writing anything until they actually click Save."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403

    company_id = claims['company_id']
    creds = _get_creds_for(company_id, 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected. Connect it in Settings → Integrations first.'}), 400

    try:
        accounts = adapter.fetch_liability_accounts(creds)
    except Exception as e:
        return jsonify({'error': f'Could not fetch Zoho accounts: {str(e)[:300]}'}), 502

    tracked_ids = {r['zoho_account_id'] for r in (
        sb.table('loan_accounts').select('zoho_account_id').eq('company_id', company_id).eq('is_tracked', True).execute().data or []
    )}
    for a in accounts:
        a['is_tracked'] = a['zoho_account_id'] in tracked_ids
    return jsonify({'accounts': accounts})

@app.route('/api/integrations/loan-accounts', methods=['GET'])
def get_loan_accounts():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    rows = sb.table('loan_accounts').select('*').eq('company_id', claims['company_id']).eq('is_tracked', True).order('account_name').execute().data or []
    return jsonify({'accounts': rows})

@app.route('/api/integrations/loan-accounts', methods=['POST'])
def save_loan_accounts():
    """Body: {accounts: [{zoho_account_id, account_name, account_type}, ...]}
    -- the full set of accounts that should be tracked as loans from now on.
    Replaces the previous selection entirely (any account not in this list
    is untracked, not deleted, so its synced history isn't lost if it's
    re-added later)."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    company_id = claims['company_id']

    picked = (request.json or {}).get('accounts') or []
    picked_ids = [a.get('zoho_account_id') for a in picked if a.get('zoho_account_id')]

    # Untrack everything first, then (re-)track exactly what was picked --
    # simpler and safer than diffing, and cheap since a company will only
    # ever have a handful of loan accounts.
    sb.table('loan_accounts').update({'is_tracked': False}).eq('company_id', company_id).execute()
    for a in picked:
        row = {
            'company_id': company_id, 'zoho_account_id': a.get('zoho_account_id'),
            'account_name': a.get('account_name') or '', 'account_type': a.get('account_type') or '',
            'is_tracked': True,
        }
        sb.table('loan_accounts').upsert(row, on_conflict='company_id,zoho_account_id').execute()
    return jsonify({'ok': True, 'tracked': len(picked_ids)})

@app.route('/api/integrations/sync-loans', methods=['POST'])
def sync_loans():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    company_id = claims['company_id']

    tracked = sb.table('loan_accounts').select('*').eq('company_id', company_id).eq('is_tracked', True).execute().data or []
    if not tracked:
        return jsonify({'synced': 0, 'message': 'No loan accounts selected yet.'})

    creds = _get_creds_for(company_id, 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected. Connect it in Settings → Integrations first.'}), 400

    try:
        live_accounts = {a['zoho_account_id']: a for a in adapter.fetch_liability_accounts(creds)}
    except Exception as e:
        return jsonify({'error': f'Zoho sync failed: {str(e)[:300]}'}), 502

    now_iso = datetime.utcnow().isoformat()
    synced = 0
    for row in tracked:
        acc_id = row['zoho_account_id']
        live = live_accounts.get(acc_id)
        current_balance = (live or {}).get('current_balance') or 0
        try:
            txns = adapter.fetch_account_transactions(creds, acc_id)
        except Exception:
            txns = []  # best-effort: keep the previous total_repaid rather than zero it on a transient error
        total_repaid = sum(
            float(t.get('debit_amount') or 0) for t in txns if (t.get('debit_or_credit') or '').lower() == 'debit'
        ) if txns else (row.get('total_repaid') or 0)
        total_borrowed = current_balance + total_repaid
        sb.table('loan_accounts').update({
            'account_name': (live or {}).get('account_name') or row.get('account_name'),
            'account_type': (live or {}).get('account_type') or row.get('account_type'),
            'current_balance': current_balance,
            'total_repaid': total_repaid,
            'total_borrowed': total_borrowed,
            'last_synced_at': now_iso,
        }).eq('id', row['id']).execute()
        synced += 1
    return jsonify({'synced': synced})

@app.route('/api/integrations/loan-accounts/<id>/transactions', methods=['GET'])
def get_loan_account_transactions(id):
    """Live drill-down, not persisted -- fetched fresh from Zoho each time
    the admin opens it, same tradeoff as sync-loans (an extra API call for
    always-current data rather than a stale local copy). Not admin-gated:
    anyone who can already see the Loans widget in Insights can open its
    detail, same as the Zoho Sales section's per-customer invoice drill-down."""
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    company_id = claims['company_id']

    row = sb.table('loan_accounts').select('*').eq('id', id).eq('company_id', company_id).execute().data
    if not row: return jsonify({'error': 'Loan account not found'}), 404
    account = row[0]

    creds = _get_creds_for(company_id, 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected.'}), 400

    try:
        txns = adapter.fetch_account_transactions(creds, account['zoho_account_id'])
    except Exception as e:
        return jsonify({'error': f'Could not fetch transactions: {str(e)[:300]}'}), 502

    out = [{
        'date': t.get('transaction_date'),
        'type': (t.get('transaction_type') or '').replace('_', ' '),
        'payee': t.get('payee') or '',
        'description': t.get('description') or '',
        'debit_or_credit': (t.get('debit_or_credit') or '').lower(),
        'amount': float(t.get('debit_amount') or 0) if (t.get('debit_or_credit') or '').lower() == 'debit' else float(t.get('credit_amount') or 0),
        'reference_number': t.get('reference_number') or '',
        'entry_number': t.get('entry_number') or '',
    } for t in txns]
    out.sort(key=lambda r: r.get('date') or '', reverse=True)
    return jsonify({
        'account_name': account.get('account_name'),
        'current_balance': account.get('current_balance'),
        'transactions': out,
    })

# ── Salaries (Insights "Salaries" page: per month, per employee) ───────────────
# Admin picks the Zoho expense account(s) that carry payroll; sync stores each
# ledger entry (debit = cost, credit = reversal) as a salary_payments row.
@app.route('/api/integrations/zoho-expense-accounts', methods=['GET'])
def list_zoho_expense_accounts():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    cid = claims['company_id']
    creds = _get_creds_for(cid, 'zoho'); adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected.'}), 400
    try:
        accounts = adapter.fetch_expense_accounts(creds)
    except Exception as e:
        return jsonify({'error': f'Could not fetch Zoho accounts: {str(e)[:300]}'}), 502
    tracked = {r['zoho_account_id'] for r in (sb.table('salary_accounts').select('zoho_account_id')
               .eq('company_id', cid).eq('is_tracked', True).execute().data or [])}
    for a in accounts: a['is_tracked'] = a['zoho_account_id'] in tracked
    def _sal_like(a): return any(k in (a['account_name'] or '').lower() for k in ('salary', 'payroll', 'wage'))
    accounts.sort(key=lambda a: (not _sal_like(a), a['account_name'].lower()))
    return jsonify({'accounts': accounts})

@app.route('/api/integrations/salary-accounts', methods=['GET', 'POST'])
def salary_accounts_route():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    cid = claims['company_id']
    if request.method == 'GET':
        rows = sb.table('salary_accounts').select('*').eq('company_id', cid).eq('is_tracked', True).order('account_name').execute().data or []
        return jsonify({'accounts': rows})
    picked = (request.json or {}).get('accounts') or []
    sb.table('salary_accounts').update({'is_tracked': False}).eq('company_id', cid).execute()
    for a in picked:
        if not a.get('zoho_account_id'): continue
        sb.table('salary_accounts').upsert({'company_id': cid, 'zoho_account_id': a['zoho_account_id'],
            'account_name': a.get('account_name') or '', 'is_tracked': True}, on_conflict='company_id,zoho_account_id').execute()
    return jsonify({'ok': True, 'tracked': len(picked)})

@app.route('/api/integrations/sync-salaries', methods=['POST'])
def sync_salaries():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    cid = claims['company_id']
    tracked = sb.table('salary_accounts').select('*').eq('company_id', cid).eq('is_tracked', True).execute().data or []
    if not tracked: return jsonify({'synced': 0, 'message': 'No salary accounts selected yet.'})
    creds = _get_creds_for(cid, 'zoho'); adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected.'}), 400
    now_iso = datetime.utcnow().isoformat(); total = 0
    try:
        _types = {a['zoho_account_id']: a['account_type'] for a in adapter.fetch_expense_accounts(creds)}
    except Exception as e:
        return jsonify({'error': f'Zoho sync failed: {str(e)[:250]}'}), 502
    import re as _re
    detail = []
    for acc in tracked:
        is_liab = 'liability' in (_types.get(acc['zoho_account_id']) or '')
        try:
            txns = adapter.fetch_account_transactions(creds, acc['zoho_account_id'])
        except Exception as e:
            return jsonify({'error': f"Zoho sync failed for {acc.get('account_name')}: {str(e)[:250]}"}), 502
        rows = {}
        for i, t in enumerate(txns):
            date = (t.get('transaction_date') or '')[:10] or None
            if is_liab:
                # per-employee "<Name> - Salary Payable" account: a DEBIT is a
                # payment to that employee (credits are accruals, not paid).
                amt = float(t.get('debit_amount') or 0)
                emp = _re.sub(r'(?i)[\s\-–]*(salary|salaries|payable|payroll|wages?)\b[\s\-–]*', ' ', acc.get('account_name') or '').strip(' -–') or (acc.get('account_name') or 'Unspecified')
            else:
                amt = float(t.get('debit_amount') or 0) - float(t.get('credit_amount') or 0)
                emp = (t.get('payee') or '').strip() or (t.get('description') or '').strip()[:80] or 'Unspecified'
            if not date or amt == 0: continue
            key = f"{acc['zoho_account_id']}:{t.get('transaction_id') or t.get('entry_number') or i}:{date}:{i}"
            rows[key] = {'company_id': cid, 'zoho_account_id': acc['zoho_account_id'], 'txn_key': key,
                         'pay_date': date, 'month': date[:7], 'employee': emp, 'amount': amt,
                         'description': (t.get('description') or '')[:300]}
        # replace this account's rows so deleted/edited Zoho entries don't linger
        sb.table('salary_payments').delete().eq('company_id', cid).eq('zoho_account_id', acc['zoho_account_id']).execute()
        batch = list(rows.values())
        for j in range(0, len(batch), 500):
            sb.table('salary_payments').insert(batch[j:j+500]).execute()
        total += len(batch)
        detail.append({'account': acc.get('account_name'), 'type': _types.get(acc['zoho_account_id']) or 'unknown (not in expense/liability list)',
                       'fetched': len(txns), 'kept': len(batch), 'sample_keys': sorted(list(txns[0].keys()))[:25] if txns else []})
        sb.table('salary_accounts').update({'last_synced_at': now_iso}).eq('id', acc['id']).execute()
    return jsonify({'synced': total, 'detail': detail})

# ── CFO summary (mobile Insights "CFO" page) ───────────────────────────────────
def _cfo_period(pl):
    g = lambda k: (pl.get(k) or {}).get('total', 0.0)
    revenue = g('operating income')
    cogs = g('cost of goods sold')
    gross = pl['gross profit']['total'] if 'gross profit' in pl else revenue - cogs
    opex = g('operating expense')
    op_profit = pl['operating profit']['total'] if 'operating profit' in pl else gross - opex
    net = None
    for k in pl:
        if k.startswith('net profit'): net = pl[k]['total']
    if net is None: net = op_profit + g('non operating income') - g('non operating expense')
    return {'revenue': revenue, 'cogs': cogs, 'gross': gross, 'opex': opex, 'op_profit': op_profit, 'net': net}

@app.route('/api/integrations/sync-cfo', methods=['POST'])
def sync_cfo():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    cid = claims['company_id']
    creds = _get_creds_for(cid, 'zoho'); adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected.'}), 400
    from concurrent.futures import ThreadPoolExecutor
    import calendar
    today = date.today()
    def month_range(y, m):
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    months = []
    y, m = today.year, today.month
    for _ in range(4):
        months.append((y, m)); m -= 1
        if m == 0: y, m = y - 1, 12
    months.reverse()
    jobs = {('m', f'{yy}-{mm:02d}'): month_range(yy, mm) for yy, mm in months}
    jobs[('ytd', 'cur')] = (date(today.year, 1, 1), today)
    try: prev_end = today.replace(year=today.year - 1)
    except ValueError: prev_end = today.replace(year=today.year - 1, day=28)
    jobs[('ytd', 'prev')] = (date(today.year - 1, 1, 1), prev_end)
    # Zoho rate-limits per minute (HTTP 429), so: sequential calls with a small
    # gap, one retry after a pause, and closed past months are reused from the
    # previous snapshot (only YTD, prior-year YTD and the current + last month
    # are re-fetched on later syncs).
    import time as _t
    prev_snap = (sb.table('cfo_snapshots').select('payload').eq('company_id', cid).execute().data or [{}])[0].get('payload') or {}
    cached_months = {m_['month']: m_ for m_ in (prev_snap.get('months') or [])}
    last_two = {f'{yy}-{mm:02d}' for yy, mm in months[-2:]}
    results = {}
    try:
        for key, (a, b) in jobs.items():
            if key[0] == 'm' and key[1] in cached_months and key[1] not in last_two:
                continue
            for attempt in (0, 1):
                try:
                    results[key] = adapter.fetch_profit_and_loss(creds, a.isoformat(), b.isoformat())
                    break
                except RuntimeError as e:
                    if '429' in str(e) and attempt == 0:
                        _t.sleep(8); continue
                    raise
            _t.sleep(1.2)
    except Exception as e:
        msg = str(e)[:300]
        if '429' in msg:
            msg = 'Zoho is rate-limiting requests (too many per minute). Wait 5 minutes and click Sync again - progress is not lost.'
        return jsonify({'error': f'Zoho P&L fetch failed: {msg}'}), 502
    cur = results[('ytd', 'cur')]
    if not cur:
        return jsonify({'error': 'Zoho returned an empty Profit & Loss report.'}), 502
    exp_lines = sorted(((n, a) for n, a in (cur.get('operating expense') or {}).get('lines', []) if a), key=lambda x: -abs(x[1]))
    payload = {
        'as_of': today.isoformat(),
        'ytd': {**_cfo_period(cur), 'expense_lines': [{'name': n, 'amount': a} for n, a in exp_lines[:8]]},
        'prev_ytd': _cfo_period(results[('ytd', 'prev')]),
        'months': [(cached_months[k[1]] if (k[0] == 'm' and k not in results) else {'month': k[1], **_cfo_period(results[k])}) for k in sorted(jobs) if k[0] == 'm'],
    }
    # Cash: bank + cash accounts' balances from Zoho Banking
    try:
        banks = adapter.fetch_bank_accounts(creds)
        payload['cash_balance'] = sum(float(b.get('balance') or 0) for b in banks
                                      if (b.get('account_type') or '') in ('bank', 'cash') and b.get('is_active', True))
    except Exception:
        payload['cash_balance'] = None
    # AR / AP from already-synced invoices and bills (non-draft, open balance)
    try:
        inv = sb.table('zoho_invoices').select('balance,status').eq('company_id', cid).execute().data or []
        payload['receivables'] = sum(float(r.get('balance') or 0) for r in inv if (r.get('status') or '').lower() != 'draft' and float(r.get('balance') or 0) > 0)
        bills = sb.table('zoho_bills').select('balance').eq('company_id', cid).execute().data or []
        payload['payables'] = sum(float(r.get('balance') or 0) for r in bills if float(r.get('balance') or 0) > 0)
    except Exception:
        payload['receivables'] = payload['payables'] = None
    days = max(1, (today - date(today.year, 1, 1)).days + 1)
    rev, cg = payload['ytd']['revenue'], payload['ytd']['cogs']
    payload['dso'] = round(payload['receivables'] / rev * days) if rev and payload.get('receivables') is not None else None
    payload['dpo'] = round(payload['payables'] / cg * days) if cg and payload.get('payables') is not None else None
    sb.table('cfo_snapshots').upsert({'company_id': cid, 'payload': payload, 'synced_at': datetime.utcnow().isoformat()}, on_conflict='company_id').execute()
    return jsonify({'ok': True, 'sections_found': sorted(cur.keys()), 'ytd': payload['ytd']})

# ── Loan transaction anomaly review ─────────────────────────────────────────
# What the admin actually worries about: "is loan money accidentally showing
# up as revenue?" This can't be answered with certainty from the API alone
# (that would require understanding intent), so this flags CANDIDATES for a
# human to check rather than asserting a verdict -- same principle as the
# quote-invoice match suggestions. Two checks:
#   1. On each tracked loan account's own ledger: a transaction_type that's
#      normally a sales-side concept (invoice, customer_payment, credit
#      notes...) has no business posting directly to a loan liability
#      account, so it's flagged as worth checking.
#   2. On the company's Income-type accounts: a large entry whose amount
#      closely matches (within 2%) a real credit (borrowing) recorded on a
#      tracked loan account is flagged as a likely candidate for "this may
#      be loan proceeds booked as income instead."
_LOAN_ACCOUNT_SUSPECT_TYPES = {
    'invoice', 'customer_payment', 'sales_without_invoices',
    'credit_notes', 'creditnote_refund', 'tax_refund', 'refund',
}
_INCOME_ACCOUNT_SUSPECT_TYPES = {
    'deposit', 'journal', 'other_income', 'sales_without_invoices',
    'base_currency_adjustment', 'owner_contribution',
}
_INCOME_REVIEW_MIN_AMOUNT = 5000  # ignore small, routine income lines

@app.route('/api/integrations/loan-accounts/review', methods=['POST'])
def review_loan_transactions():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    company_id = claims['company_id']

    tracked = sb.table('loan_accounts').select('*').eq('company_id', company_id).eq('is_tracked', True).execute().data or []
    if not tracked:
        return jsonify({'error': 'No loan accounts selected yet — pick your loan accounts above first.'}), 400

    creds = _get_creds_for(company_id, 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected. Connect it in Settings → Integrations first.'}), 400

    findings = []
    loan_credits = []  # every borrowing (credit) entry across tracked loan accounts, for amount cross-matching
    earliest_date = None

    for acc in tracked:
        try:
            txns = adapter.fetch_account_transactions(creds, acc['zoho_account_id'])
        except Exception as e:
            findings.append({'severity': 'error', 'account': acc['account_name'],
                              'message': f"Could not read transactions for this account: {str(e)[:200]}"})
            continue
        for t in txns:
            date = t.get('transaction_date')
            if date and (earliest_date is None or date < earliest_date):
                earliest_date = date
            ttype = (t.get('transaction_type') or '').lower()
            debit_amt = float(t.get('debit_amount') or 0)
            credit_amt = float(t.get('credit_amount') or 0)
            if credit_amt > 0:
                loan_credits.append({'amount': credit_amt, 'date': date, 'account': acc['account_name']})
            if ttype in _LOAN_ACCOUNT_SUSPECT_TYPES:
                findings.append({
                    'severity': 'warning', 'account': acc['account_name'],
                    'message': f"A '{ttype.replace('_', ' ')}' transaction of AED {(debit_amt or credit_amt):,.2f} on {date or 'an unknown date'} is posted directly to this loan account. Loan accounts are normally only touched by deposits, journals, expenses, or loan/bill payments — a sales-type entry here is worth checking in Zoho.",
                })

    # Cross-check Income accounts, bounded to since the earliest loan
    # transaction (or 3 years back if that's unknown) so this stays a single
    # bounded request rather than scanning the company's entire history.
    date_start = earliest_date or (datetime.utcnow().replace(year=datetime.utcnow().year - 3)).strftime('%Y-%m-%d')
    try:
        income_accounts = [a for a in adapter.fetch_income_accounts(creds) if a.get('is_involved_in_transaction')][:15]
    except Exception as e:
        income_accounts = []
        findings.append({'severity': 'error', 'account': '(Income accounts)',
                          'message': f"Could not read Income accounts: {str(e)[:200]}"})

    for acc in income_accounts:
        try:
            txns = adapter.fetch_account_transactions(creds, acc['zoho_account_id'], extra_params={'date_start': date_start})
        except Exception:
            continue
        for t in txns:
            credit_amt = float(t.get('credit_amount') or 0)
            if credit_amt < _INCOME_REVIEW_MIN_AMOUNT:
                continue
            ttype = (t.get('transaction_type') or '').lower()
            date = t.get('transaction_date')
            matched = next((l for l in loan_credits if l['amount'] > 0 and abs(l['amount'] - credit_amt) / l['amount'] <= 0.02), None)
            if matched:
                findings.append({
                    'severity': 'high', 'account': acc['account_name'],
                    'message': f"AED {credit_amt:,.2f} was recorded as income here on {date or 'an unknown date'} ('{ttype.replace('_', ' ')}') — this closely matches a borrowing amount on '{matched['account']}' ({matched['date']}). This may be loan proceeds booked as income instead of to the loan account — verify in Zoho Books.",
                })
            elif ttype in _INCOME_ACCOUNT_SUSPECT_TYPES:
                findings.append({
                    'severity': 'info', 'account': acc['account_name'],
                    'message': f"A large, manually-entered-looking transaction of AED {credit_amt:,.2f} ('{ttype.replace('_', ' ')}') was recorded here on {date or 'an unknown date'}. Worth a quick look to confirm it's genuine income rather than a loan or other one-off receipt.",
                })

    severity_rank = {'high': 0, 'warning': 1, 'error': 2, 'info': 3}
    findings.sort(key=lambda f: severity_rank.get(f['severity'], 9))
    return jsonify({
        'reviewed_loan_accounts': len(tracked),
        'reviewed_income_accounts': len(income_accounts),
        'date_range_from': date_start,
        'findings': findings,
    })

# ── Bank Reconciliation tracker ─────────────────────────────────────────────
# "Is every bank transaction reconciled in Zoho, every month?" -- tracks the
# reconciliation runs the admin already does in Zoho (Banking -> Reconcile
# Account) whenever a bank statement comes in, and surfaces any month with
# no completed reconciliation as a visible gap rather than silently omitting
# it. See migration-add-bank-reconciliation.sql and
# ZohoAdapter.fetch_bank_accounts/fetch_bank_reconciliations above.
def _month_start(d):
    return date(d.year, d.month, 1)

def _months_back(n):
    """Last n calendar months INCLUDING the current one, oldest first."""
    today = _month_start(datetime.utcnow().date())
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))

def _add_month(m):
    ny, nm = (m.year + 1, 1) if m.month == 12 else (m.year, m.month + 1)
    return date(ny, nm, 1)

def _sub_months(m, n):
    y, mo = m.year, m.month - n
    while mo <= 0:
        mo += 12
        y -= 1
    return date(y, mo, 1)

def _month_range(start, end):
    """Inclusive list of first-of-month dates from start to end, oldest first."""
    out = []
    m = start
    while m <= end:
        out.append(m)
        m = _add_month(m)
    return out

# How far back the reconciliation grid is allowed to go when real history
# supports it -- "all months and years" in practice, capped so one very old
# account can't blow the table up to hundreds of rows. 8 years covers every
# realistic case for this app's customers.
MAX_RECON_MONTHS = 96

def _parse_zoho_date(s):
    if not s: return None
    try:
        return datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
    except Exception:
        return None

# Zoho's public docs don't show a full response example for a reconciliation
# record, so status is read defensively from whichever of these keys is
# present; a record is only ever treated as a completed reconciliation when
# one of these fields is explicitly present and does NOT say "draft" --
# never assumed reconciled by default.
def _recon_is_completed(r):
    for key in ('status', 'reconciliation_status', 'save_option'):
        v = (r.get(key) or '').lower()
        if v:
            return 'draft' not in v
    return False

@app.route('/api/integrations/sync-bank-reconciliation', methods=['POST'])
def sync_bank_reconciliation():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    company_id = claims['company_id']

    creds = _get_creds_for(company_id, 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected. Connect it in Settings → Integrations first.'}), 400

    try:
        accounts = adapter.fetch_bank_accounts(creds)
    except Exception as e:
        return jsonify({'error': f'Could not fetch bank accounts: {str(e)[:300]}'}), 502

    now_iso = datetime.utcnow().isoformat()
    current_month = _month_start(datetime.utcnow().date())
    active_accounts = [a for a in accounts if a.get('is_active', True)]

    # Pass 1: pull each account's full reconciliation history (fetch_bank_reconciliations
    # already paginates through everything Zoho has) and find how far back real
    # data goes, so the grid covers "all months and years" rather than a guessed window.
    recons_by_account = {}
    earliest_month = None
    for acc in active_accounts:
        acc_id = acc['zoho_account_id']
        try:
            recons = adapter.fetch_bank_reconciliations(creds, acc_id)
        except Exception:
            recons = []  # best-effort: still record the snapshot below even if history can't be read
        recons_by_account[acc_id] = recons
        for r in recons:
            start = _parse_zoho_date(r.get('start_date'))
            if start:
                m = _month_start(start)
                if earliest_month is None or m < earliest_month:
                    earliest_month = m

    if earliest_month is None:
        # No reconciliation records found anywhere in Zoho for this company --
        # nothing tells us how far back to go, so fall back to a recent window
        # instead of fabricating years of "unreconciled" history.
        earliest_month = _months_back(12)[0]
    else:
        cap = _sub_months(current_month, MAX_RECON_MONTHS - 1)
        if earliest_month < cap:
            earliest_month = cap

    months = _month_range(earliest_month, current_month)
    synced_accounts, synced_months = 0, 0

    # Pass 2: snapshot + per-month reconciled/gap rows across the full range above.
    for acc in active_accounts:
        acc_id = acc['zoho_account_id']
        sb.table('bank_account_snapshot').upsert({
            'company_id': company_id, 'zoho_account_id': acc_id,
            'account_name': acc['account_name'], 'account_type': acc['account_type'],
            'bank_name': acc['bank_name'],
            'balance': acc['balance'],
            # Only a meaningful comparison figure when bank feeds are
            # actually active -- otherwise Zoho's 0 would falsely look like
            # a perfect match against the books balance.
            'bank_balance': acc['bank_balance'] if acc['is_feeds_active'] else None,
            'uncategorized_transactions': acc['uncategorized_transactions'],
            'last_synced_at': now_iso,
        }, on_conflict='company_id,zoho_account_id').execute()
        synced_accounts += 1

        reconciled_months = {}  # month_start(date) -> (closing_balance, reconciled_at)
        for r in recons_by_account.get(acc_id, []):
            if not _recon_is_completed(r):
                continue
            start = _parse_zoho_date(r.get('start_date'))
            end = _parse_zoho_date(r.get('end_date')) or start
            if not start:
                continue
            m = _month_start(start)
            end_m = _month_start(end)
            while m <= end_m:
                reconciled_months[m] = (r.get('closing_balance'), r.get('reconciled_at') or r.get('created_time'))
                m = _add_month(m)

        for month in months:
            closing_balance, reconciled_at = reconciled_months.get(month, (None, None))
            sb.table('bank_reconciliation_months').upsert({
                'company_id': company_id, 'zoho_account_id': acc_id, 'account_name': acc['account_name'],
                'month': month.isoformat(), 'is_reconciled': month in reconciled_months,
                'closing_balance': closing_balance, 'reconciled_at': reconciled_at,
                'last_synced_at': now_iso,
            }, on_conflict='company_id,zoho_account_id,month').execute()
            synced_months += 1

    return jsonify({
        'synced_accounts': synced_accounts, 'synced_months': synced_months,
        'earliest_month': earliest_month.isoformat(), 'months_covered': len(months),
    })

@app.route('/api/integrations/bank-reconciliation', methods=['GET'])
def get_bank_reconciliation():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReconciliation'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    company_id = claims['company_id']
    accounts = sb.table('bank_account_snapshot').select('*').eq('company_id', company_id).order('account_name').execute().data or []
    months = sb.table('bank_reconciliation_months').select('*').eq('company_id', company_id).order('month').execute().data or []
    return jsonify({'accounts': accounts, 'months': months})

# ── Bank vs. Zoho Transaction Report (Zoho -> Report) ───────────────────────
# Both sides come straight from Zoho -- no upload step. Confirmed directly
# with the customer: they import the bank statement into Zoho's own Banking
# module every month (Banking -> Import Statement), so Zoho's own
# /banktransactions already holds the independent bank-side record, each
# line carrying Zoho's own status (uncategorized / matched / categorized /
# excluded / manually_added) showing whether it's linked to a books entry.
# QTcal doesn't need to build a matching engine on top of that -- Zoho
# already did the matching when the statement was imported and reconciled.
# The "Zoho side" is the same Chart-of-Accounts ledger already used for
# Loans, each transaction carrying its own reconcile_status from Zoho's
# monthly Reconcile Account process.
_BANK_TXN_FLAG_STATUSES = {'uncategorized', 'excluded'}  # not linked to a books entry -- the discrepancies to surface

def _is_reconciled_value(v):
    # Zoho's public docs don't show example values for reconcile_status, so
    # this is read defensively -- only ever treated as reconciled when it
    # explicitly says so, never assumed by a missing/unexpected value.
    if v is None: return False
    if isinstance(v, bool): return v
    return str(v).strip().lower() in ('reconciled', 'yes', 'true', '1')

@app.route('/api/integrations/bank-vs-zoho-report', methods=['GET'])
def bank_vs_zoho_report():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'zohoReport'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    company_id = claims['company_id']
    account_id = request.args.get('account_id')
    month = request.args.get('month')  # 'YYYY-MM'
    if not account_id or not month:
        return jsonify({'error': 'account_id and month (YYYY-MM) are required'}), 400
    try:
        y, m = int(month[:4]), int(month[5:7])
        period_start = date(y, m, 1)
        period_end = _add_month(period_start) - timedelta(days=1)
    except Exception:
        return jsonify({'error': 'month must be in YYYY-MM format'}), 400

    creds = _get_creds_for(company_id, 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected. Connect it in Settings → Integrations first.'}), 400

    try:
        bank_rows = adapter.fetch_bank_transactions(creds, account_id, period_start.isoformat(), period_end.isoformat())
    except Exception as e:
        return jsonify({'error': f'Could not fetch bank transactions from Zoho: {str(e)[:300]}'}), 502

    def _fetch_ledger(acc_id):
        return adapter.fetch_account_transactions(creds, acc_id, {
            'date_start': period_start.isoformat(), 'date_end': period_end.isoformat(),
        })
    try:
        ledger_rows = _fetch_ledger(account_id)
        ledger_account_id = account_id
    except Exception as e:
        return jsonify({'error': f'Could not fetch Zoho ledger transactions: {str(e)[:300]}'}), 502

    # On some Zoho organizations the same physical bank account has a
    # different account_id in the Banking module vs. Chart of Accounts.
    # If the direct lookup came back empty but the bank side has activity,
    # resolve the Chart-of-Accounts id by matching account name and retry
    # once -- never silently trust an empty result when there's clear
    # evidence (bank transactions exist) that ledger data should too.
    #
    # diag is returned to the caller (not just logged) so that when the
    # resolution still doesn't work, the actual account names Zoho returned
    # are visible in the UI instead of us having to guess a third time blind
    # -- this endpoint has no live Zoho access from the dev sandbox.
    diag = {
        'initial_ledger_account_id': account_id,
        'initial_ledger_count': len(ledger_rows),
        'bank_row_count': len(bank_rows),
        'bank_account_name': bank_rows[0].get('account_name') if bank_rows else None,
        'resolution_attempted': False,
        'chart_accounts_seen': None,
        'matched_account_id': None,
        'resolution_error': None,
        'raw_probe': None,
    }
    # Raw, unprocessed probe against the exact endpoint fetch_account_transactions
    # calls -- shows Zoho's actual response shape (error code/message, or a
    # genuinely empty transactions list) instead of only the parsed-and-empty
    # result, and separately checks whether this account has ANY ledger
    # history at all (no date filter) to tell a date-range problem apart from
    # an account that simply has no chartofaccounts-side postings yet.
    if not ledger_rows:
        probe = {}
        try:
            r1 = adapter._get(creds, '/chartofaccounts/accounttransactions', {
                'account_id': account_id, 'filter_by': 'TransactionType.All',
                'sort_column': 'transaction_date', 'per_page': 5,
                'date_start': period_start.isoformat(), 'date_end': period_end.isoformat(),
            })
            probe['response_keys'] = list(r1.keys())
            probe['code'] = r1.get('code')
            probe['message'] = r1.get('message')
            probe['transactions_in_period'] = len(r1.get('transactions') or [])
        except Exception as e:
            probe['period_probe_error'] = str(e)[:300]
        try:
            r2 = adapter._get(creds, '/chartofaccounts/accounttransactions', {
                'account_id': account_id, 'filter_by': 'TransactionType.All',
                'sort_column': 'transaction_date', 'per_page': 5,
            })
            probe['any_transactions_ever'] = len(r2.get('transactions') or [])
            sample = (r2.get('transactions') or [])[:2]
            probe['sample_dates'] = [t.get('transaction_date') for t in sample]
        except Exception as e:
            probe['no_date_probe_error'] = str(e)[:300]
        diag['raw_probe'] = probe
    if not ledger_rows and bank_rows:
        bank_acc_name = (bank_rows[0].get('account_name') or '').strip().lower()
        if bank_acc_name:
            diag['resolution_attempted'] = True
            try:
                chart_accounts = adapter.fetch_bank_chart_accounts(creds)
                diag['chart_accounts_seen'] = [
                    {'account_id': c['zoho_account_id'], 'account_name': c['account_name'], 'account_type': c['account_type']}
                    for c in chart_accounts
                ]
                match = next((c for c in chart_accounts
                              if c['account_name'].strip().lower() == bank_acc_name
                              and c['zoho_account_id'] != account_id), None)
                if match:
                    diag['matched_account_id'] = match['zoho_account_id']
                    retry_rows = _fetch_ledger(match['zoho_account_id'])
                    diag['retry_ledger_count'] = len(retry_rows)
                    if retry_rows:
                        ledger_rows = retry_rows
                        ledger_account_id = match['zoho_account_id']
            except Exception as e:
                diag['resolution_error'] = str(e)[:300]  # best-effort resolution -- fall through with the empty result rather than failing the whole report

    # Confirmed against a real customer account + Zoho's own official API
    # docs (Sep 2026): /chartofaccounts/accounttransactions reliably returns
    # data for non-bank ledger accounts (e.g. Loans, a liability type) but
    # comes back genuinely empty -- correct scope, correct params, Zoho
    # itself reports "success" -- for bank/cash type accounts, even when
    # Zoho's own Chart-of-Accounts UI report shows real postings for that
    # same account/month. That UI report apparently isn't backed by this
    # public endpoint for bank/cash accounts.
    #
    # Rather than keep treating that as "0 books entries" (which reads as a
    # data problem when it isn't one), fall back to the /banktransactions
    # data we already fetched above: each line already carries Zoho's own
    # status telling us whether it's linked to a books entry or not (see
    # _BANK_TXN_FLAG_STATUSES / fetch_bank_transactions' docstring) -- for a
    # bank/cash account, Zoho's Banking module *is* the authoritative record
    # of what's in the books, so there's no need for a second ledger call at
    # all. Only used when the direct+name-match ledger lookups above still
    # came back empty, so an account/org where the ledger call does work
    # keeps using that (richer) data untouched.
    books_source = 'ledger'
    if not ledger_rows and bank_rows:
        linked = [r for r in bank_rows if (r.get('status') or '').lower() not in _BANK_TXN_FLAG_STATUSES]
        diag['books_derived_from_banking'] = True
        diag['banking_linked_count'] = len(linked)
        if linked:
            ledger_rows = linked
            ledger_account_id = account_id
            books_source = 'banking'

    bank_out = [{
        'transaction_id': r.get('transaction_id'), 'date': r.get('date'),
        'description': r.get('description') or r.get('payee') or '',
        'payee': r.get('payee'), 'amount': r.get('amount'),
        'debit_or_credit': r.get('debit_or_credit'), 'status': r.get('status'),
        'reference_number': r.get('reference_number'),
        'flagged': (r.get('status') or '').lower() in _BANK_TXN_FLAG_STATUSES,
    } for r in bank_rows]
    bank_out.sort(key=lambda b: (not b['flagged'], b['date'] or ''))

    if books_source == 'banking':
        # ledger_rows here are raw /banktransactions rows (already filtered to
        # the "linked" ones above), not chartofaccounts transactions -- map
        # using the bank-side field names instead. There's no separate
        # Zoho reconcile_status for these (that's a chartofaccounts-only
        # concept), so nothing here is flagged -- by construction every row
        # already cleared the same "linked to books" bar as the left column;
        # reconcile_status instead carries Zoho's own Banking status
        # (matched/categorized/manually_added) for visibility.
        ledger_out = [{
            'transaction_id': r.get('transaction_id'), 'date': r.get('date'),
            'description': r.get('description') or r.get('payee') or '',
            'transaction_type': r.get('status'),
            'debit_amount': r.get('amount') if (r.get('debit_or_credit') or '').lower() == 'debit' else None,
            'credit_amount': r.get('amount') if (r.get('debit_or_credit') or '').lower() == 'credit' else None,
            'reference_number': r.get('reference_number'),
            'reconcile_status': r.get('status'),
            'flagged': False,
        } for r in ledger_rows]
    else:
        ledger_out = [{
            'transaction_id': r.get('transaction_id'), 'date': r.get('transaction_date'),
            'description': r.get('description') or r.get('payee') or '',
            'transaction_type': r.get('transaction_type'),
            'debit_amount': r.get('debit_amount'), 'credit_amount': r.get('credit_amount'),
            'reference_number': r.get('reference_number'),
            'reconcile_status': r.get('reconcile_status'),
            'flagged': not _is_reconciled_value(r.get('reconcile_status')),
        } for r in ledger_rows]
    ledger_out.sort(key=lambda l: (not l['flagged'], l['date'] or ''))

    return jsonify({
        'account_id': account_id, 'ledger_account_id': ledger_account_id, 'month': month,
        'period_start': period_start.isoformat(), 'period_end': period_end.isoformat(),
        'bank_transactions': bank_out, 'ledger_transactions': ledger_out,
        'bank_flagged_count': sum(1 for b in bank_out if b['flagged']),
        'ledger_flagged_count': sum(1 for l in ledger_out if l['flagged']),
        'books_source': books_source,
        'diagnostics': diag,
    })

# ── Push a single locally-created customer to Zoho Books ────────────────────────
# Used by the Deals module (api/quotes.py -> index.html): when a deal is created
# with a brand-new customer name, the frontend creates the local customers row
# first (POST /api/customers, unchanged), then calls this to replicate it into
# Zoho as a Contact and store the returned id back onto that row -- mirroring
# the existing pull-direction sync (_sync_contacts) but in the push direction,
# for exactly one record at a time rather than a bulk import. Any authenticated
# user can call this (not admin-only) since it's a side effect of an action
# (creating a deal) that Deals RBAC already gates -- there's nothing extra to
# protect here beyond normal company scoping.
@app.route('/api/integrations/zoho/push-customer', methods=['POST'])
def push_customer_to_zoho():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    cid = (request.json or {}).get('customer_id')
    if not cid: return jsonify({'error': 'customer_id is required'}), 400

    row = sb.table('customers').select('*').eq('id', cid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Customer not found'}), 404
    customer = row.data[0]
    if customer.get('external_contact_id'):
        return jsonify({'ok': True, 'external_contact_id': customer['external_contact_id'], 'already_synced': True})

    creds = _get_creds_for(claims['company_id'], 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        # Not an error -- Zoho Books may simply not be connected for this
        # company. The local customer record is still perfectly usable.
        return jsonify({'ok': True, 'skipped': 'Zoho Books is not connected.'})

    try:
        contact = adapter.create_customer(
            creds, customer.get('name') or '', company_name=customer.get('company_name'),
            email=customer.get('email'), phone=customer.get('phone'),
        )
    except Exception as e:
        # Best-effort: the customer already exists locally regardless of
        # whether this push succeeds, so this is a soft error, not a 500.
        return jsonify({'ok': False, 'error': f'Zoho customer creation failed: {str(e)[:300]}'}), 502

    sb.table('customers').update({
        'external_contact_id': contact['contact_id'], 'source': 'zoho',
    }).eq('id', cid).eq('company_id', claims['company_id']).execute()
    return jsonify({'ok': True, 'external_contact_id': contact['contact_id']})

# ── Create customer, replicated to the accounting system FIRST ─────────────────
# Used by the deal / quote customer picker. Order: (1) search the connected
# system live -> link if it already exists, (2) otherwise create it there,
# (3) only then write the local Customer Master row. If the external system
# fails, nothing is saved locally and the real error is returned so the user
# can retry. If no system is connected, a local-only customer is created.
@app.route('/api/integrations/create-customer', methods=['POST'])
def create_customer_linked():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json or {}
    name = str(d.get('name') or '').strip()[:200]
    email = str(d.get('email') or '').strip()[:200]
    phone = str(d.get('phone') or '').strip()[:50]
    trn = str(d.get('trn') or '').strip()[:50]
    link_id = str(d.get('link_contact_id') or '').strip()
    force = bool(d.get('force'))
    if not name: return jsonify({'error': 'Customer name is required.'}), 400
    if not email: return jsonify({'error': 'Email is required.'}), 400
    cid_co = claims['company_id']

    # Local exact-name duplicate -> reuse (and push it if not yet linked)
    local = sb.table('customers').select('*').eq('company_id', cid_co).ilike('name', name.replace('%', '').replace('_', r'\_')).execute()
    local_row = next((r for r in (local.data or []) if (r.get('name') or '').strip().lower() == name.lower()), None)
    if local_row and local_row.get('external_contact_id'):
        return jsonify({'customer': local_row, 'existing': True})

    provider = None
    pref = _company_provider(cid_co, 'customer_sync_provider')
    connected = _connected_providers(cid_co)
    for p in ([pref] if pref else []) + connected:
        if p in connected and hasattr(ADAPTERS.get(p), 'create_customer'):
            provider = p; break

    ext_id, ext_name = None, None
    if provider:
        adapter = ADAPTERS[provider]
        creds = _get_creds_for(cid_co, provider)
        label = PROVIDER_LABELS.get(provider, provider)
        try:
            if link_id:
                ext_id = link_id
            else:
                found = adapter.find_customers(creds, name) if hasattr(adapter, 'find_customers') else []
                exact = next((f for f in found if f['name'].strip().lower() == name.lower()
                              or f['company'].strip().lower() == name.lower()), None)
                if exact:
                    ext_id, ext_name = exact['id'], exact['name']
                elif found and not force:
                    return jsonify({'needs_confirm': True, 'provider': provider,
                                    'message': f'Similar customers already exist in {label}.',
                                    'matches': found[:8]}), 409
                else:
                    contact = adapter.create_customer(creds, name, company_name=name, email=email,
                                                      phone=phone, trn=trn)
                    ext_id = contact['contact_id']
        except Exception as e:
            return jsonify({'error': f'{label} customer creation failed: {str(e)[:300]}. Nothing was saved — please retry.'}), 502

    fields = {'name': name, 'company_name': name, 'email': email, 'phone': phone, 'trn': trn}
    if ext_id:
        fields.update({'external_contact_id': ext_id, 'source': provider})
    try:
        if local_row:
            row = sb.table('customers').update(fields).eq('id', local_row['id']).execute()
        else:
            # A previous sync may already have inserted this contact id
            dup = sb.table('customers').select('id').eq('company_id', cid_co).eq('external_contact_id', ext_id).execute() if ext_id else None
            if dup and dup.data:
                row = sb.table('customers').update(fields).eq('id', dup.data[0]['id']).execute()
            else:
                fields.update({'company_id': cid_co, 'created_by': claims['user_id']})
                if not ext_id: fields['source'] = 'manual'
                row = sb.table('customers').insert(fields).execute()
    except Exception as e:
        return jsonify({'error': f'Created in Zoho but could not save locally: {str(e)[:250]}. Run Sync Customers.'}), 500
    return jsonify({'customer': row.data[0], 'linked_existing': bool(ext_id and ext_name),
                    'synced': bool(ext_id), 'provider': provider}), 201

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

# ── Line-item pricing (mirrors index.html's calcItem() / api/proposal.py's
# calc_item() -- duplicated here per this project's established pattern:
# Vercel's Python builder doesn't bundle sibling modules, so every file
# needing quote-line math carries its own copy) ─────────────────────────────
def _num(v, d=0):
    try: return float(v or 0)
    except Exception: return d

def _calc_item_sell(it, pricing_type):
    cad = _num(it.get('cost')) * (1 - _num(it.get('disc'))/100.0) * (1 - _num(it.get('discAdd'))/100.0)
    m = _num(it.get('margin'))
    if pricing_type == 'margin':
        mm = min(0.95, max(0.0, m))
        up = int(cad / (1 - mm) + 0.5) if mm < 1 else 0
    else:
        up = int(cad * (1 + m) + 0.5)
    return up

def _estimate_line_items(option, pricing_type):
    """The won option's items, flattened across sections (Zoho estimates
    have no section-grouping equivalent) -- name/description is the item's
    own description exactly as QTcal has it. Brand/model ride along
    separately for ZohoAdapter.create_estimate() to place into the org's
    Brand/Model No custom fields when it can find them."""
    vat_on = bool(option.get('vatEnabled'))
    vat_rate = _num(option.get('vatRate'), 5) if vat_on else None
    items = []
    for s in (option.get('sections') or []):
        for it in (s.get('items') or []):
            items.append({
                'description': it.get('desc') or '',
                'rate': _calc_item_sell(it, pricing_type),
                'quantity': _num(it.get('qty'), 1) or 1,
                'tax_rate': vat_rate,
                'brand': it.get('brand') or None,
                'model': it.get('model') or None,
            })
    return items

def _estimate_totals(option, pricing_type):
    """Mirrors index.html's calcOpt()/calcItem() exactly: per-line GP is
    (sell - cost-after-discounts) x qty, subtotal is sell x qty summed, and
    margin is GP/subtotal -- same figures shown in the app's own totals bar
    ("Total GP", "Margin") that Nish wants reflected on the Zoho Estimate's
    header-level Profit Margin fields. Computed separately from
    _estimate_line_items() since those feed per-line custom fields while
    this feeds header-level ones -- both read the same won option."""
    ts = tc = 0.0
    for s in (option.get('sections') or []):
        for it in (s.get('items') or []):
            cad = _num(it.get('cost')) * (1 - _num(it.get('disc'))/100.0) * (1 - _num(it.get('discAdd'))/100.0)
            qty = _num(it.get('qty'), 1) or 1
            up = _calc_item_sell(it, pricing_type)
            ts += up * qty
            tc += cad * qty
    total_gp = ts - tc
    margin_pct = (total_gp / ts) if ts > 0 else 0.0
    return total_gp, margin_pct

# ── Create a Zoho Books project (+ Estimate) from an app project ────────────
# One click creates both: the Zoho Project (unchanged from before) and a
# Zoho Estimate replicating the won option of the originating quote. The two
# are independent Zoho objects with no native link between them -- both
# reference the same customer_id so they sit side by side in Zoho.
# Idempotent per object: a second click never re-creates a project or
# estimate that already exists, so a partial failure (e.g. project created,
# estimate failed) can just be retried and only fills in what's missing.
@app.route('/api/integrations/zoho/create-project', methods=['POST'])
def zoho_create_project():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'projectPerformance'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    if not (claims.get('features') or {}).get('project_performance'):
        return jsonify({'error': 'Feature not enabled'}), 403
    pid = (request.json or {}).get('project_id')
    if not pid: return jsonify({'error': 'project_id is required'}), 400
    row = sb.table('projects').select('*').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Project not found'}), 404
    proj = row.data[0]
    if proj.get('zoho_project_id') and proj.get('zoho_estimate_id'):
        return jsonify({'error': 'Already linked to Zoho', 'zoho_project_id': proj['zoho_project_id'],
                         'zoho_estimate_id': proj['zoho_estimate_id']}), 409
    creds = _get_creds_for(claims['company_id'], 'zoho')
    adapter = ADAPTERS.get('zoho')
    if not creds or not adapter or not adapter.is_configured(creds):
        return jsonify({'error': 'Zoho Books is not connected. Connect it in Integrations first.'}), 400
    customer_name = (proj.get('customer') or '').strip()
    if not customer_name:
        return jsonify({'error': 'Project has no customer set. Add a customer first.'}), 400
    _cn = customer_name.replace('%', '').replace('_', r'\_')
    match = sb.table('customers').select('external_contact_id,name').eq('company_id', claims['company_id']).ilike('name', _cn).execute()
    _linked = [m for m in (match.data or []) if m.get('external_contact_id')]
    if not _linked:  # looser fallback: contains-match, still requiring a Zoho link
        loose = sb.table('customers').select('external_contact_id,name').eq('company_id', claims['company_id']).ilike('name', f'%{_cn}%').execute()
        _linked = [m for m in (loose.data or []) if m.get('external_contact_id')]
    if not _linked:
        return jsonify({'error': f'Customer "{customer_name}" is not linked to Zoho. Re-select it via the Deal/Quote customer picker (+ Add new customer) or run Sync Customers.'}), 400
    zoho_customer_id = _linked[0]['external_contact_id']

    zoho_project_id = proj.get('zoho_project_id')
    zoho_project_no = proj.get('zoho_project_no')

    if not zoho_project_id:
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
        # Cost Budget = what the baseline estimated we'd spend; Revenue Budget =
        # the contract value (same figure as `rate`).
        cost_budget = proj.get('original_estimated_cost')
        try:
            zoho_proj = adapter.create_project(creds, proj['name'], zoho_customer_id,
                                                rate=rate, cost_budget=cost_budget, revenue_budget=rate)
        except Exception as e:
            return jsonify({'error': f'Zoho project creation failed: {str(e)[:400]}'}), 502
        # zoho_project_id is Zoho's internal id (required for every subsequent
        # Zoho API call -- POs/bills/expenses/invoices are all fetched by it).
        # zoho_project_no is the human-readable number staff actually use,
        # stored in Zoho as the cf_project_no custom field. It may not be set
        # yet at creation time (it's often filled in inside Zoho afterward) --
        # that's fine, a later sync/import picks it up once it exists.
        zoho_project_id = zoho_proj['project_id']
        zoho_project_no = zoho_proj.get('cf_project_no') or None
        sb.table('projects').update({
            'zoho_project_id': zoho_project_id, 'zoho_project_no': zoho_project_no,
        }).eq('id', pid).eq('company_id', claims['company_id']).execute()
        if proj.get('quotation_id'):
            sb.table('quotes').update({
                'zoho_project_id': zoho_project_id, 'zoho_project_no': zoho_project_no,
            }).eq('id', proj['quotation_id']).eq('company_id', claims['company_id']).execute()

    # Estimate creation is best-effort and never rolls back the project
    # creation above -- a Zoho hiccup here (e.g. no matching tax rate) still
    # leaves you with a usable, linked project. The error (if any) comes
    # back in the response for the frontend to surface, not swallowed.
    zoho_estimate_id = proj.get('zoho_estimate_id')
    estimate_error = None
    if not zoho_estimate_id:
        quote = None
        if proj.get('quotation_id'):
            qrow = sb.table('quotes').select('quote_data,pricing_type,quote_ref').eq('id', proj['quotation_id']).eq('company_id', claims['company_id']).execute()
            quote = qrow.data[0] if qrow.data else None
        if not quote:
            estimate_error = 'No originating quote found for this project -- estimate not created.'
        else:
            options = quote.get('quote_data') or []
            won_idx = proj.get('won_option_index')
            won_idx = won_idx if isinstance(won_idx, int) and 0 <= won_idx < len(options) else 0
            option = options[won_idx] if options else None
            if not option:
                estimate_error = 'The originating quote has no line items -- estimate not created.'
            else:
                pricing_type = quote.get('pricing_type') or 'markup'
                total_gp, margin_pct = _estimate_totals(option, pricing_type)
                try:
                    zoho_est = adapter.create_estimate(
                        creds, zoho_customer_id,
                        reference_number=quote.get('quote_ref') or '',
                        quote_date=option.get('date') or None,
                        line_items=_estimate_line_items(option, pricing_type),
                        salesperson_name=option.get('by') or None,
                        total_gp=total_gp,
                        margin_pct=margin_pct,
                        zoho_project_id=zoho_project_id,
                    )
                    zoho_estimate_id = zoho_est['estimate_id']
                    sb.table('projects').update({'zoho_estimate_id': zoho_estimate_id}).eq('id', pid).eq('company_id', claims['company_id']).execute()
                    sb.table('quotes').update({'zoho_estimate_id': zoho_estimate_id}).eq('id', proj['quotation_id']).eq('company_id', claims['company_id']).execute()
                except Exception as e:
                    estimate_error = f'Zoho estimate creation failed: {str(e)[:400]}'

    return jsonify({
        'ok': True, 'zoho_project_id': zoho_project_id, 'zoho_project_no': zoho_project_no,
        'zoho_estimate_id': zoho_estimate_id, 'estimate_error': estimate_error,
    })

# Note: a live "/api/integrations/search-customers" route used to live here,
# searching the connected provider (e.g. Zoho) directly on every keystroke.
# It's no longer called from anywhere in the frontend — customer search during
# quote creation now searches the local, synced `customers` table instead
# (see api/customers.py), which is faster and works even when nothing is
# connected. Removed as dead code.

if __name__ == '__main__':
    app.run(debug=True)
