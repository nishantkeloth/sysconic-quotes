"""
api/delivery_challans.py — Delivery Challans (goods-movement documents)

Tracks AV equipment leaving the warehouse: delivery for installation, loan/
demo units, RMA returns to vendor, warehouse transfers, and returns from
site. Deliberately carries no rate/tax/amount columns -- see
migration-delivery-challans.sql for why. Wired to the same RBAC pattern as
api/fsm_engineers.py / api/fsm_tickets.py (single 'deliveryChallans' page
key, gated behind the delivery_challans feature flag).

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import uuid
import traceback
from datetime import datetime

from api.auth import verify_token, get_sb

app = Flask(__name__)

CHALLAN_TYPES = {'installation', 'loan_demo', 'rma_return', 'warehouse_transfer', 'site_return'}
STATUSES = {'draft', 'dispatched', 'delivered', 'returned', 'cancelled'}
CONDITIONS = {'new', 'refurbished', 'demo'}

# Which status a challan can move to from its current one. Keeps the
# workflow linear and stops e.g. a cancelled challan from being "delivered".
STATUS_TRANSITIONS = {
    'draft': {'dispatched', 'cancelled'},
    'dispatched': {'delivered', 'cancelled'},
    'delivered': {'returned'},
    'returned': set(),
    'cancelled': set(),
}


def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))


def has_feature(claims, feature):
    return bool((claims.get('features') or {}).get(feature))


@app.before_request
def rbac_gate():
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'unauthorized'}), 401
    if not has_feature(claims, 'delivery_challans'):
        return jsonify({'error': 'Delivery Challans is not enabled for this company'}), 403
    if not has_page_access(claims, 'deliveryChallans'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    g.claims = claims
    return None


def _next_challan_number(sb, company_id):
    year = datetime.utcnow().year
    result = sb.rpc('dc_next_challan_number', {
        'p_company_id': company_id, 'p_year': year
    }).execute()
    seq = result.data
    if isinstance(seq, list):
        seq = seq[0].get('dc_next_challan_number') if seq else 1
    return f'DC-{year}-{seq:04d}'


def _clean_item(d):
    serials = d.get('serial_numbers') or []
    if isinstance(serials, str):
        serials = [s.strip() for s in serials.split(',') if s.strip()]
    else:
        serials = [str(s).strip() for s in serials if str(s or '').strip()]
    condition = (d.get('condition') or 'new').strip()
    if condition not in CONDITIONS:
        condition = 'new'
    return {
        'product_id': d.get('product_id') or None,
        'item_name': (d.get('item_name') or '').strip()[:300],
        'brand': (d.get('brand') or '').strip()[:150] or None,
        'model_no': (d.get('model_no') or '').strip()[:150] or None,
        'quantity': float(d.get('quantity') or 1),
        'unit': (d.get('unit') or 'pcs').strip()[:30] or 'pcs',
        'condition': condition,
        'serial_numbers': serials,
        'remarks': (d.get('remarks') or '').strip()[:500] or None,
    }


def _clean_header(d, partial=False):
    out = {}
    text_fields = ('customer_name', 'delivery_address', 'warehouse', 'vehicle_number',
                   'driver_name', 'packages_count', 'received_by_name',
                   'received_by_designation', 'customer_notes', 'terms_conditions')
    for k in text_fields:
        if k in d:
            out[k] = (str(d.get(k) or '').strip() or None)
    if 'challan_type' in d:
        t = (d.get('challan_type') or '').strip()
        if t not in CHALLAN_TYPES:
            return None, f'invalid challan_type: {t}'
        out['challan_type'] = t
    elif not partial:
        out['challan_type'] = 'installation'
    if 'challan_date' in d:
        out['challan_date'] = (str(d.get('challan_date') or '').strip()[:10] or None)
    if 'expected_return_date' in d:
        out['expected_return_date'] = (str(d.get('expected_return_date') or '').strip()[:10] or None)
    for k in ('quote_id', 'project_id', 'site_id'):
        if k in d:
            out[k] = d.get(k) or None
    if 'total_weight_kg' in d:
        v = d.get('total_weight_kg')
        out['total_weight_kg'] = float(v) if v not in (None, '') else None
    return out, None


# ------------------------------------------------------------------
# List / create
# ------------------------------------------------------------------
@app.route('/api/delivery_challans', methods=['GET'])
def list_challans():
    company_id = g.claims['company_id']
    sb = get_sb()
    q = sb.table('delivery_challans').select(
        'id,challan_number,challan_type,status,challan_date,customer_name,'
        'project_id,quote_id,site_id,expected_return_date,created_at'
    ).eq('company_id', company_id)

    status = request.args.get('status')
    if status:
        q = q.eq('status', status)
    ctype = request.args.get('challan_type')
    if ctype:
        q = q.eq('challan_type', ctype)
    if request.args.get('pending_return') == '1':
        q = q.eq('challan_type', 'loan_demo').eq('status', 'delivered')
    search = (request.args.get('search') or '').strip()
    if search:
        q = q.or_(f'challan_number.ilike.%{search}%,customer_name.ilike.%{search}%')

    rows = q.order('created_at', desc=True).limit(500).execute()
    return jsonify({'challans': rows.data or []})


@app.route('/api/delivery_challans', methods=['POST'])
def create_challan():
    company_id = g.claims['company_id']
    payload = request.get_json(force=True) or {}
    header, err = _clean_header(payload)
    if err:
        return jsonify({'error': err}), 400

    items_in = payload.get('items') or []
    if not items_in:
        return jsonify({'error': 'At least one item is required'}), 400
    items = [_clean_item(i) for i in items_in]
    for it in items:
        if not it['item_name']:
            return jsonify({'error': 'Every item needs a name'}), 400

    sb = get_sb()
    try:
        challan_number = _next_challan_number(sb, company_id)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'Could not generate challan number: {e}'}), 500

    record = {
        'id': str(uuid.uuid4()),
        'company_id': company_id,
        'challan_number': challan_number,
        'status': 'draft',
        'created_by': g.claims['user_id'],
        **header,
    }
    try:
        result = sb.table('delivery_challans').insert(record).execute()
        challan = result.data[0]
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

    item_records = [{
        'id': str(uuid.uuid4()), 'company_id': company_id, 'challan_id': challan['id'],
        'sort_order': i, **it,
    } for i, it in enumerate(items)]
    try:
        sb.table('delivery_challan_items').insert(item_records).execute()
    except Exception as e:
        traceback.print_exc()
        # Don't leave an item-less draft behind if the item insert failed.
        sb.table('delivery_challans').delete().eq('id', challan['id']).execute()
        return jsonify({'error': f'Could not save items: {e}'}), 500

    challan['items'] = item_records
    return jsonify(challan), 201


# ------------------------------------------------------------------
# Get / update / delete
# ------------------------------------------------------------------
@app.route('/api/delivery_challans/<cid>', methods=['GET'])
def get_challan(cid):
    company_id = g.claims['company_id']
    sb = get_sb()
    row = sb.table('delivery_challans').select('*').eq('id', cid).eq('company_id', company_id).execute()
    if not row.data:
        return jsonify({'error': 'not found'}), 404
    challan = row.data[0]
    items = sb.table('delivery_challan_items').select('*').eq('challan_id', cid)\
        .order('sort_order').execute()
    challan['items'] = items.data or []
    return jsonify(challan)


@app.route('/api/delivery_challans/<cid>', methods=['PUT'])
def update_challan(cid):
    company_id = g.claims['company_id']
    sb = get_sb()
    existing = sb.table('delivery_challans').select('id,status').eq('id', cid).eq('company_id', company_id).execute()
    if not existing.data:
        return jsonify({'error': 'not found'}), 404
    if existing.data[0]['status'] != 'draft':
        return jsonify({'error': 'Only draft challans can be edited. Cancel and create a new one instead.'}), 409

    payload = request.get_json(force=True) or {}
    header, err = _clean_header(payload, partial=True)
    if err:
        return jsonify({'error': err}), 400
    header['updated_at'] = datetime.utcnow().isoformat()

    try:
        sb.table('delivery_challans').update(header).eq('id', cid).execute()
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

    if 'items' in payload:
        items = [_clean_item(i) for i in (payload.get('items') or [])]
        for it in items:
            if not it['item_name']:
                return jsonify({'error': 'Every item needs a name'}), 400
        sb.table('delivery_challan_items').delete().eq('challan_id', cid).execute()
        item_records = [{
            'id': str(uuid.uuid4()), 'company_id': company_id, 'challan_id': cid,
            'sort_order': i, **it,
        } for i, it in enumerate(items)]
        if item_records:
            sb.table('delivery_challan_items').insert(item_records).execute()

    return get_challan(cid)


@app.route('/api/delivery_challans/<cid>', methods=['DELETE'])
def delete_challan(cid):
    company_id = g.claims['company_id']
    sb = get_sb()
    existing = sb.table('delivery_challans').select('id,status').eq('id', cid).eq('company_id', company_id).execute()
    if not existing.data:
        return jsonify({'error': 'not found'}), 404
    if existing.data[0]['status'] != 'draft':
        return jsonify({'error': 'Only draft challans can be deleted — cancel it instead to keep the audit trail.'}), 409
    sb.table('delivery_challans').delete().eq('id', cid).execute()
    return jsonify({'ok': True})


# ------------------------------------------------------------------
# Status workflow
# ------------------------------------------------------------------
@app.route('/api/delivery_challans/<cid>/status', methods=['PUT'])
def set_status(cid):
    company_id = g.claims['company_id']
    payload = request.get_json(force=True) or {}
    new_status = (payload.get('status') or '').strip()
    if new_status not in STATUSES:
        return jsonify({'error': f'invalid status: {new_status}'}), 400

    sb = get_sb()
    existing = sb.table('delivery_challans').select('id,status').eq('id', cid).eq('company_id', company_id).execute()
    if not existing.data:
        return jsonify({'error': 'not found'}), 404
    current = existing.data[0]['status']
    if new_status not in STATUS_TRANSITIONS.get(current, set()):
        return jsonify({'error': f'Cannot move a {current} challan to {new_status}'}), 409

    update = {'status': new_status, 'updated_at': datetime.utcnow().isoformat()}
    if new_status == 'dispatched':
        update['dispatch_time'] = datetime.utcnow().isoformat()
    result = sb.table('delivery_challans').update(update).eq('id', cid).execute()
    return jsonify(result.data[0])


@app.route('/api/delivery_challans/<cid>/acknowledge', methods=['POST'])
def acknowledge_challan(cid):
    """Site acknowledgment of receipt. Typed attestation (name + designation),
    same pattern as the existing FSM work-order customer_signature_name field
    -- a drawn signature / photo upload would need file storage this app
    doesn't have yet (see migration comment)."""
    company_id = g.claims['company_id']
    payload = request.get_json(force=True) or {}
    received_by_name = (payload.get('received_by_name') or '').strip()
    if not received_by_name:
        return jsonify({'error': 'received_by_name is required'}), 400

    sb = get_sb()
    existing = sb.table('delivery_challans').select('id,status').eq('id', cid).eq('company_id', company_id).execute()
    if not existing.data:
        return jsonify({'error': 'not found'}), 404
    if existing.data[0]['status'] not in ('dispatched', 'draft'):
        return jsonify({'error': f"Cannot acknowledge a {existing.data[0]['status']} challan"}), 409

    update = {
        'received_by_name': received_by_name,
        'received_by_designation': (payload.get('received_by_designation') or '').strip() or None,
        'received_at': datetime.utcnow().isoformat(),
        'status': 'delivered',
        'updated_at': datetime.utcnow().isoformat(),
    }
    result = sb.table('delivery_challans').update(update).eq('id', cid).execute()
    return jsonify(result.data[0])


# ------------------------------------------------------------------
# Auto-create FSM assets from delivered serial numbers
# ------------------------------------------------------------------
@app.route('/api/delivery_challans/<cid>/create-assets', methods=['POST'])
def create_assets(cid):
    company_id = g.claims['company_id']
    claims = g.claims
    if not has_feature(claims, 'fsm_module'):
        return jsonify({'error': 'Field Service module is not enabled for this company'}), 403

    sb = get_sb()
    challan = sb.table('delivery_challans').select('id,site_id,status').eq('id', cid).eq('company_id', company_id).execute()
    if not challan.data:
        return jsonify({'error': 'not found'}), 404
    site_id = challan.data[0]['site_id']
    if not site_id:
        return jsonify({'error': 'This challan has no linked site — assets need a site to attach to. Set Project / Site on the challan first.'}), 400

    items = sb.table('delivery_challan_items').select('*').eq('challan_id', cid).execute().data or []
    created, skipped = [], []
    for item in items:
        already_traced = len(item.get('fsm_asset_ids') or [])
        serials = item.get('serial_numbers') or []
        if not serials or already_traced >= len(serials):
            continue
        new_asset_ids = list(item.get('fsm_asset_ids') or [])
        for serial in serials[already_traced:]:
            record = {
                'id': str(uuid.uuid4()),
                'company_id': company_id,
                'site_id': site_id,
                'asset_code': serial,
                'category': None,
                'manufacturer': item.get('brand'),
                'model': item.get('model_no'),
                'serial_number': serial,
                'status': 'active',
                'created_at': datetime.utcnow().isoformat(),
            }
            try:
                result = sb.table('fsm_assets').insert(record).execute()
                new_asset_ids.append(result.data[0]['id'])
                created.append(serial)
            except Exception as e:
                traceback.print_exc()
                skipped.append({'serial': serial, 'reason': 'already exists as an asset' if 'uq_fsm_assets_code_per_company' in str(e) else str(e)})
        sb.table('delivery_challan_items').update({'fsm_asset_ids': new_asset_ids}).eq('id', item['id']).execute()

    return jsonify({'created': created, 'skipped': skipped})
