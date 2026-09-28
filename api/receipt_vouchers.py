"""
api/receipt_vouchers.py — Receipt Vouchers (money received)

The counterpart to api/vouchers.py (Payment Vouchers, money paid out).
Deliberately has no approval workflow -- an accountant records a receipt and
it's immediately final, same as tearing one out of a physical receipt book
(see the requirements discussion in chat: "create & print directly" was
chosen over routing through the same approver workflow as Payment
Vouchers). Gated behind the same 'vouchers' feature flag as Payment
Vouchers, with its own RBAC page key ('receiptVouchers') so access can be
granted independently -- e.g. a front-desk/sales-coordinator role could get
Receipt Vouchers without also getting Payment Voucher approval rights.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import uuid
import traceback
from datetime import datetime

from api.auth import verify_token, get_sb

app = Flask(__name__)

PAYMENT_METHODS = {'cash', 'cheque', 'bank_transfer', 'card'}


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
    # Same feature flag as Payment Vouchers (api/vouchers.py) -- a company
    # that already has Payment Vouchers turned on gets Receipt Vouchers too,
    # no separate admin toggle needed.
    if not has_feature(claims, 'vouchers'):
        return jsonify({'error': 'Vouchers is not enabled for this company'}), 403
    if not has_page_access(claims, 'receiptVouchers'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    g.claims = claims
    return None


# Prefix/padding are per-company settings (defaults 'RV'/4 apply with no
# settings row needed, giving RV-2026-0001 out of the box); the running
# number itself lives in receipt_voucher_counters and is only ever touched
# atomically via rv_next_receipt_number() -- never read-then-write from
# Python, so concurrent voucher creation can't hand out the same number
# twice. Same pattern as _numbering_settings()/_next_challan_number() in
# api/delivery_challans.py.
def _numbering_settings(company_id):
    sb = get_sb()
    row = sb.table('rv_numbering_settings').select('prefix,padding').eq('company_id', company_id).execute()
    if row.data:
        return row.data[0].get('prefix') or 'RV', row.data[0].get('padding') or 4
    return 'RV', 4


def _next_receipt_number(sb, company_id):
    year = datetime.utcnow().year
    prefix, padding = _numbering_settings(company_id)
    result = sb.rpc('rv_next_receipt_number', {
        'p_company_id': company_id, 'p_year': year
    }).execute()
    seq = result.data
    if isinstance(seq, list):
        seq = seq[0].get('rv_next_receipt_number') if seq else 1
    return f'{prefix}-{year}-{str(seq).zfill(padding)}'


def _clean_header(d, partial=False):
    out = {}
    text_fields = ('received_from', 'currency', 'cheque_number', 'bank_name', 'notes')
    for k in text_fields:
        if k in d:
            out[k] = (str(d.get(k) or '').strip() or None)
    if not partial and not out.get('received_from'):
        return None, 'received_from is required'
    if 'currency' in d and not out.get('currency'):
        out['currency'] = 'AED'

    if 'receipt_date' in d:
        out['receipt_date'] = (str(d.get('receipt_date') or '').strip()[:10] or None)

    if 'amount' in d:
        try:
            amount = float(d.get('amount'))
        except (TypeError, ValueError):
            return None, 'amount must be a number'
        if amount <= 0:
            return None, 'amount must be greater than 0'
        out['amount'] = amount
    elif not partial:
        return None, 'amount is required'

    if 'payment_method' in d:
        pm = (d.get('payment_method') or 'cash').strip()
        if pm not in PAYMENT_METHODS:
            return None, f'invalid payment_method: {pm}'
        out['payment_method'] = pm

    for k in ('project_id', 'customer_id'):
        if k in d:
            out[k] = d.get(k) or None

    return out, None


# ------------------------------------------------------------------
# Numbering settings (admin only) — mirrors GET/PATCH /api/delivery_challans/numbering
# ------------------------------------------------------------------
@app.route('/api/receipt_vouchers/numbering', methods=['GET'])
def get_rv_numbering():
    claims = g.claims
    if claims.get('role') != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    company_id = claims['company_id']
    prefix, padding = _numbering_settings(company_id)
    sb = get_sb()
    rows = sb.table('receipt_voucher_counters').select('year,seq').eq('company_id', company_id).order('year').execute()
    # UI works in "next number to hand out" terms (like Delivery Challans);
    # the counter table stores "last number handed out", so translate
    # seq -> seq + 1.
    sequences = [{'year': r['year'], 'next_number': (r['seq'] or 0) + 1} for r in (rows.data or [])]
    return jsonify({
        'prefix': prefix, 'padding': padding,
        'current_year': datetime.utcnow().year, 'sequences': sequences,
    })


@app.route('/api/receipt_vouchers/numbering', methods=['PATCH'])
def update_rv_numbering():
    claims = g.claims
    if claims.get('role') != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    company_id = claims['company_id']
    sb = get_sb()
    d = request.get_json(force=True) or {}

    if 'prefix' in d or 'padding' in d:
        prefix = (d.get('prefix') or 'RV').strip()[:20] or 'RV'
        try:
            padding = max(1, min(6, int(d.get('padding') or 4)))
        except (TypeError, ValueError):
            padding = 4
        sb.table('rv_numbering_settings').upsert({
            'company_id': company_id, 'prefix': prefix, 'padding': padding,
            'updated_by': claims['user_id'], 'updated_at': datetime.utcnow().isoformat(),
        }, on_conflict='company_id').execute()

    if 'year' in d and 'next_number' in d:
        try:
            year = int(d['year'])
            next_number = int(d['next_number'])
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid year or next_number'}), 400
        if next_number < 1:
            return jsonify({'error': 'next_number must be at least 1'}), 400
        # rv_next_receipt_number() does seq+1 then returns it, so storing
        # next_number-1 here makes the *next* call hand out next_number.
        sb.table('receipt_voucher_counters').upsert({
            'company_id': company_id, 'year': year, 'seq': next_number - 1,
        }, on_conflict='company_id,year').execute()

    return get_rv_numbering()


# ------------------------------------------------------------------
# List / create
# ------------------------------------------------------------------
@app.route('/api/receipt_vouchers', methods=['GET'])
def list_receipts():
    company_id = g.claims['company_id']
    sb = get_sb()
    q = sb.table('receipt_vouchers').select(
        'id,receipt_number,receipt_date,received_from,amount,currency,'
        'payment_method,project_id,customer_id,created_at'
    ).eq('company_id', company_id)

    search = (request.args.get('search') or '').strip()
    if search:
        q = q.or_(f'receipt_number.ilike.%{search}%,received_from.ilike.%{search}%')
    project_id = request.args.get('project_id')
    if project_id:
        q = q.eq('project_id', project_id)

    rows = q.order('receipt_date', desc=True).order('created_at', desc=True).limit(500).execute()
    return jsonify({'receipts': rows.data or []})


@app.route('/api/receipt_vouchers', methods=['POST'])
def create_receipt():
    company_id = g.claims['company_id']
    payload = request.get_json(force=True) or {}
    header, err = _clean_header(payload)
    if err:
        return jsonify({'error': err}), 400

    sb = get_sb()
    try:
        receipt_number = _next_receipt_number(sb, company_id)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'Could not generate receipt number: {e}'}), 500

    record = {
        'id': str(uuid.uuid4()),
        'company_id': company_id,
        'receipt_number': receipt_number,
        'currency': header.get('currency') or 'AED',
        'payment_method': header.get('payment_method') or 'cash',
        'created_by': g.claims['user_id'],
        **{k: v for k, v in header.items() if k not in ('currency', 'payment_method')},
    }
    try:
        result = sb.table('receipt_vouchers').insert(record).execute()
        return jsonify(result.data[0]), 201
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------------
# Get / update / delete
# ------------------------------------------------------------------
@app.route('/api/receipt_vouchers/<rid>', methods=['GET'])
def get_receipt(rid):
    company_id = g.claims['company_id']
    sb = get_sb()
    row = sb.table('receipt_vouchers').select('*').eq('id', rid).eq('company_id', company_id).execute()
    if not row.data:
        return jsonify({'error': 'not found'}), 404
    return jsonify(row.data[0])


@app.route('/api/receipt_vouchers/<rid>', methods=['PUT'])
def update_receipt(rid):
    company_id = g.claims['company_id']
    sb = get_sb()
    existing = sb.table('receipt_vouchers').select('id').eq('id', rid).eq('company_id', company_id).execute()
    if not existing.data:
        return jsonify({'error': 'not found'}), 404

    payload = request.get_json(force=True) or {}
    header, err = _clean_header(payload, partial=True)
    if err:
        return jsonify({'error': err}), 400
    if not header:
        return jsonify({'error': 'Nothing to update'}), 400
    header['updated_at'] = datetime.utcnow().isoformat()

    try:
        sb.table('receipt_vouchers').update(header).eq('id', rid).execute()
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    return get_receipt(rid)


@app.route('/api/receipt_vouchers/<rid>', methods=['DELETE'])
def delete_receipt(rid):
    company_id = g.claims['company_id']
    sb = get_sb()
    existing = sb.table('receipt_vouchers').select('id').eq('id', rid).eq('company_id', company_id).execute()
    if not existing.data:
        return jsonify({'error': 'not found'}), 404
    sb.table('receipt_vouchers').delete().eq('id', rid).execute()
    return jsonify({'ok': True})
