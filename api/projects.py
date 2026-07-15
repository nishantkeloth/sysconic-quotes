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

def has_feature(claims, feature):
    return bool((claims.get('features') or {}).get(feature))

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