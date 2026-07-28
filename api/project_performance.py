from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import os, jwt, traceback
from supabase import create_client

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')

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


# ══ Calculation engine ═══════════════════════════════════════════════════════
# Duplicated from api/pp_sync.py — see the note there on why (Vercel's
# one-file-per-route Python builder). Used here so a forecast/completion
# edit recalculates immediately instead of waiting for the next sync.
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
    existing = sb.table('project_alerts').select('id').eq('project_id', project_id)\
        .eq('alert_type', alert_type).eq('status', 'open').execute()
    if existing.data:
        return
    sb.table('project_alerts').insert({
        'company_id': company_id, 'project_id': project_id, 'alert_type': alert_type,
        'severity': severity, 'explanation': explanation, 'financial_impact': financial_impact,
    }).execute()

def recalculate_project(company_id, project_id):
    """See api/pp_sync.py's copy of this function for the full formula
    commentary — identical logic, kept in sync by hand."""
    proj = sb.table('projects').select('*').eq('id', project_id).eq('company_id', company_id).execute()
    if not proj.data:
        return None
    p = proj.data[0]
    settings = _get_settings(company_id)

    original_cost = _num(p.get('original_estimated_cost'))
    original_sell = _num(p.get('original_selling_price'))
    original_gp_pct = _num(p.get('original_gp_pct'))
    revenue_forecast = _num(p.get('revenue_forecast')) or original_sell

    bills = sb.table('zoho_bills').select('total,status').eq('project_id', project_id).execute().data or []
    expenses = sb.table('zoho_expenses').select('amount').eq('project_id', project_id).execute().data or []
    actual_cost = sum(_num(b.get('total')) for b in bills) + sum(_num(e.get('amount')) for e in expenses)

    pos = sb.table('zoho_purchase_orders').select('total,billed_total,status').eq('project_id', project_id).execute().data or []
    committed_cost = sum(max(0.0, _num(po.get('total')) - _num(po.get('billed_total'))) for po in pos
                          if (po.get('status') or '').lower() not in ('cancelled', 'void', 'deleted'))

    forecasts = sb.table('project_forecasts').select('amount').eq('project_id', project_id).eq('status', 'active').execute().data or []
    forecast_remaining = sum(_num(f.get('amount')) for f in forecasts)

    eac = actual_cost + committed_cost + forecast_remaining
    forecast_cost_variance = original_cost - eac
    forecast_gp = revenue_forecast - eac
    forecast_gp_pct = (forecast_gp / revenue_forecast * 100.0) if revenue_forecast else 0.0
    # No-baseline guard (see api/projects.py, the routed copy): erosion vs a
    # fake 0% baseline is meaningless for Zoho-imported projects.
    has_baseline = original_cost > 0
    margin_erosion = (original_gp_pct - forecast_gp_pct) if has_baseline else 0.0

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
                   'committed_cost,estimate_at_completion,invoiced_value,collected_value,net_cash_position,'
                   'health_score,health_status,completion_pct,zoho_project_id,quote_ref,created_at')

@app.route('/api/project-performance', methods=['GET'])
def portfolio():
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
def project_detail(pid):
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
    for b_ in bills:
        pass  # bill-level category lives on bill_lines; roll up via lines below
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
def force_recalculate(pid):
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
def add_forecast(pid):
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
def revise_forecast(pid, fid):
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
    # Never overwritten silently (§36) — mark the old row revised, insert a new active one.
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
def remove_forecast(pid, fid):
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
def add_completion(pid):
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
def add_budget_revision(pid):
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
def list_cost_categories():
    claims = verify_token(request)
    err = _require_pp(claims)
    if err: return err
    rows = sb.table('project_cost_categories').select('*').eq('company_id', claims['company_id']).order('sort_order').execute()
    return jsonify({'cost_categories': rows.data or []})

@app.route('/api/project-performance/cost-categories', methods=['POST'])
def create_cost_category():
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
def list_alerts():
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
def update_alert(aid):
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

if __name__ == '__main__':
    app.run(debug=True)
