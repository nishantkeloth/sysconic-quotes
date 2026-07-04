from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt
from datetime import datetime
from supabase import create_client

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET', 'sysconic-quotes-secret-2026')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

def verify_token(req):
    auth = req.headers.get('Authorization','')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

# ── List quotes ────────────────────────────────────────────────────────────────
@app.route('/api/quotes', methods=['GET'])
def list_quotes():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    rows = sb.table('quotes')\
        .select('id,title,customer,status,currency,total_sell,total_gp,margin,created_at,updated_at,created_by')\
        .eq('company_id', claims['company_id'])\
        .order('updated_at', desc=True)\
        .execute()
    return jsonify({'quotes': rows.data})

# ── Get single quote ───────────────────────────────────────────────────────────
@app.route('/api/quotes/<qid>', methods=['GET'])
def get_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    row = sb.table('quotes').select('*').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Not found'}), 404
    return jsonify({'quote': row.data[0]})

# ── Create quote ───────────────────────────────────────────────────────────────
@app.route('/api/quotes', methods=['POST'])
def create_quote():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    d = request.json
    row = sb.table('quotes').insert({
        'company_id':    claims['company_id'],
        'created_by':    claims['user_id'],
        'title':         d.get('title', 'Untitled Quote'),
        'customer':      d.get('customer', ''),
        'status':        d.get('status', 'draft'),
        'currency':      d.get('currency', 'AED'),
        'exchange_rate': d.get('exchange_rate', 1),
        'quote_data':    d.get('quote_data', []),
        'vendor_data':   d.get('vendor_data', []),
        'terms_data':    d.get('terms_data', []),
        'total_sell':    d.get('total_sell', 0),
        'total_gp':      d.get('total_gp', 0),
        'margin':        d.get('margin', 0),
    }).execute()
    return jsonify({'quote': row.data[0]}), 201

# ── Update quote ───────────────────────────────────────────────────────────────
@app.route('/api/quotes/<qid>', methods=['PUT'])
def update_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    # Check ownership
    existing = sb.table('quotes').select('id,created_by').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404
    if claims['role'] != 'admin' and existing.data[0]['created_by'] != claims['user_id']:
        return jsonify({'error': 'Forbidden'}), 403

    d = request.json
    allowed = ['title','customer','status','currency','exchange_rate','quote_data','vendor_data','terms_data','total_sell','total_gp','margin']
    update = {k: d[k] for k in allowed if k in d}

    row = sb.table('quotes').update(update).eq('id', qid).execute()
    return jsonify({'quote': row.data[0]})

# ── Delete quote ───────────────────────────────────────────────────────────────
@app.route('/api/quotes/<qid>', methods=['DELETE'])
def delete_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('quotes').select('id,created_by').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404
    if claims['role'] != 'admin' and existing.data[0]['created_by'] != claims['user_id']:
        return jsonify({'error': 'Forbidden'}), 403

    sb.table('quotes').delete().eq('id', qid).execute()
    return jsonify({'ok': True})

# ── Duplicate quote ────────────────────────────────────────────────────────────
@app.route('/api/quotes/<qid>/duplicate', methods=['POST'])
def duplicate_quote(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    orig = sb.table('quotes').select('*').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not orig.data: return jsonify({'error': 'Not found'}), 404

    o = orig.data[0]
    row = sb.table('quotes').insert({
        'company_id':    claims['company_id'],
        'created_by':    claims['user_id'],
        'title':         o['title'] + ' (Copy)',
        'customer':      o['customer'],
        'status':        'draft',
        'currency':      o['currency'],
        'exchange_rate': o['exchange_rate'],
        'quote_data':    o['quote_data'],
        'vendor_data':   o['vendor_data'],
        'terms_data':    o['terms_data'],
        'total_sell':    o['total_sell'],
        'total_gp':      o['total_gp'],
        'margin':        o['margin'],
    }).execute()
    return jsonify({'quote': row.data[0]}), 201

if __name__ == '__main__':
    app.run(debug=True)
