from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt
from supabase import create_client

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

def verify_token(req):
    auth = req.headers.get('Authorization','')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

def clean_vendor(d):
    out = {}
    for k in ('name','contact_person','email','phone','address','category','notes'):
        if k in d: out[k] = str(d.get(k) or '').strip()[:500]
    return out

# ── List / search vendors ───────────────────────────────────────────────────────
@app.route('/api/vendors', methods=['GET'])
def list_vendors():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    search = (request.args.get('search') or '').strip()
    try: limit = min(100, max(1, int(request.args.get('limit', 50))))
    except: limit = 50
    try: offset = max(0, int(request.args.get('offset', 0)))
    except: offset = 0

    q = sb.table('vendors').select('*', count='exact').eq('company_id', claims['company_id'])
    if search:
        s = search.replace('%',' ').replace(',',' ')
        q = q.or_(f'name.ilike.%{s}%,category.ilike.%{s}%,contact_person.ilike.%{s}%,email.ilike.%{s}%')
    rows = q.order('name').range(offset, offset + limit - 1).execute()
    return jsonify({'vendors': rows.data, 'total': rows.count or 0})

# ── Create vendor ────────────────────────────────────────────────────────────────
@app.route('/api/vendors', methods=['POST'])
def create_vendor():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    d = clean_vendor(request.json or {})
    if not d.get('name'):
        return jsonify({'error': 'Name is required'}), 400
    d['company_id'] = claims['company_id']
    d['created_by'] = claims['user_id']
    row = sb.table('vendors').insert(d).execute()
    return jsonify({'vendor': row.data[0]}), 201

# ── Update vendor ────────────────────────────────────────────────────────────────
@app.route('/api/vendors/<vid>', methods=['PUT'])
def update_vendor(vid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('vendors').select('id').eq('id', vid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    d = clean_vendor(request.json or {})
    row = sb.table('vendors').update(d).eq('id', vid).execute()
    return jsonify({'vendor': row.data[0]})

# ── Delete vendor ────────────────────────────────────────────────────────────────
@app.route('/api/vendors/<vid>', methods=['DELETE'])
def delete_vendor(vid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('vendors').select('id').eq('id', vid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    sb.table('vendors').delete().eq('id', vid).execute()
    return jsonify({'ok': True})

# ── Bulk import ──────────────────────────────────────────────────────────────────
@app.route('/api/vendors/bulk', methods=['POST'])
def bulk_import():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    items = (request.json or {}).get('vendors') or []
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'No vendors provided'}), 400
    if len(items) > 1000:
        return jsonify({'error': 'Maximum 1000 vendors per import'}), 400

    rows, skipped = [], 0
    for it in items:
        d = clean_vendor(it or {})
        if not d.get('name'):
            skipped += 1; continue
        d['company_id'] = claims['company_id']
        d['created_by'] = claims['user_id']
        rows.append(d)

    if not rows: return jsonify({'error': 'No valid rows found'}), 400
    sb.table('vendors').insert(rows).execute()
    return jsonify({'imported': len(rows), 'skipped': skipped})

if __name__ == '__main__':
    app.run(debug=True)
