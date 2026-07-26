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

@app.before_request
def _rbac_page_gate():
    claims = verify_token(request)
    if not claims:
        return None
    if request.method == 'GET':
        return None
    if not has_page_access(claims, 'customers'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    return None
def clean_customer(d):
    out = {}
    for k in ('name','company_name','email','phone','address','trn','notes'):
        if k in d: out[k] = str(d.get(k) or '').strip()[:500]
    return out

# ── List / search customers ────────────────────────────────────────────────────
@app.route('/api/customers', methods=['GET'])
def list_customers():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    search = (request.args.get('search') or '').strip()
    try: limit = min(100, max(1, int(request.args.get('limit', 50))))
    except: limit = 50
    try: offset = max(0, int(request.args.get('offset', 0)))
    except: offset = 0

    q = sb.table('customers').select('*', count='exact').eq('company_id', claims['company_id'])
    if search:
        s = search.replace('%',' ').replace(',',' ')
        q = q.or_(f'name.ilike.%{s}%,company_name.ilike.%{s}%,email.ilike.%{s}%,phone.ilike.%{s}%')
    rows = q.order('name').range(offset, offset + limit - 1).execute()
    return jsonify({'customers': rows.data, 'total': rows.count or 0})

# ── Create customer ─────────────────────────────────────────────────────────────
@app.route('/api/customers', methods=['POST'])
def create_customer():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    d = clean_customer(request.json or {})
    if not d.get('name'):
        return jsonify({'error': 'Name is required'}), 400
    d['company_id'] = claims['company_id']
    d['created_by'] = claims['user_id']
    d['source'] = 'manual'
    row = sb.table('customers').insert(d).execute()
    return jsonify({'customer': row.data[0]}), 201

# ── Update customer ─────────────────────────────────────────────────────────────
@app.route('/api/customers/<cid>', methods=['PUT'])
def update_customer(cid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('customers').select('id').eq('id', cid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    d = clean_customer(request.json or {})
    row = sb.table('customers').update(d).eq('id', cid).execute()
    return jsonify({'customer': row.data[0]})

# ── Delete customer ─────────────────────────────────────────────────────────────
@app.route('/api/customers/<cid>', methods=['DELETE'])
def delete_customer(cid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('customers').select('id').eq('id', cid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    sb.table('customers').delete().eq('id', cid).execute()
    return jsonify({'ok': True})

# ── Bulk import ──────────────────────────────────────────────────────────────────
@app.route('/api/customers/bulk', methods=['POST'])
def bulk_import():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    items = (request.json or {}).get('customers') or []
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'No customers provided'}), 400
    if len(items) > 1000:
        return jsonify({'error': 'Maximum 1000 customers per import'}), 400

    rows, skipped = [], 0
    for it in items:
        d = clean_customer(it or {})
        if not d.get('name'):
            skipped += 1; continue
        d['company_id'] = claims['company_id']
        d['created_by'] = claims['user_id']
        d['source'] = 'manual'
        rows.append(d)

    if not rows: return jsonify({'error': 'No valid rows found'}), 400
    sb.table('customers').insert(rows).execute()
    return jsonify({'imported': len(rows), 'skipped': skipped})

if __name__ == '__main__':
    app.run(debug=True)
