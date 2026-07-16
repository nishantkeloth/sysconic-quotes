from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import os, jwt, json, time, traceback
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
from supabase import create_client

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')
CRON_SECRET  = os.environ.get('CRON_SECRET')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    return jsonify({'error': str(e)}), 500

def verify_token(req):
    auth = req.headers.get('Authorization', '')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except Exception:
        return None

def has_feature(claims, feature):
    return bool((claims.get('features') or {}).get(feature))

def is_admin(claims):
    return (claims or {}).get('role') == 'admin'

def can_manage(claims):
    return is_admin(claims) or bool(claims.get('can_manage_project_performance'))

def _num(v, d=0.0):
    try:
        return float(v) if v not in (None, '') else d
    except (TypeError, ValueError):
        return d

# ══ Zoho Books adapter (sync-relevant subset) ═══════════════════════════════
# Duplicated from api/integrations.py's ZohoAdapter — Vercel's Python builder
# packages one file per route, no sibling imports (see the note there). Only
# the pieces this file needs: token refresh + the five actuals-fetch methods.
# Keep in sync by hand if the adapter in api/integrations.py changes shape.
ZOHO_ACCOUNTS = 'https://accounts.zoho.com'
ZOHO_API      = 'https://www.zohoapis.com/books/v3'

class _ZohoSync:
    REQUIRED_FIELDS = ['client_id', 'client_secret', 'refresh_token', 'org_id']

    def __init__(self):
        self._tok_cache = {}

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

    def _fetch_paginated(self, creds, path, params, list_key, max_pages=25):
        out, page = [], 1
        while page <= max_pages:
            p = dict(params or {})
            p.update({'per_page': 200, 'page': page})
            data = self._get(creds, path, p)
            out.extend(data.get(list_key, []) or [])
            if not (data.get('page_context') or {}).get('has_more_page'):
                break
            page += 1
        return out

    def fetch_purchase_orders(self, creds, zoho_project_id):
        rows = self._fetch_paginated(creds, '/purchaseorders', {'project_id': zoho_project_id}, 'purchaseorders')
        pos, lines = [], []
        for r in rows:
            po_id = r.get('purchaseorder_id')
            if not po_id: continue
            pos.append({
                'zoho_purchase_order_id': po_id, 'zoho_project_id': zoho_project_id,
                'po_number': r.get('purchaseorder_number'), 'po_date': r.get('date'),
                'vendor_name': r.get('vendor_name'), 'status': r.get('status'),
                'total': r.get('total') or 0,
                'billed_total': r.get('total_invoiced_amount') or r.get('billed_amount') or 0,
                'raw': r,
            })
            try:
                detail = self._get(creds, f'/purchaseorders/{po_id}', {})
                for li in (detail.get('purchaseorder') or {}).get('line_items') or []:
                    lines.append({
                        'zoho_purchase_order_id': po_id, 'zoho_purchase_order_line_id': li.get('line_item_id'),
                        'zoho_item_id': li.get('item_id'), 'item_name': li.get('name') or li.get('description'),
                        'quantity': li.get('quantity') or 0, 'rate': li.get('rate') or 0,
                        'total': li.get('item_total') or 0, 'raw': li,
                    })
            except Exception:
                pass
        return pos, lines

    def fetch_bills(self, creds, zoho_project_id):
        rows = self._fetch_paginated(creds, '/bills', {'project_id': zoho_project_id}, 'bills')
        bills, lines = [], []
        for r in rows:
            bill_id = r.get('bill_id')
            if not bill_id: continue
            bills.append({
                'zoho_bill_id': bill_id,
                'zoho_purchase_order_id': r.get('purchaseorder_id') or r.get('purchase_order_id'),
                'bill_number': r.get('bill_number'), 'bill_date': r.get('date'),
                'vendor_name': r.get('vendor_name'), 'status': r.get('status'),
                'total': r.get('total') or 0, 'raw': r,
            })
            try:
                detail = self._get(creds, f'/bills/{bill_id}', {})
                for li in (detail.get('bill') or {}).get('line_items') or []:
                    lines.append({
                        'zoho_bill_id': bill_id, 'zoho_bill_line_id': li.get('line_item_id'),
                        'zoho_item_id': li.get('item_id'), 'item_name': li.get('name') or li.get('description'),
                        'quantity': li.get('quantity') or 0, 'rate': li.get('rate') or 0,
                        'total': li.get('item_total') or 0, 'raw': li,
                    })
            except Exception:
                pass
        return bills, lines

    def fetch_expenses(self, creds, zoho_project_id):
        rows = self._fetch_paginated(creds, '/expenses', {'project_id': zoho_project_id}, 'expenses')
        return [{
            'zoho_expense_id': r.get('expense_id'), 'expense_account': r.get('account_name'),
            'vendor_name': r.get('vendor_name') or r.get('paid_through_account_name'),
            'expense_date': r.get('date'), 'amount': r.get('total') or r.get('amount') or 0,
            'description': r.get('description'), 'raw': r,
        } for r in rows if r.get('expense_id')]

    def fetch_invoices(self, creds, zoho_project_id):
        rows = self._fetch_paginated(creds, '/invoices', {'project_id': zoho_project_id}, 'invoices')
        return [{
            'zoho_invoice_id': r.get('invoice_id'), 'invoice_number': r.get('invoice_number'),
            'invoice_date': r.get('date'), 'due_date': r.get('due_date'), 'status': r.get('status'),
            'total': r.get('total') or 0, 'balance': r.get('balance') or 0, 'raw': r,
        } for r in rows if r.get('invoice_id')]

    def fetch_payments(self, creds, invoice_ids):
        out = []
        for inv_id in (invoice_ids or []):
            try:
                data = self._get(creds, f'/invoices/{inv_id}/payments', {})
            except Exception:
                continue
            for p in data.get('payments', []) or []:
                out.append({
                    'zoho_payment_id': p.get('payment_id'), 'zoho_invoice_id': inv_id,
                    'payment_date': p.get('date'), 'amount': p.get('amount_applied') or p.get('amount') or 0,
                    'raw': p,
                })
        return out

ZOHO = _ZohoSync()

def _get_zoho_creds(company_id):
    row = sb.table('company_integrations').select('credentials,status').eq('company_id', company_id).eq('provider', 'zoho').execute()
    if not row.data or row.data[0].get('status') != 'connected':
        return None
    return row.data[0].get('credentials') or {}

# ── Sync one project's actuals from Zoho into the staging tables ───────────
def _sync_project_actuals(company_id, project):
    zoho_project_id = project.get('zoho_project_id')
    if not zoho_project_id:
        return {'resource': 'all', 'status': 'error', 'records': 0, 'error': 'Project has no linked Zoho Project yet'}

    creds = _get_zoho_creds(company_id)
    if not creds or not ZOHO.is_configured(creds):
        return {'resource': 'all', 'status': 'error', 'records': 0, 'error': 'Zoho Books is not connected for this company'}

    project_id = project['id']
    results = []

    def _log(resource, status, count, err=None):
        sb.table('pp_sync_logs').insert({
            'company_id': company_id, 'project_id': project_id, 'resource': resource,
            'status': status, 'records_synced': count, 'error_detail': (err or '')[:500] or None,
            'finished_at': datetime.now(timezone.utc).isoformat(),
        }).execute()
        results.append({'resource': resource, 'status': status, 'records': count, 'error': err})

    # Purchase Orders (+ lines) — committed cost source
    try:
        pos, po_lines = ZOHO.fetch_purchase_orders(creds, zoho_project_id)
        for po in pos:
            po['company_id'] = company_id; po['project_id'] = project_id
        if pos:
            saved = sb.table('zoho_purchase_orders').upsert(pos, on_conflict='company_id,zoho_purchase_order_id').execute()
            id_by_zoho = {r['zoho_purchase_order_id']: r['id'] for r in (saved.data or [])}
            if po_lines:
                line_rows = []
                for li in po_lines:
                    po_uuid = id_by_zoho.get(li['zoho_purchase_order_id'])
                    if not po_uuid: continue
                    line_rows.append({
                        'company_id': company_id, 'purchase_order_id': po_uuid,
                        'zoho_purchase_order_line_id': li.get('zoho_purchase_order_line_id'),
                        'zoho_item_id': li.get('zoho_item_id'), 'item_name': li.get('item_name'),
                        'quantity': li.get('quantity'), 'rate': li.get('rate'), 'total': li.get('total'),
                        'raw': li.get('raw'),
                    })
                if line_rows:
                    sb.table('zoho_purchase_order_lines').delete().in_('purchase_order_id', list(id_by_zoho.values())).execute()
                    sb.table('zoho_purchase_order_lines').insert(line_rows).execute()
        _log('purchase_orders', 'success', len(pos))
    except Exception as e:
        _log('purchase_orders', 'error', 0, str(e))

    # Bills (+ lines) — actual cost source
    try:
        bills, bill_lines = ZOHO.fetch_bills(creds, zoho_project_id)
        for b in bills:
            b['company_id'] = company_id; b['project_id'] = project_id
        if bills:
            saved = sb.table('zoho_bills').upsert(bills, on_conflict='company_id,zoho_bill_id').execute()
            id_by_zoho = {r['zoho_bill_id']: r['id'] for r in (saved.data or [])}
            if bill_lines:
                line_rows = []
                for li in bill_lines:
                    bill_uuid = id_by_zoho.get(li['zoho_bill_id'])
                    if not bill_uuid: continue
                    line_rows.append({
                        'company_id': company_id, 'bill_id': bill_uuid,
                        'zoho_bill_line_id': li.get('zoho_bill_line_id'), 'zoho_item_id': li.get('zoho_item_id'),
                        'item_name': li.get('item_name'), 'quantity': li.get('quantity'),
                        'rate': li.get('rate'), 'total': li.get('total'), 'raw': li.get('raw'),
                    })
                if line_rows:
                    sb.table('zoho_bill_lines').delete().in_('bill_id', list(id_by_zoho.values())).execute()
                    sb.table('zoho_bill_lines').insert(line_rows).execute()
        _log('bills', 'success', len(bills))
    except Exception as e:
        _log('bills', 'error', 0, str(e))

    # Expenses — actual cost source
    try:
        expenses = ZOHO.fetch_expenses(creds, zoho_project_id)
        for e_ in expenses:
            e_['company_id'] = company_id; e_['project_id'] = project_id
        if expenses:
            sb.table('zoho_expenses').upsert(expenses, on_conflict='company_id,zoho_expense_id').execute()
        _log('expenses', 'success', len(expenses))
    except Exception as e:
        _log('expenses', 'error', 0, str(e))

    # Invoices — billing source
    invoice_ids = []
    try:
        invoices = ZOHO.fetch_invoices(creds, zoho_project_id)
        for inv in invoices:
            inv['company_id'] = company_id; inv['project_id'] = project_id
        if invoices:
            sb.table('zoho_invoices').upsert(invoices, on_conflict='company_id,zoho_invoice_id').execute()
        invoice_ids = [inv['zoho_invoice_id'] for inv in invoices]
        _log('invoices', 'success', len(invoices))
    except Exception as e:
        _log('invoices', 'error', 0, str(e))

    # Payments — collection source (depends on invoice_ids from this run)
    try:
        payments = ZOHO.fetch_payments(creds, invoice_ids)
        for p in payments:
            p['company_id'] = company_id; p['project_id'] = project_id
        if payments:
            sb.table('zoho_payments').upsert(payments, on_conflict='company_id,zoho_payment_id').execute()
        _log('payments', 'success', len(payments))
    except Exception as e:
        _log('payments', 'error', 0, str(e))

    sb.table('projects').update({'last_synced_at': datetime.now(timezone.utc).isoformat()}).eq('id', project_id).eq('company_id', company_id).execute()
    return results


# ══ Calculation engine ═══════════════════════════════════════════════════════
# Duplicated in api/project_performance.py for the same Vercel one-file-per-
# route reason — see the note on _ZohoSync above. Keep both copies in sync.
def _get_settings(company_id):
    row = sb.table('project_performance_settings').select('*').eq('company_id', company_id).execute()
    if row.data:
        return row.data[0]
    defaults = {
        'company_id': company_id, 'margin_erosion_healthy_max': 2, 'margin_erosion_at_risk_max': 5,
        'health_score_healthy_min': 80, 'health_score_at_risk_min': 60,
        'health_score_weights': {'margin': 30, 'cost_control': 20, 'billing': 15, 'collection': 15, 'cash': 10, 'commitment': 10},
        'billing_gap_alert_threshold': 15,
    }
    try:
        sb.table('project_performance_settings').insert(defaults).execute()
    except Exception:
        pass
    return defaults

def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))

def _raise_alert(company_id, project_id, alert_type, severity, explanation, financial_impact=None):
    """Skips creating a duplicate if this alert type is already open on the
    project — re-syncing every few hours must not spam the alert log."""
    existing = sb.table('project_alerts').select('id').eq('project_id', project_id)\
        .eq('alert_type', alert_type).eq('status', 'open').execute()
    if existing.data:
        return
    sb.table('project_alerts').insert({
        'company_id': company_id, 'project_id': project_id, 'alert_type': alert_type,
        'severity': severity, 'explanation': explanation, 'financial_impact': financial_impact,
    }).execute()

def recalculate_project(company_id, project_id):
    """The single source of truth for a project's current commercial
    position. Reads synced Zoho staging tables + user-maintained forecast
    entries, writes the result onto `projects` (so the dashboard/list can
    read one row, no live joins/Zoho calls at page load), records today's
    project_performance_snapshots row, stores health-score detail, and
    raises/clears core alerts. Called after every sync and after any manual
    forecast/completion edit — never computed ad hoc in the frontend."""
    proj = sb.table('projects').select('*').eq('id', project_id).eq('company_id', company_id).execute()
    if not proj.data:
        return None
    p = proj.data[0]
    settings = _get_settings(company_id)

    original_cost = _num(p.get('original_estimated_cost'))
    original_sell = _num(p.get('original_selling_price'))
    original_gp_pct = _num(p.get('original_gp_pct'))
    revenue_forecast = _num(p.get('revenue_forecast')) or original_sell

    # Actual Cost = posted Bills + Expenses (requirement §12)
    bills = sb.table('zoho_bills').select('total,status').eq('project_id', project_id).execute().data or []
    expenses = sb.table('zoho_expenses').select('amount').eq('project_id', project_id).execute().data or []
    actual_cost = sum(_num(b.get('total')) for b in bills) + sum(_num(e.get('amount')) for e in expenses)

    # Committed Cost = open PO value not yet billed (requirement §12, §18) —
    # excludes amounts already matched to a bill to avoid double counting.
    pos = sb.table('zoho_purchase_orders').select('total,billed_total,status').eq('project_id', project_id).execute().data or []
    committed_cost = sum(max(0.0, _num(po.get('total')) - _num(po.get('billed_total'))) for po in pos
                          if (po.get('status') or '').lower() not in ('cancelled', 'void', 'deleted'))

    # Forecast Remaining Cost = active user-maintained entries (requirement §36)
    forecasts = sb.table('project_forecasts').select('amount').eq('project_id', project_id).eq('status', 'active').execute().data or []
    forecast_remaining = sum(_num(f.get('amount')) for f in forecasts)

    eac = actual_cost + committed_cost + forecast_remaining
    forecast_cost_variance = original_cost - eac
    forecast_gp = revenue_forecast - eac
    forecast_gp_pct = (forecast_gp / revenue_forecast * 100.0) if revenue_forecast else 0.0
    margin_erosion = original_gp_pct - forecast_gp_pct

    # Billing & Collection (requirement §19). Collected Value derived from
    # invoice.total - invoice.balance rather than requiring the payments
    # table to be fully synced — robust even if payment-level sync partially
    # fails, at the cost of losing individual payment-date detail (fine for
    # this project-level rollup; the invoice table in the Billing &
    # Collection tab still lists per-invoice balances).
    invoices = sb.table('zoho_invoices').select('total,balance').eq('project_id', project_id).execute().data or []
    invoiced_value = sum(_num(i.get('total')) for i in invoices)
    collected_value = sum(_num(i.get('total')) - _num(i.get('balance')) for i in invoices)
    unbilled_value = revenue_forecast - invoiced_value
    invoice_pct = (invoiced_value / revenue_forecast * 100.0) if revenue_forecast else 0.0
    collection_pct = (collected_value / invoiced_value * 100.0) if invoiced_value else 0.0

    # Cash Position (requirement §21). Cash Out is an approximation: this
    # schema doesn't yet capture a bill-level paid-amount/balance field
    # (only `status`), so paid bills count in full and partially-paid bills
    # aren't split out. Worth tightening once a real Zoho bill payload
    # confirms the right field (see the note in api/integrations.py's
    # ZohoAdapter.fetch_bills) — flagged here rather than silently assumed
    # correct.
    vendor_cash_paid = sum(_num(b.get('total')) for b in bills if (b.get('status') or '').lower() in ('paid',))
    cash_out = vendor_cash_paid + sum(_num(e.get('amount')) for e in expenses)
    net_cash_position = collected_value - cash_out

    # ── Health score components (0-100 each). Weights/bands are configurable
    # (project_performance_settings); the shape of each component curve
    # below is a first-pass heuristic, not specified by the requirement doc
    # beyond "0-2% margin erosion = Healthy" etc. — reasonable to tune later
    # without changing the overall architecture.
    erosion_healthy = _num(settings.get('margin_erosion_healthy_max'), 2)
    erosion_at_risk = _num(settings.get('margin_erosion_at_risk_max'), 5)
    if margin_erosion <= erosion_healthy:
        margin_health = 100.0
    elif margin_erosion <= erosion_at_risk:
        span = max(0.001, erosion_at_risk - erosion_healthy)
        margin_health = 100.0 - (margin_erosion - erosion_healthy) / span * 40.0
    else:
        margin_health = _clamp(60.0 - (margin_erosion - erosion_at_risk) * 10.0)

    cost_control = 100.0 if original_cost <= 0 else _clamp(100.0 - max(0.0, (eac - original_cost) / original_cost * 100.0))

    completion_pct = _num(p.get('completion_pct'))
    billing_gap_threshold = _num(settings.get('billing_gap_alert_threshold'), 15)
    if completion_pct <= 0:
        billing_health = 100.0  # no completion % recorded yet — nothing to compare against
    else:
        gap = completion_pct - invoice_pct
        billing_health = 100.0 if gap <= 0 else _clamp(100.0 - (gap / max(1.0, billing_gap_threshold)) * 50.0)

    collection_health = _clamp(collection_pct) if invoiced_value else 100.0

    if net_cash_position >= 0:
        cash_health = 100.0
    else:
        denom = revenue_forecast or original_sell or 1.0
        cash_health = _clamp(100.0 - abs(net_cash_position) / denom * 100.0)

    remaining_budget = original_cost - actual_cost
    if remaining_budget > 0:
        commitment_exposure = _clamp(100.0 - min(1.0, committed_cost / remaining_budget) * 100.0)
    else:
        commitment_exposure = 0.0 if committed_cost > 0 else 100.0

    weights = settings.get('health_score_weights') or {}
    w = lambda k, d: _num(weights.get(k), d)
    total_w = sum([w('margin', 30), w('cost_control', 20), w('billing', 15), w('collection', 15), w('cash', 10), w('commitment', 10)]) or 100.0
    overall = (
        margin_health * w('margin', 30) + cost_control * w('cost_control', 20) +
        billing_health * w('billing', 15) + collection_health * w('collection', 15) +
        cash_health * w('cash', 10) + commitment_exposure * w('commitment', 10)
    ) / total_w

    healthy_min = _num(settings.get('health_score_healthy_min'), 80)
    at_risk_min = _num(settings.get('health_score_at_risk_min'), 60)
    status = 'healthy' if overall >= healthy_min else ('at_risk' if overall >= at_risk_min else 'critical')

    update = {
        'actual_cost': actual_cost, 'committed_cost': committed_cost, 'forecast_remaining_cost': forecast_remaining,
        'estimate_at_completion': eac, 'forecast_gp': forecast_gp, 'forecast_gp_pct': forecast_gp_pct,
        'margin_erosion_pct': margin_erosion, 'invoiced_value': invoiced_value, 'collected_value': collected_value,
        'net_cash_position': net_cash_position, 'health_score': overall, 'health_status': status,
    }
    sb.table('projects').update(update).eq('id', project_id).eq('company_id', company_id).execute()

    sb.table('project_health_scores').insert({
        'company_id': company_id, 'project_id': project_id,
        'margin_health': margin_health, 'cost_control': cost_control, 'billing_health': billing_health,
        'collection_health': collection_health, 'cash_health': cash_health, 'commitment_exposure': commitment_exposure,
        'weights': weights, 'overall_score': overall, 'status': status,
    }).execute()

    snapshot = {
        'company_id': company_id, 'project_id': project_id,
        'project_value': revenue_forecast, 'original_gp_pct': original_gp_pct, 'forecast_gp_pct': forecast_gp_pct,
        'margin_erosion_pct': margin_erosion, 'actual_cost': actual_cost, 'committed_cost': committed_cost,
        'forecast_remaining_cost': forecast_remaining, 'estimate_at_completion': eac,
        'invoiced_value': invoiced_value, 'collected_value': collected_value,
        'net_cash_position': net_cash_position, 'health_score': overall,
    }
    sb.table('project_performance_snapshots').upsert(snapshot, on_conflict='project_id,snapshot_date').execute()

    # Core alert set (requirement §27) — the rest of the catalogue (PO vs
    # quotation-line variance, vendor concentration, etc.) needs Phase 2's
    # product/transaction mapping to compute meaningfully and is deferred.
    if margin_erosion > erosion_at_risk:
        _raise_alert(company_id, project_id, 'margin_erosion_critical', 'critical',
                     f'Margin erosion is {margin_erosion:.1f}%, above the {erosion_at_risk:.1f}% critical threshold.', forecast_gp - (original_gp_pct/100*revenue_forecast if revenue_forecast else 0))
    elif margin_erosion > erosion_healthy:
        _raise_alert(company_id, project_id, 'margin_erosion_at_risk', 'medium',
                     f'Margin erosion is {margin_erosion:.1f}%, above the {erosion_healthy:.1f}% healthy threshold.')
    if forecast_cost_variance < 0:
        _raise_alert(company_id, project_id, 'eac_exceeds_budget', 'high',
                     f'Estimate at Completion (AED {eac:,.0f}) exceeds the original budget (AED {original_cost:,.0f}).', forecast_cost_variance)
    if actual_cost + committed_cost > original_cost > 0:
        _raise_alert(company_id, project_id, 'actual_plus_committed_exceeds_budget', 'high',
                     f'Actual + Committed cost (AED {actual_cost + committed_cost:,.0f}) exceeds the original budget (AED {original_cost:,.0f}).')
    if completion_pct > 0 and (completion_pct - invoice_pct) > billing_gap_threshold:
        _raise_alert(company_id, project_id, 'billing_gap', 'medium',
                     f'Project is {completion_pct:.0f}% complete but only {invoice_pct:.0f}% invoiced.')
    if net_cash_position < 0 and forecast_gp > 0:
        _raise_alert(company_id, project_id, 'profitable_but_cash_negative', 'high',
                     f'Project is forecast profitable (AED {forecast_gp:,.0f} GP) but cash position is AED {net_cash_position:,.0f}.')

    return update


# ── Manual "Sync Now" — per project, or every linked project in the company
@app.route('/api/pp-sync/run', methods=['POST'])
def run_sync():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'project_performance'): return jsonify({'error': 'Feature not enabled'}), 403

    d = request.json or {}
    company_id = claims['company_id']
    pid = d.get('project_id')

    if pid:
        proj = sb.table('projects').select('*').eq('id', pid).eq('company_id', company_id).execute()
        if not proj.data: return jsonify({'error': 'Project not found'}), 404
        project = proj.data[0]
        allowed = can_manage(claims) or claims['user_id'] in (project.get('project_manager_id'), project.get('salesperson_id'))
        if not allowed: return jsonify({'error': 'Forbidden'}), 403
        projects = [project]
    else:
        if not can_manage(claims): return jsonify({'error': 'Admin only — ask a company admin to sync the whole portfolio.'}), 403
        projects = sb.table('projects').select('*').eq('company_id', company_id).not_.is_('zoho_project_id', 'null').execute().data or []

    summary = []
    for project in projects:
        if not project.get('zoho_project_id'):
            continue
        sync_results = _sync_project_actuals(company_id, project)
        calc = None
        try:
            calc = recalculate_project(company_id, project['id'])
        except Exception as e:
            traceback.print_exc()
        summary.append({'project_id': project['id'], 'name': project.get('name'), 'sync': sync_results, 'recalculated': bool(calc)})

    return jsonify({'ok': True, 'projects_synced': len(summary), 'results': summary})

# ── Cron: run once daily via Vercel's Cron Jobs, same pattern as the
# existing customer/vendor auto-sync in api/integrations.py ────────────────
@app.route('/api/pp-sync/run-auto-sync', methods=['GET', 'POST'])
def run_auto_sync():
    auth = request.headers.get('Authorization', '')
    provided = auth[7:] if auth.startswith('Bearer ') else ''
    provided = provided or request.args.get('secret') or request.headers.get('X-Cron-Secret') or ''
    if not CRON_SECRET or provided != CRON_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    # Phase 1 simplification: every company with the project_performance
    # feature flag on and a connected Zoho integration gets synced daily —
    # there's no separate opt-in toggle yet (mirrors the current state of
    # the `projects` feature itself, which also has no admin UI to flip it).
    companies = sb.table('companies').select('id,features').execute().data or []
    results = []
    for co in companies:
        if not (co.get('features') or {}).get('project_performance'):
            continue
        company_id = co['id']
        projects = sb.table('projects').select('*').eq('company_id', company_id).not_.is_('zoho_project_id', 'null').execute().data or []
        for project in projects:
            sync_results = _sync_project_actuals(company_id, project)
            try:
                recalculate_project(company_id, project['id'])
            except Exception:
                traceback.print_exc()
            results.append({'company_id': company_id, 'project_id': project['id'], 'sync': sync_results})
    return jsonify({'companies_processed': len(results), 'results': results})

# ── Sync status — last runs per project, for the sync-status UI ────────────
@app.route('/api/pp-sync/status', methods=['GET'])
def sync_status():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'project_performance'): return jsonify({'error': 'Feature not enabled'}), 403

    company_id = claims['company_id']
    pid = request.args.get('project_id')
    q = sb.table('pp_sync_logs').select('*').eq('company_id', company_id)
    if pid: q = q.eq('project_id', pid)
    rows = q.order('started_at', desc=True).limit(50).execute()
    return jsonify({'logs': rows.data or []})

if __name__ == '__main__':
    app.run(debug=True)
