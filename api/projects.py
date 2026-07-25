from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import os, jwt, traceback, json, time, secrets
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from supabase import create_client

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')
CRON_SECRET  = os.environ.get('CRON_SECRET')
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# Syncing every linked project's full Zoho actuals (POs + per-PO line items,
# bills + per-bill line items, expenses, invoices, payments) in one HTTP
# request doesn't scale past a handful of projects -- on a portfolio of 20+
# it was blowing past Vercel's function time limit and dropping the
# connection ("Server disconnected" in the browser) before anything got
# written. Both sync entry points below are time-boxed instead: they process
# as many projects as fit in the budget, oldest-synced-first, and report how
# many are left so the caller (manual Sync Now, or tomorrow's cron run) can
# pick up where this call stopped.
MANUAL_SYNC_TIME_BUDGET_SECONDS = 8
CRON_SYNC_TIME_BUDGET_SECONDS = 45

@app.errorhandler(Exception)
def handle_exception(e):
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

def has_feature(claims, feature):
    return bool((claims.get('features') or {}).get(feature))

def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))

_PUBLIC_TESTIMONIAL_PATHS = ('/api/testimonials/verify', '/api/testimonials/submit', '/api/testimonials/public')

@app.before_request
def _rbac_page_gate():
    if request.path in _PUBLIC_TESTIMONIAL_PATHS:
        return None
    claims = verify_token(request)
    if not claims:
        return None
    path = request.path
    if path.startswith('/api/testimonials'):
        page_key = 'testimonials'
    elif path.startswith('/api/project-performance') or path.startswith('/api/pp-sync'):
        page_key = 'projectPerformance'
    else:
        page_key = 'projects'
    if not has_page_access(claims, page_key):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    return None

STATUSES = ('active','on_hold','completed','cancelled')

def clean_project(d):
    out = {}
    for k in ('name','customer','site_location','po_number','notes'):
        if k in d: out[k] = str(d.get(k) or '').strip()[:500]
    if 'status' in d:
        s = str(d.get('status') or '').strip()
        if s in STATUSES: out['status'] = s
    for k in ('start_date','end_date'):
        if k in d:
            v = str(d.get(k) or '').strip()[:10]
            out[k] = v if v else None
    return out

# -- List projects ----------------------------------------------------------
@app.route('/api/projects', methods=['GET'])
def list_projects():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'projects'): return jsonify({'error': 'Feature not enabled'}), 403
    q = sb.table('projects').select('*').eq('company_id', claims['company_id']).order('created_at', desc=True)
    status = (request.args.get('status') or '').strip()
    if status in STATUSES: q = q.eq('status', status)
    search = (request.args.get('search') or '').strip()
    if search: q = q.ilike('name', f'%{search}%')
    rows = q.execute()
    return jsonify({'projects': rows.data or []})

# -- Get single project -----------------------------------------------------
@app.route('/api/projects/<pid>', methods=['GET'])
def get_project(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'projects'): return jsonify({'error': 'Feature not enabled'}), 403
    row = sb.table('projects').select('*').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Not found'}), 404
    quotes = sb.table('quotes').select('id,title,quote_ref,status,created_at').eq('project_id', pid).eq('company_id', claims['company_id']).order('created_at', desc=True).execute()
    return jsonify({'project': row.data[0], 'quotes': quotes.data or []})

# -- Create project ---------------------------------------------------------
@app.route('/api/projects', methods=['POST'])
def create_project():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'projects'): return jsonify({'error': 'Feature not enabled'}), 403
    d = clean_project(request.json or {})
    if not d.get('name'): return jsonify({'error': 'Project name is required'}), 400
    d['company_id'] = claims['company_id']
    d['created_by'] = claims['user_id']
    if 'status' not in d: d['status'] = 'active'
    row = sb.table('projects').insert(d).execute()
    return jsonify({'project': row.data[0]})

# -- Update project ---------------------------------------------------------
@app.route('/api/projects/<pid>', methods=['PUT'])
def update_project(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'projects'): return jsonify({'error': 'Feature not enabled'}), 403
    exists = sb.table('projects').select('id').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not exists.data: return jsonify({'error': 'Not found'}), 404
    d = clean_project(request.json or {})
    if not d: return jsonify({'error': 'Nothing to update'}), 400
    sb.table('projects').update(d).eq('id', pid).eq('company_id', claims['company_id']).execute()
    return jsonify({'ok': True})

# -- Delete project ---------------------------------------------------------
@app.route('/api/projects/<pid>', methods=['DELETE'])
def delete_project(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'projects'): return jsonify({'error': 'Feature not enabled'}), 403
    if claims.get('role') != 'admin': return jsonify({'error': 'Admin only'}), 403
    exists = sb.table('projects').select('id').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not exists.data: return jsonify({'error': 'Not found'}), 404
    sb.table('quotes').update({'project_id': None}).eq('project_id', pid).eq('company_id', claims['company_id']).execute()
    sb.table('projects').delete().eq('id', pid).eq('company_id', claims['company_id']).execute()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════
# Project Performance — originally api/project_performance.py and
# api/pp_sync.py, merged in here because Vercel's Hobby plan caps a
# deployment at 12 Serverless Functions and this repo was already at that
# ceiling with 12 files before this module existed. Project Performance is
# a direct extension of the `projects` table/feature above, so this is
# also the most natural home for it, not just the path of least resistance
# on the function count. verify_token/has_feature above are shared as-is;
# everything below is otherwise self-contained the same way every other
# api/*.py file is (Vercel's Python builder packages one file per route,
# no imports across sibling route files).
# ══════════════════════════════════════════════════════════════════════════

def is_admin(claims):
    return (claims or {}).get('role') == 'admin'

def can_manage(claims):
    return is_admin(claims) or bool(claims.get('can_manage_project_performance'))

def _num(v, d=0.0):
    try:
        return float(v) if v not in (None, '') else d
    except (TypeError, ValueError):
        return d

def _require_pp(claims):
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'project_performance'): return jsonify({'error': 'Feature not enabled'}), 403
    return None

def _project_access(claims, project):
    """Management (admin/can_manage_project_performance) sees everything.
    A plain user only gets a project if they're its assigned Project Manager
    or Salesperson — mirrors the can_view_all_quotes-style narrow-by-default
    scoping already used for quotes (see api/quotes.py)."""
    if can_manage(claims): return True
    uid = claims.get('user_id')
    return uid in (project.get('project_manager_id'), project.get('salesperson_id'))


# ── Calculation engine ═══════════════════════════════════════════════════════
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

    bills = sb.table('zoho_bills').select('total,status,zoho_purchase_order_id').eq('project_id', project_id).execute().data or []
    expenses = sb.table('zoho_expenses').select('amount').eq('project_id', project_id).execute().data or []
    # Split actual cost by whether it traces back to a Purchase Order or was
    # entered directly (a non-PO bill, or a project expense) -- both feed
    # into Zoho Books Projects and both count toward actual cost, but PMs
    # want to see the split since PO-based spend went through approval.
    po_based_cost = sum(_num(b.get('total')) for b in bills if b.get('zoho_purchase_order_id'))
    non_po_based_cost = sum(_num(b.get('total')) for b in bills if not b.get('zoho_purchase_order_id')) \
        + sum(_num(e.get('amount')) for e in expenses)
    actual_cost = po_based_cost + non_po_based_cost

    pos = sb.table('zoho_purchase_orders').select('total,billed_total,status').eq('project_id', project_id).execute().data or []
    committed_cost = sum(max(0.0, _num(po.get('total')) - _num(po.get('billed_total'))) for po in pos
                          if (po.get('status') or '').lower() not in ('cancelled', 'void', 'deleted'))

    forecasts = sb.table('project_forecasts').select('amount').eq('project_id', project_id).eq('status', 'active').execute().data or []
    forecast_remaining = sum(_num(f.get('amount')) for f in forecasts)

    eac = actual_cost + committed_cost + forecast_remaining
    forecast_cost_variance = original_cost - eac
    forecast_gp = revenue_forecast - eac
    forecast_gp_pct = (forecast_gp / revenue_forecast * 100.0) if revenue_forecast else 0.0
    margin_erosion = original_gp_pct - forecast_gp_pct

    invoices = sb.table('zoho_invoices').select('total,balance').eq('project_id', project_id).execute().data or []
    invoiced_value = sum(_num(i.get('total')) for i in invoices)
    collected_value = sum(_num(i.get('total')) - _num(i.get('balance')) for i in invoices)
    invoice_pct = (invoiced_value / revenue_forecast * 100.0) if revenue_forecast else 0.0
    collection_pct = (collected_value / invoiced_value * 100.0) if invoiced_value else 0.0

    vendor_cash_paid = sum(_num(b.get('total')) for b in bills if (b.get('status') or '').lower() in ('paid',))
    cash_out = vendor_cash_paid + sum(_num(e.get('amount')) for e in expenses)
    net_cash_position = collected_value - cash_out

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
        billing_health = 100.0
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
        'actual_cost': actual_cost, 'po_based_actual_cost': po_based_cost, 'non_po_based_actual_cost': non_po_based_cost,
        'committed_cost': committed_cost, 'forecast_remaining_cost': forecast_remaining,
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

    if margin_erosion > erosion_at_risk:
        _raise_alert(company_id, project_id, 'margin_erosion_critical', 'critical',
                     f'Margin erosion is {margin_erosion:.1f}%, above the {erosion_at_risk:.1f}% critical threshold.')
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


# ── Portfolio dashboard + project list ──────────────────────────────────────
PP_LIST_FIELDS = ('id,name,customer,status,project_manager_id,salesperson_id,project_type,'
                   'revenue_forecast,original_gp_pct,forecast_gp_pct,margin_erosion_pct,actual_cost,'
                   'po_based_actual_cost,non_po_based_actual_cost,'
                   'committed_cost,estimate_at_completion,invoiced_value,collected_value,net_cash_position,'
                   'health_score,health_status,completion_pct,zoho_project_id,zoho_project_no,quote_ref,created_at')

@app.route('/api/project-performance', methods=['GET'])
def pp_portfolio():
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    company_id = claims['company_id']

    q = sb.table('projects').select(PP_LIST_FIELDS).eq('company_id', company_id)
    if not can_manage(claims):
        uid = claims['user_id']
        q = q.or_(f'project_manager_id.eq.{uid},salesperson_id.eq.{uid}')

    a = request.args
    if a.get('customer'): q = q.ilike('customer', f"%{a['customer']}%")
    if a.get('project_manager_id'): q = q.eq('project_manager_id', a['project_manager_id'])
    if a.get('salesperson_id'): q = q.eq('salesperson_id', a['salesperson_id'])
    if a.get('status'): q = q.eq('status', a['status'])
    if a.get('health_status'): q = q.eq('health_status', a['health_status'])
    if a.get('project_type'): q = q.eq('project_type', a['project_type'])
    if a.get('date_from'): q = q.gte('created_at', a['date_from'])
    if a.get('date_to'): q = q.lte('created_at', a['date_to'])
    if a.get('gp_min'): q = q.gte('forecast_gp_pct', _num(a.get('gp_min')))
    if a.get('gp_max'): q = q.lte('forecast_gp_pct', _num(a.get('gp_max')))
    if a.get('erosion_min'): q = q.gte('margin_erosion_pct', _num(a.get('erosion_min')))
    if a.get('erosion_max'): q = q.lte('margin_erosion_pct', _num(a.get('erosion_max')))
    if a.get('value_min'): q = q.gte('revenue_forecast', _num(a.get('value_min')))
    if a.get('value_max'): q = q.lte('revenue_forecast', _num(a.get('value_max')))
    search = (a.get('search') or '').strip()
    if search:
        q = q.or_(f'name.ilike.%{search}%,customer.ilike.%{search}%,zoho_project_id.ilike.%{search}%,quote_ref.ilike.%{search}%')

    sort = a.get('sort') or 'created_at'
    desc = (a.get('dir') or 'desc') == 'desc'
    if sort not in PP_LIST_FIELDS.split(','):
        sort = 'created_at'
    rows = q.order(sort, desc=desc).execute().data or []

    active = [r for r in rows if r.get('status') == 'active']
    total_value = sum(_num(r.get('revenue_forecast')) for r in active)
    weighted_gp = (sum(_num(r.get('forecast_gp_pct')) * _num(r.get('revenue_forecast')) for r in active) / total_value) if total_value else 0.0
    kpis = {
        'active_projects': len(active),
        'total_active_project_value': total_value,
        'portfolio_forecast_gp_pct': weighted_gp,
        'projects_at_risk': len([r for r in rows if r.get('health_status') == 'at_risk']),
        'critical_projects': len([r for r in rows if r.get('health_status') == 'critical']),
        'total_unbilled_value': sum(_num(r.get('revenue_forecast')) - _num(r.get('invoiced_value')) for r in rows),
        'total_outstanding_receivables': sum(_num(r.get('invoiced_value')) - _num(r.get('collected_value')) for r in rows),
        'total_project_cash_exposure': sum(min(0.0, _num(r.get('net_cash_position'))) for r in rows),
    }
    return jsonify({'kpis': kpis, 'projects': rows, 'total': len(rows)})


# ── Project detail — Overview / Financial / Cost Control / Billing & Cash tabs
@app.route('/api/project-performance/<pid>', methods=['GET'])
def pp_project_detail(pid):
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    company_id = claims['company_id']

    row = sb.table('projects').select('*').eq('id', pid).eq('company_id', company_id).execute()
    if not row.data: return jsonify({'error': 'Not found'}), 404
    project = row.data[0]
    if not _project_access(claims, project):
        return jsonify({'error': 'Forbidden'}), 403

    baseline_row = sb.table('project_commercial_baselines').select('*').eq('project_id', pid)\
        .order('created_at', desc=True).limit(1).execute()
    baseline = baseline_row.data[0] if baseline_row.data else None
    baseline_sections = []
    if baseline:
        sections = sb.table('project_baseline_sections').select('*').eq('baseline_id', baseline['id']).execute().data or []
        for s in sections:
            s['lines'] = sb.table('project_baseline_lines').select('*').eq('section_id', s['id']).execute().data or []
        baseline_sections = sections

    pos = sb.table('zoho_purchase_orders').select('*').eq('project_id', pid).order('po_date', desc=True).execute().data or []
    bills = sb.table('zoho_bills').select('*').eq('project_id', pid).order('bill_date', desc=True).execute().data or []
    expenses = sb.table('zoho_expenses').select('*').eq('project_id', pid).order('expense_date', desc=True).execute().data or []
    invoices = sb.table('zoho_invoices').select('*').eq('project_id', pid).order('invoice_date', desc=True).execute().data or []
    payments = sb.table('zoho_payments').select('*').eq('project_id', pid).order('payment_date', desc=True).execute().data or []
    forecasts = sb.table('project_forecasts').select('*').eq('project_id', pid).order('created_at', desc=True).execute().data or []
    progress = sb.table('project_progress_history').select('*').eq('project_id', pid).order('created_at', desc=True).execute().data or []
    budget_revisions = sb.table('project_budget_revisions').select('*').eq('project_id', pid).order('revision_number', desc=True).execute().data or []
    health = sb.table('project_health_scores').select('*').eq('project_id', pid).order('calculated_at', desc=True).limit(1).execute()
    snapshots = sb.table('project_performance_snapshots').select('*').eq('project_id', pid).order('snapshot_date').execute().data or []
    alerts = sb.table('project_alerts').select('*').eq('project_id', pid).order('created_at', desc=True).execute().data or []

    # Cost Control breakdown by category — Phase 1 groups by cost_category_id
    # (null = "Uncategorized" until Phase 2's mapping config assigns one).
    categories = sb.table('project_cost_categories').select('*').eq('company_id', company_id).eq('is_active', True).execute().data or []
    cat_name = {c['id']: c['name'] for c in categories}
    breakdown = {}
    def _bucket(cat_id, actual=0.0, committed=0.0):
        key = cat_id or '__uncategorized__'
        b = breakdown.setdefault(key, {'cost_category_id': cat_id, 'name': cat_name.get(cat_id, 'Uncategorized'), 'actual': 0.0, 'committed': 0.0, 'forecast_remaining': 0.0})
        b['actual'] += actual; b['committed'] += committed
    bill_lines = sb.table('zoho_bill_lines').select('cost_category_id,total').in_('bill_id', [b['id'] for b in bills]).execute().data if bills else []
    for li in bill_lines:
        _bucket(li.get('cost_category_id'), actual=_num(li.get('total')))
    for e_ in expenses:
        _bucket(e_.get('cost_category_id'), actual=_num(e_.get('amount')))
    po_lines = sb.table('zoho_purchase_order_lines').select('cost_category_id,total').in_('purchase_order_id', [po['id'] for po in pos]).execute().data if pos else []
    for li in po_lines:
        _bucket(li.get('cost_category_id'), committed=_num(li.get('total')))
    for f in forecasts:
        if f.get('status') == 'active':
            key = f.get('cost_category_id') or '__uncategorized__'
            b = breakdown.setdefault(key, {'cost_category_id': f.get('cost_category_id'), 'name': cat_name.get(f.get('cost_category_id'), 'Uncategorized'), 'actual': 0.0, 'committed': 0.0, 'forecast_remaining': 0.0})
            b['forecast_remaining'] += _num(f.get('amount'))

    return jsonify({
        'project': project,
        'baseline': baseline,
        'baseline_sections': baseline_sections,
        'purchase_orders': pos,
        'bills': bills,
        'expenses': expenses,
        'invoices': invoices,
        'payments': payments,
        'forecasts': forecasts,
        'progress_history': progress,
        'budget_revisions': budget_revisions,
        'health_detail': health.data[0] if health.data else None,
        'snapshots': snapshots,
        'alerts': alerts,
        'cost_breakdown': list(breakdown.values()),
    })

@app.route('/api/project-performance/<pid>/recalculate', methods=['POST'])
def pp_force_recalculate(pid):
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    company_id = claims['company_id']
    row = sb.table('projects').select('id,project_manager_id,salesperson_id').eq('id', pid).eq('company_id', company_id).execute()
    if not row.data: return jsonify({'error': 'Not found'}), 404
    if not _project_access(claims, row.data[0]): return jsonify({'error': 'Forbidden'}), 403
    result = recalculate_project(company_id, pid)
    return jsonify({'ok': True, 'project': result})


# ── Forecast Remaining Cost — add / revise / remove-with-reason (§36) ──────
@app.route('/api/project-performance/<pid>/forecast', methods=['POST'])
def pp_add_forecast(pid):
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    company_id = claims['company_id']
    proj = sb.table('projects').select('id,project_manager_id,salesperson_id').eq('id', pid).eq('company_id', company_id).execute()
    if not proj.data: return jsonify({'error': 'Not found'}), 404
    if not _project_access(claims, proj.data[0]): return jsonify({'error': 'Forbidden'}), 403

    d = request.json or {}
    amount = _num(d.get('amount'))
    row = sb.table('project_forecasts').insert({
        'company_id': company_id, 'project_id': pid, 'cost_category_id': d.get('cost_category_id'),
        'amount': amount, 'expected_date': d.get('expected_date'), 'description': (d.get('description') or '')[:1000],
        'created_by': claims['user_id'],
    }).execute()
    recalculate_project(company_id, pid)
    return jsonify({'forecast': row.data[0]}), 201

@app.route('/api/project-performance/<pid>/forecast/<fid>', methods=['PUT'])
def pp_revise_forecast(pid, fid):
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    company_id = claims['company_id']
    proj = sb.table('projects').select('id,project_manager_id,salesperson_id').eq('id', pid).eq('company_id', company_id).execute()
    if not proj.data: return jsonify({'error': 'Not found'}), 404
    if not _project_access(claims, proj.data[0]): return jsonify({'error': 'Forbidden'}), 403

    existing = sb.table('project_forecasts').select('*').eq('id', fid).eq('project_id', pid).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404
    d = request.json or {}
    sb.table('project_forecasts').update({'status': 'revised'}).eq('id', fid).execute()
    row = sb.table('project_forecasts').insert({
        'company_id': company_id, 'project_id': pid,
        'cost_category_id': d.get('cost_category_id', existing.data[0].get('cost_category_id')),
        'amount': _num(d.get('amount', existing.data[0].get('amount'))),
        'expected_date': d.get('expected_date', existing.data[0].get('expected_date')),
        'description': (d.get('description') or existing.data[0].get('description') or '')[:1000],
        'created_by': claims['user_id'],
    }).execute()
    recalculate_project(company_id, pid)
    return jsonify({'forecast': row.data[0]})

@app.route('/api/project-performance/<pid>/forecast/<fid>', methods=['DELETE'])
def pp_remove_forecast(pid, fid):
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    company_id = claims['company_id']
    proj = sb.table('projects').select('id,project_manager_id,salesperson_id').eq('id', pid).eq('company_id', company_id).execute()
    if not proj.data: return jsonify({'error': 'Not found'}), 404
    if not _project_access(claims, proj.data[0]): return jsonify({'error': 'Forbidden'}), 403

    d = request.json or {}
    reason = (d.get('reason') or '').strip()
    if not reason: return jsonify({'error': 'A reason is required to remove a forecast entry'}), 400
    sb.table('project_forecasts').update({'status': 'removed', 'removal_reason': reason}).eq('id', fid).eq('project_id', pid).execute()
    recalculate_project(company_id, pid)
    return jsonify({'ok': True})


# ── Completion percentage history (§20) ─────────────────────────────────────
@app.route('/api/project-performance/<pid>/completion', methods=['POST'])
def pp_add_completion(pid):
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    company_id = claims['company_id']
    proj = sb.table('projects').select('id,project_manager_id,salesperson_id').eq('id', pid).eq('company_id', company_id).execute()
    if not proj.data: return jsonify({'error': 'Not found'}), 404
    if not _project_access(claims, proj.data[0]): return jsonify({'error': 'Forbidden'}), 403

    d = request.json or {}
    pct = _clamp(_num(d.get('percentage')), 0, 100)
    sb.table('project_progress_history').insert({
        'company_id': company_id, 'project_id': pid, 'percentage': pct,
        'updated_by': claims['user_id'], 'comment': (d.get('comment') or '')[:1000],
    }).execute()
    sb.table('projects').update({'completion_pct': pct}).eq('id', pid).eq('company_id', company_id).execute()
    recalculate_project(company_id, pid)
    return jsonify({'ok': True, 'completion_pct': pct}), 201


# ── Approved budget revisions (§37) ─────────────────────────────────────────
@app.route('/api/project-performance/<pid>/budget-revision', methods=['POST'])
def pp_add_budget_revision(pid):
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    if not can_manage(claims): return jsonify({'error': 'Admin only'}), 403
    company_id = claims['company_id']
    proj = sb.table('projects').select('id,original_estimated_cost').eq('id', pid).eq('company_id', company_id).execute()
    if not proj.data: return jsonify({'error': 'Not found'}), 404

    d = request.json or {}
    prior = sb.table('project_budget_revisions').select('revision_number').eq('project_id', pid).order('revision_number', desc=True).limit(1).execute()
    next_num = (prior.data[0]['revision_number'] + 1) if prior.data else 1
    previous_budget = prior.data[0].get('revised_budget') if prior.data else proj.data[0].get('original_estimated_cost')
    row = sb.table('project_budget_revisions').insert({
        'company_id': company_id, 'project_id': pid, 'revision_number': next_num,
        'reason': (d.get('reason') or '')[:1000], 'previous_budget': previous_budget,
        'revised_budget': _num(d.get('revised_budget')), 'created_by': claims['user_id'],
        'approved_by': d.get('approved_by'),
    }).execute()
    return jsonify({'budget_revision': row.data[0]}), 201


# ── Cost categories config (§14) ────────────────────────────────────────────
@app.route('/api/project-performance/cost-categories', methods=['GET'])
def pp_list_cost_categories():
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    rows = sb.table('project_cost_categories').select('*').eq('company_id', claims['company_id']).order('sort_order').execute()
    return jsonify({'cost_categories': rows.data or []})

@app.route('/api/project-performance/cost-categories', methods=['POST'])
def pp_create_cost_category():
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    if not can_manage(claims): return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    name = (d.get('name') or '').strip()
    if not name: return jsonify({'error': 'Name is required'}), 400
    row = sb.table('project_cost_categories').insert({
        'company_id': claims['company_id'], 'name': name[:100], 'sort_order': int(d.get('sort_order') or 0),
    }).execute()
    return jsonify({'cost_category': row.data[0]}), 201


# ── Alerts (§27) ─────────────────────────────────────────────────────────────
@app.route('/api/project-performance/alerts', methods=['GET'])
def pp_list_alerts():
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    company_id = claims['company_id']
    q = sb.table('project_alerts').select('*').eq('company_id', company_id)
    status = request.args.get('status')
    if status: q = q.eq('status', status)
    pid = request.args.get('project_id')
    if pid: q = q.eq('project_id', pid)
    rows = q.order('created_at', desc=True).limit(500).execute()
    return jsonify({'alerts': rows.data or []})

@app.route('/api/project-performance/alerts/<aid>', methods=['PUT'])
def pp_update_alert(aid):
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    company_id = claims['company_id']
    existing = sb.table('project_alerts').select('id').eq('id', aid).eq('company_id', company_id).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404
    d = request.json or {}
    allowed = ['status', 'assigned_user_id', 'resolution_comment']
    update = {k: d[k] for k in allowed if k in d}
    if update.get('status') not in (None, 'open', 'acknowledged', 'under_review', 'resolved', 'ignored'):
        return jsonify({'error': 'Invalid status'}), 400
    row = sb.table('project_alerts').update(update).eq('id', aid).eq('company_id', company_id).execute()
    return jsonify({'alert': row.data[0]})


# ══ Zoho Books adapter (sync-relevant subset) ═══════════════════════════════
# Duplicated from api/integrations.py's ZohoAdapter for the same
# one-file-per-route reason. Only the pieces sync needs: token refresh + the
# five actuals-fetch methods. Keep in sync by hand if that adapter changes.
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
        using per-line project assignment)."""
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
                # Bills are matched against the PO as a whole in Zoho, not
                # per line, so there's no exact per-project billed figure --
                # prorate by this project's share of the PO total instead.
                share = (po_total / header_total) if header_total else 0.0
                billed_total = float(r.get('total_invoiced_amount') or r.get('billed_amount') or 0) * share
            else:
                matching = all_lines
                po_total = r.get('total') or 0
                billed_total = r.get('total_invoiced_amount') or r.get('billed_amount') or 0

            pos.append({
                'zoho_purchase_order_id': po_id, 'zoho_project_id': zoho_project_id,
                'po_number': r.get('purchaseorder_number'), 'po_date': r.get('date'),
                'vendor_name': r.get('vendor_name'), 'status': r.get('status'),
                'total': po_total,
                'billed_total': billed_total,
                'raw': r,
            })
            for li in matching:
                lines.append({
                    'zoho_purchase_order_id': po_id, 'zoho_purchase_order_line_id': li.get('line_item_id'),
                    'zoho_item_id': li.get('item_id'), 'item_name': li.get('name') or li.get('description'),
                    'quantity': li.get('quantity') or 0, 'rate': li.get('rate') or 0,
                    'total': li.get('item_total') or 0, 'raw': li,
                })
        return pos, lines

    def fetch_bills(self, creds, zoho_project_id):
        """Same rationale as fetch_purchase_orders above: Zoho lets each
        bill line item be assigned to its own project too (same 'line item
        -> More -> Project' mechanism), so a bill can span multiple
        projects. When any line carries a project_id, scope this bill's
        total (and the lines stored) to just this project's share instead
        of the header total. Falls back to the header total when no line
        carries a project_id (org isn't using per-line project assignment,
        or this Zoho org's Bills line items don't expose it)."""
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

            # Zoho's bill detail payload carries the PO link as a 'purchaseorder_ids'
            # array (confirmed via live API response), not a singular
            # 'purchaseorder_id'/'purchase_order_id' field -- those never populate,
            # which was silently sending every PO-linked bill into the non-PO
            # cost bucket. Prefer the detail payload; fall back to the list-row
            # fields in case an older org/API shape ever provides them instead.
            po_ids = (detail.get('bill') or {}).get('purchaseorder_ids') or []
            po_id = (po_ids[0] if po_ids else None) or r.get('purchaseorder_id') or r.get('purchase_order_id')

            bills.append({
                'zoho_bill_id': bill_id,
                'zoho_purchase_order_id': po_id,
                'bill_number': r.get('bill_number'), 'bill_date': r.get('date'),
                'vendor_name': r.get('vendor_name'), 'status': r.get('status'),
                'total': bill_total, 'raw': r,
            })
            for li in matching:
                lines.append({
                    'zoho_bill_id': bill_id, 'zoho_bill_line_id': li.get('line_item_id'),
                    'zoho_item_id': li.get('item_id'), 'item_name': li.get('name') or li.get('description'),
                    'quantity': li.get('quantity') or 0, 'rate': li.get('rate') or 0,
                    'total': li.get('item_total') or 0, 'raw': li,
                })
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

    def fetch_all_projects(self, creds):
        """List every project that exists in Zoho Books for this org --
        independent of whether it was ever linked to a project in this app.
        Used by the daily import job / manual Sync Now to pull in projects
        created directly in Zoho."""
        rows = self._fetch_paginated(creds, '/projects', {}, 'projects')
        out = []
        for r in rows:
            pid = r.get('project_id')
            if not pid: continue
            out.append({
                'zoho_project_id': str(pid),
                'zoho_project_no': (str(r.get('cf_project_no')) if r.get('cf_project_no') else None),
                'project_name': (r.get('project_name') or 'Untitled Zoho Project')[:500],
                'customer_name': (r.get('customer_name') or '')[:500],
                'status': r.get('status'),
                # `rate` only means "Total Project Cost" for fixed-cost
                # billing -- for hourly/daily/task billing types it's a
                # per-hour/day rate, not a project value, so don't use it there.
                'total_project_cost': (r.get('rate') if r.get('billing_type') == 'fixed_cost_for_project' else None),
            })
        return out

    def fetch_project(self, creds, zoho_project_id):
        """Fetch a single project by Zoho ID -- used for targeted test syncs
        where the caller names specific project IDs that may not have been
        pulled in by the full fetch_all_projects list yet."""
        data = self._get(creds, f'/projects/{zoho_project_id}', {})
        r = data.get('project') or {}
        if not r.get('project_id'):
            return None
        return {
            'zoho_project_id': str(r['project_id']),
            'zoho_project_no': (str(r.get('cf_project_no')) if r.get('cf_project_no') else None),
            'project_name': (r.get('project_name') or 'Untitled Zoho Project')[:500],
            'customer_name': (r.get('customer_name') or '')[:500],
            'status': r.get('status'),
            'total_project_cost': (r.get('rate') if r.get('billing_type') == 'fixed_cost_for_project' else None),
        }

ZOHO = _ZohoSync()

def _get_zoho_creds(company_id):
    row = sb.table('company_integrations').select('credentials,status').eq('company_id', company_id).eq('provider', 'zoho').execute()
    if not row.data or row.data[0].get('status') != 'connected':
        return None
    return row.data[0].get('credentials') or {}

def _sync_project_actuals(company_id, project):
    zoho_project_id = project.get('zoho_project_id')
    if not zoho_project_id:
        return [{'resource': 'all', 'status': 'error', 'records': 0, 'error': 'Project has no linked Zoho Project yet'}]

    creds = _get_zoho_creds(company_id)
    if not creds or not ZOHO.is_configured(creds):
        return [{'resource': 'all', 'status': 'error', 'records': 0, 'error': 'Zoho Books is not connected for this company'}]

    project_id = project['id']
    results = []

    def _log(resource, status, count, err=None):
        sb.table('pp_sync_logs').insert({
            'company_id': company_id, 'project_id': project_id, 'resource': resource,
            'status': status, 'records_synced': count, 'error_detail': (err or '')[:500] or None,
            'finished_at': datetime.now(timezone.utc).isoformat(),
        }).execute()
        results.append({'resource': resource, 'status': status, 'records': count, 'error': err})

    try:
        pos, po_lines = ZOHO.fetch_purchase_orders(creds, zoho_project_id)
        for po in pos:
            po['company_id'] = company_id; po['project_id'] = project_id
        if pos:
            saved = sb.table('zoho_purchase_orders').upsert(pos, on_conflict='company_id,zoho_purchase_order_id,project_id').execute()
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

    try:
        bills, bill_lines = ZOHO.fetch_bills(creds, zoho_project_id)
        for b in bills:
            b['company_id'] = company_id; b['project_id'] = project_id
        if bills:
            saved = sb.table('zoho_bills').upsert(bills, on_conflict='company_id,zoho_bill_id,project_id').execute()
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

    try:
        expenses = ZOHO.fetch_expenses(creds, zoho_project_id)
        for e_ in expenses:
            e_['company_id'] = company_id; e_['project_id'] = project_id
        if expenses:
            sb.table('zoho_expenses').upsert(expenses, on_conflict='company_id,zoho_expense_id').execute()
        _log('expenses', 'success', len(expenses))
    except Exception as e:
        _log('expenses', 'error', 0, str(e))

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


# ── Import every project that exists in Zoho Books but isn't linked to a
# local `projects` row yet. Imported projects have no originating quote, so
# they get no commercial baseline (original_selling_price/cost/gp stay 0) --
# they're actuals-tracked only until/unless a baseline is added by hand.
# Called from both the manual portfolio-wide "Sync Now" and the daily cron.
def _upsert_zoho_project(company_id, zp):
    """Create the local `projects` row for a Zoho project not seen before.
    Shared by the full portfolio import and the targeted Sync Selected
    lookup so both stay in sync on which fields get carried over."""
    insert = {
        'company_id': company_id,
        'name': zp['project_name'],
        'customer': zp['customer_name'],
        'status': 'active',
        'zoho_project_id': zp['zoho_project_id'],
        'zoho_project_no': zp.get('zoho_project_no'),
        'source': 'zoho_import',
    }
    cost = _num(zp.get('total_project_cost'))
    if cost:
        # No quote baseline exists for an imported project, so Zoho's own
        # Total Project Cost is the best available stand-in for Value --
        # revenue_forecast is what the Portfolio/detail Value figures read.
        insert['revenue_forecast'] = cost
        insert['original_selling_price'] = cost
    sb.table('projects').insert(insert).execute()

def _zoho_project_patch(row, zp):
    """Compute which fields on an *existing* local project row should be
    refreshed from newer Zoho data -- shared by the full-portfolio backfill
    and the Sync Selected backfill so both apply the same rules."""
    patch = {}
    if zp.get('zoho_project_no') and row.get('zoho_project_no') != zp['zoho_project_no']:
        patch['zoho_project_no'] = zp['zoho_project_no']
    if zp.get('project_name') and row.get('name') != zp['project_name']:
        patch['name'] = zp['project_name']
    if zp.get('customer_name') and row.get('customer') != zp['customer_name']:
        patch['customer'] = zp['customer_name']
    # Only ever set Value from Zoho's Total Project Cost for projects with
    # no quote-derived baseline (source='zoho_import') and no value set yet
    # -- never overwrite a real commercial baseline frozen from an awarded quote.
    if row.get('source') == 'zoho_import' and not _num(row.get('revenue_forecast')):
        cost = _num(zp.get('total_project_cost'))
        if cost:
            patch['revenue_forecast'] = cost
            patch['original_selling_price'] = cost
    return patch

def _mirror_zoho_no_to_quote(company_id, row, patch):
    """zoho_create_project() mirrors zoho_project_no onto the originating
    quote once, at the instant the Zoho project is created -- but Zoho often
    doesn't have cf_project_no (the human-readable Project No) assigned yet
    at that exact moment; it shows up moments later via Zoho's own
    auto-numbering. A later sync backfills it onto the `projects` row via
    _zoho_project_patch above, but that never touched the `quotes` row, so
    the quote editor kept showing the long internal Zoho ID forever even
    after the project itself had the correct Project No. Call this right
    after applying `patch` to the projects row so the quote catches up too."""
    if patch.get('zoho_project_no') and row.get('quotation_id'):
        try:
            sb.table('quotes').update({'zoho_project_no': patch['zoho_project_no']})\
                .eq('id', row['quotation_id']).eq('company_id', company_id).execute()
        except Exception:
            pass  # best-effort -- the projects-row patch above is the source of truth

def _import_zoho_projects(company_id, project_filter=None):
    """project_filter, if given, is a predicate over the fetch_all_projects
    dicts -- used by the Project No range sync so it only imports/backfills
    projects inside the requested range instead of the whole org."""
    creds = _get_zoho_creds(company_id)
    if not creds or not ZOHO.is_configured(creds):
        return {'imported': 0, 'updated': 0, 'total_in_zoho': 0, 'errors': ['Zoho Books is not connected for this company']}

    try:
        zoho_projects = ZOHO.fetch_all_projects(creds)
    except Exception as e:
        return {'imported': 0, 'updated': 0, 'total_in_zoho': 0, 'errors': [str(e)]}

    if project_filter:
        zoho_projects = [zp for zp in zoho_projects if project_filter(zp)]

    existing = sb.table('projects').select('id,zoho_project_id,zoho_project_no,name,customer,source,revenue_forecast,quotation_id').eq('company_id', company_id)\
        .not_.is_('zoho_project_id', 'null').execute().data or []
    existing_by_id = {r['zoho_project_id']: r for r in existing}

    imported, updated, errors = 0, 0, []
    for zp in zoho_projects:
        row = existing_by_id.get(zp['zoho_project_id'])
        if row:
            # Already imported before -- backfill anything we're missing
            # (Project No, name/customer changes, Value) rather than leaving
            # the row stale forever.
            patch = _zoho_project_patch(row, zp)
            if patch:
                try:
                    sb.table('projects').update(patch).eq('id', row['id']).execute()
                    _mirror_zoho_no_to_quote(company_id, row, patch)
                    updated += 1
                except Exception as e:
                    errors.append(f"{zp['zoho_project_id']}: {e}")
            continue
        try:
            _upsert_zoho_project(company_id, zp)
            imported += 1
        except Exception as e:
            errors.append(f"{zp['zoho_project_id']}: {e}")
    return {'imported': imported, 'updated': updated, 'total_in_zoho': len(zoho_projects), 'errors': errors}

# ── Targeted import for a caller-specified list of Zoho Project IDs -- used
# by the "Sync Selected" testing flow so you don't have to pull every
# project in the org just to check one or two while testing.
def _import_specific_zoho_projects(company_id, values):
    """Resolve a caller-given list that may be internal Zoho project IDs
    OR human-readable Project Nos (cf_project_no) -- whichever the user
    pasted into the Sync Selected box -- and import any that aren't already
    a local project. Tries the cheap per-ID lookup first (works when the
    value is an internal ID); anything left over is resolved by scanning
    the full Zoho project list once and matching on Project No."""
    creds = _get_zoho_creds(company_id)
    if not creds or not ZOHO.is_configured(creds):
        return {'imported': 0, 'updated': 0, 'total_in_zoho': len(values), 'errors': ['Zoho Books is not connected for this company']}

    existing = sb.table('projects').select('id,zoho_project_id,zoho_project_no,name,customer,source,revenue_forecast,quotation_id').eq('company_id', company_id).execute().data or []
    by_id = {r['zoho_project_id']: r for r in existing if r.get('zoho_project_id')}
    by_no = {r['zoho_project_no']: r for r in existing if r.get('zoho_project_no')}

    imported, updated, errors = 0, 0, []
    to_resolve = []
    for v in values:
        row = by_id.get(v) or by_no.get(v)
        if not row:
            to_resolve.append(v)
            continue
        # Already local -- refresh from Zoho in case Project No, name,
        # customer, or Value (Total Project Cost) weren't captured before.
        if row.get('zoho_project_id'):
            try:
                zp = ZOHO.fetch_project(creds, row['zoho_project_id'])
                if zp:
                    patch = _zoho_project_patch(row, zp)
                    if patch:
                        sb.table('projects').update(patch).eq('id', row['id']).execute()
                        _mirror_zoho_no_to_quote(company_id, row, patch)
                        updated += 1
            except Exception as e:
                errors.append(f'{v}: {e}')

    still_unresolved = []
    for v in to_resolve:
        try:
            zp = ZOHO.fetch_project(creds, v)
        except Exception:
            zp = None
        if zp:
            try:
                _upsert_zoho_project(company_id, zp)
                imported += 1
            except Exception as e:
                errors.append(f'{v}: {e}')
        else:
            still_unresolved.append(v)

    if still_unresolved:
        try:
            all_zp = ZOHO.fetch_all_projects(creds)
        except Exception as e:
            all_zp = []
            errors.append(f'Project No lookup failed: {e}')
        by_no_zoho = {zp['zoho_project_no']: zp for zp in all_zp if zp.get('zoho_project_no')}
        for v in still_unresolved:
            zp = by_no_zoho.get(v)
            if not zp:
                errors.append(f'{v}: not found in Zoho (tried as Project ID and Project No)')
                continue
            try:
                _upsert_zoho_project(company_id, zp)
                imported += 1
            except Exception as e:
                errors.append(f'{v}: {e}')

    return {'imported': imported, 'updated': updated, 'total_in_zoho': len(values), 'errors': errors}

# ── Manual "Sync Now" — a single project, or the whole portfolio.
# Portfolio-wide runs (no project_id) import any new Zoho projects first
# (skippable via skip_import, so a polling loop doesn't re-hit that endpoint
# every batch), then sync least-recently-synced projects first within a time
# budget. Response includes has_more/remaining so the frontend can call this
# again immediately to keep going -- see ppSyncAll() in index.html.
@app.route('/api/pp-sync/run', methods=['POST'])
def pp_run_sync():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'project_performance'): return jsonify({'error': 'Feature not enabled'}), 403

    d = request.json or {}
    company_id = claims['company_id']
    pid = d.get('project_id')
    zoho_ids_filter = [str(z).strip() for z in (d.get('zoho_project_ids') or []) if str(z).strip()]
    no_from = (d.get('zoho_project_no_from') or '').strip() or None
    no_to = (d.get('zoho_project_no_to') or '').strip() or None

    import_result = None
    is_portfolio_wide = not pid
    if pid:
        proj = sb.table('projects').select('*').eq('id', pid).eq('company_id', company_id).execute()
        if not proj.data: return jsonify({'error': 'Project not found'}), 404
        project = proj.data[0]
        allowed = can_manage(claims) or claims['user_id'] in (project.get('project_manager_id'), project.get('salesperson_id'))
        if not allowed: return jsonify({'error': 'Forbidden'}), 403
        projects = [project]
    elif zoho_ids_filter:
        # Targeted test sync -- only the named Zoho Project IDs, so testing
        # doesn't have to pull the whole org's project list every time.
        if not can_manage(claims): return jsonify({'error': 'Admin only — ask a company admin to sync.'}), 403
        import_result = _import_specific_zoho_projects(company_id, zoho_ids_filter)
        # Match on either field -- the pasted values might be internal Zoho
        # IDs or human-readable Project Nos.
        ors = ','.join([f'zoho_project_id.in.({",".join(zoho_ids_filter)})', f'zoho_project_no.in.({",".join(zoho_ids_filter)})'])
        projects = sb.table('projects').select('*').eq('company_id', company_id).or_(ors).execute().data or []
    elif no_from or no_to:
        # Testing by range: e.g. PRJ100000 to PRJ100010. Project Nos are
        # fixed-width zero-padded (PRJ + digits), so plain string comparison
        # sorts the same as numeric comparison would -- no need to parse out
        # the numeric part.
        if not can_manage(claims): return jsonify({'error': 'Admin only — ask a company admin to sync.'}), 403
        in_range = lambda zp: zp.get('zoho_project_no') \
            and (not no_from or zp['zoho_project_no'] >= no_from) \
            and (not no_to or zp['zoho_project_no'] <= no_to)
        import_result = _import_zoho_projects(company_id, project_filter=in_range)
        q = sb.table('projects').select('*').eq('company_id', company_id).not_.is_('zoho_project_no', 'null')
        if no_from: q = q.gte('zoho_project_no', no_from)
        if no_to: q = q.lte('zoho_project_no', no_to)
        projects = q.execute().data or []
    else:
        if not can_manage(claims): return jsonify({'error': 'Admin only — ask a company admin to sync the whole portfolio.'}), 403
        if not d.get('skip_import'):
            import_result = _import_zoho_projects(company_id)
        projects = sb.table('projects').select('*').eq('company_id', company_id).not_.is_('zoho_project_id', 'null').execute().data or []
        projects.sort(key=lambda p: p.get('last_synced_at') or '')

    summary = []
    processed_ids = set()
    deadline = time.time() + MANUAL_SYNC_TIME_BUDGET_SECONDS
    for project in projects:
        if not project.get('zoho_project_id'):
            continue
        if is_portfolio_wide and time.time() > deadline:
            break
        sync_results = _sync_project_actuals(company_id, project)
        calc = None
        try:
            calc = recalculate_project(company_id, project['id'])
        except Exception:
            traceback.print_exc()
        summary.append({'project_id': project['id'], 'name': project.get('name'), 'sync': sync_results, 'recalculated': bool(calc)})
        processed_ids.add(project['id'])

    remaining = len([p for p in projects if p.get('zoho_project_id') and p['id'] not in processed_ids]) if is_portfolio_wide else 0

    return jsonify({
        'ok': True, 'projects_synced': len(summary), 'results': summary, 'zoho_import': import_result,
        'remaining': remaining, 'has_more': remaining > 0,
    })

# ── Cron: run once daily via Vercel's Cron Jobs, same pattern as the
# existing customer/vendor auto-sync in api/integrations.py ────────────────
@app.route('/api/pp-sync/run-auto-sync', methods=['GET', 'POST'])
def pp_run_auto_sync():
    auth = request.headers.get('Authorization', '')
    provided = auth[7:] if auth.startswith('Bearer ') else ''
    provided = provided or request.args.get('secret') or request.headers.get('X-Cron-Secret') or ''
    if not CRON_SECRET or provided != CRON_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    companies = sb.table('companies').select('id,features').execute().data or []
    results = []
    deadline = time.time() + CRON_SYNC_TIME_BUDGET_SECONDS
    for co in companies:
        if not (co.get('features') or {}).get('project_performance'):
            continue
        if time.time() > deadline:
            break
        company_id = co['id']
        _import_zoho_projects(company_id)
        projects = sb.table('projects').select('*').eq('company_id', company_id).not_.is_('zoho_project_id', 'null').execute().data or []
        # Least-recently-synced first, so if this run can't get through the
        # whole portfolio before the deadline, whatever's left is exactly
        # what tomorrow's run will pick up first -- nothing starves.
        projects.sort(key=lambda p: p.get('last_synced_at') or '')
        for project in projects:
            if time.time() > deadline:
                break
            sync_results = _sync_project_actuals(company_id, project)
            try:
                recalculate_project(company_id, project['id'])
            except Exception:
                traceback.print_exc()
            results.append({'company_id': company_id, 'project_id': project['id'], 'sync': sync_results})
    return jsonify({'companies_processed': len(results), 'results': results})

# ── Sync status — last runs per project, for the sync-status UI ────────────
@app.route('/api/pp-sync/status', methods=['GET'])
def pp_sync_status():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'project_performance'): return jsonify({'error': 'Feature not enabled'}), 403

    company_id = claims['company_id']
    pid = request.args.get('project_id')
    q = sb.table('pp_sync_logs').select('*').eq('company_id', company_id)
    if pid: q = q.eq('project_id', pid)
    rows = q.order('started_at', desc=True).limit(50).execute()
    return jsonify({'logs': rows.data or []})

# â”€â”€ Testimonials feature (merged into projects.py to stay within Vercel's
#    Hobby-plan 12-function limit -- see api/testimonials.py history if ever
#    split back out) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
TESTIMONIAL_TOKEN_VALID_DAYS = 60
TESTIMONIAL_STATUSES = ('draft', 'pending', 'approved', 'rejected', 'published')

def _testi_esc_rating(v):
    try:
        r = int(v)
        return r if 1 <= r <= 5 else None
    except (TypeError, ValueError):
        return None

# â”€â”€ Authenticated: generate a feedback link for a completed project â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/api/testimonials/request', methods=['POST'])
def request_feedback():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'testimonials'): return jsonify({'error': 'Feature not enabled'}), 403
    d = request.json or {}
    project_id = (d.get('project_id') or '').strip()
    if not project_id:
        return jsonify({'error': 'project_id is required'}), 400

    proj = sb.table('projects').select('id,name').eq('id', project_id) \
        .eq('company_id', claims['company_id']).execute()
    if not proj.data:
        return jsonify({'error': 'Project not found'}), 404

    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=TESTIMONIAL_TOKEN_VALID_DAYS)).isoformat()

    row = sb.table('testimonials').insert({
        'company_id': claims['company_id'],
        'project_id': project_id,
        'feedback_token': token,
        'token_expires_at': expires,
        'token_used': False,
        'status': 'draft',
    }).execute()

    return jsonify({'testimonial': row.data[0], 'feedback_token': token})

# â”€â”€ Public: check a token is valid before showing the form â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/api/testimonials/verify', methods=['GET'])
def verify_feedback_token():
    token = (request.args.get('token') or '').strip()
    if not token:
        return jsonify({'error': 'Missing token'}), 400

    row = sb.table('testimonials').select('id,project_id,company_id,token_expires_at,token_used') \
        .eq('feedback_token', token).execute()
    if not row.data:
        return jsonify({'error': 'Invalid link'}), 404

    t = row.data[0]
    if t['token_used']:
        return jsonify({'error': 'This feedback link has already been used'}), 410
    if t.get('token_expires_at') and t['token_expires_at'] < datetime.now(timezone.utc).isoformat():
        return jsonify({'error': 'This feedback link has expired'}), 410

    proj = sb.table('projects').select('name').eq('id', t['project_id']).execute()
    proj_name = proj.data[0]['name'] if proj.data else 'your project'

    co = sb.table('companies').select('name').eq('id', t['company_id']).execute()
    company_name = co.data[0]['name'] if co.data else ''

    return jsonify({'valid': True, 'project_name': proj_name, 'company_name': company_name})

# â”€â”€ Public: submit the feedback form â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/api/testimonials/submit', methods=['POST'])
def submit_feedback():
    d = request.json or {}
    token = (d.get('token') or '').strip()
    if not token:
        return jsonify({'error': 'Missing token'}), 400

    row = sb.table('testimonials').select('id,token_expires_at,token_used') \
        .eq('feedback_token', token).execute()
    if not row.data:
        return jsonify({'error': 'Invalid link'}), 404

    t = row.data[0]
    if t['token_used']:
        return jsonify({'error': 'This feedback link has already been used'}), 410
    if t.get('token_expires_at') and t['token_expires_at'] < datetime.now(timezone.utc).isoformat():
        return jsonify({'error': 'This feedback link has expired'}), 410

    rating = _testi_esc_rating(d.get('rating'))
    testimonial = (d.get('testimonial') or '').strip()[:2000]
    if not testimonial:
        return jsonify({'error': 'Please enter your feedback'}), 400

    sb.table('testimonials').update({
        'client_name': (d.get('client_name') or '').strip()[:150],
        'client_title': (d.get('client_title') or '').strip()[:150],
        'rating': rating,
        'testimonial': testimonial,
        'consent_to_publish': bool(d.get('consent_to_publish')),
        'submitted_at': datetime.now(timezone.utc).isoformat(),
        'status': 'pending',
        'token_used': True,
    }).eq('id', t['id']).execute()

    return jsonify({'ok': True})

# â”€â”€ Authenticated: list submissions for review â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/api/testimonials', methods=['GET'])
def list_testimonials():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'testimonials'): return jsonify({'error': 'Feature not enabled'}), 403

    q = sb.table('testimonials').select('*').eq('company_id', claims['company_id']) \
        .order('created_at', desc=True)
    status = (request.args.get('status') or '').strip()
    if status in TESTIMONIAL_STATUSES: q = q.eq('status', status)
    rows = q.execute()
    return jsonify({'testimonials': rows.data or []})

# â”€â”€ Authenticated: approve / reject / lightly edit a submission â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/api/testimonials/<tid>', methods=['PUT'])
def update_testimonial(tid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'testimonials'): return jsonify({'error': 'Feature not enabled'}), 403
    d = request.json or {}

    existing = sb.table('testimonials').select('id,consent_to_publish') \
        .eq('id', tid).eq('company_id', claims['company_id']).execute()
    if not existing.data:
        return jsonify({'error': 'Not found'}), 404

    patch = {}
    if 'status' in d:
        s = str(d.get('status') or '').strip()
        if s not in TESTIMONIAL_STATUSES:
            return jsonify({'error': 'Invalid status'}), 400
        if s == 'published' and not existing.data[0].get('consent_to_publish'):
            return jsonify({'error': 'Cannot publish: client did not consent to publishing'}), 400
        patch['status'] = s
        patch['reviewed_by'] = claims.get('user_id')
        patch['reviewed_at'] = datetime.now(timezone.utc).isoformat()
    if 'published_text' in d:
        patch['published_text'] = (d.get('published_text') or '').strip()[:2000] or None

    if patch:
        sb.table('testimonials').update(patch).eq('id', tid).execute()

    row = sb.table('testimonials').select('*').eq('id', tid).execute()
    return jsonify({'testimonial': row.data[0] if row.data else None})

# â”€â”€ Public: what the website embed fetches â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/api/testimonials/public', methods=['GET'])
def public_testimonials():
    company_id = (request.args.get('company_id') or '').strip()
    if not company_id:
        return jsonify({'error': 'company_id is required'}), 400

    rows = sb.table('testimonials').select(
        'client_name,client_title,rating,testimonial,published_text,submitted_at'
    ).eq('company_id', company_id).eq('status', 'published') \
     .order('submitted_at', desc=True).execute()

    out = [{
        'client_name': r.get('client_name'),
        'client_title': r.get('client_title'),
        'rating': r.get('rating'),
        'text': r.get('published_text') or r.get('testimonial'),
        'date': r.get('submitted_at'),
    } for r in (rows.data or [])]

    return jsonify({'testimonials': out})

