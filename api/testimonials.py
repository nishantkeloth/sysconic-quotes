from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import os, jwt, traceback, secrets
from datetime import datetime, timezone, timedelta
from supabase import create_client

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

TOKEN_VALID_DAYS = 60
STATUSES = ('draft', 'pending', 'approved', 'rejected', 'published')

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
    except:
        return None

def has_feature(claims, feature):
    return bool((claims.get('features') or {}).get(feature))

def esc_rating(v):
    try:
        r = int(v)
        return r if 1 <= r <= 5 else None
    except (TypeError, ValueError):
        return None

# ── Authenticated: generate a feedback link for a completed project ────────
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
    expires = (datetime.now(timezone.utc) + timedelta(days=TOKEN_VALID_DAYS)).isoformat()

    row = sb.table('testimonials').insert({
        'company_id': claims['company_id'],
        'project_id': project_id,
        'feedback_token': token,
        'token_expires_at': expires,
        'token_used': False,
        'status': 'draft',
    }).execute()

    return jsonify({'testimonial': row.data[0], 'feedback_token': token})

# ── Public: check a token is valid before showing the form ──────────────────
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

# ── Public: submit the feedback form ────────────────────────────────────────
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

    rating = esc_rating(d.get('rating'))
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

# ── Authenticated: list submissions for review ──────────────────────────────
@app.route('/api/testimonials', methods=['GET'])
def list_testimonials():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_feature(claims, 'testimonials'): return jsonify({'error': 'Feature not enabled'}), 403

    q = sb.table('testimonials').select('*').eq('company_id', claims['company_id']) \
        .order('created_at', desc=True)
    status = (request.args.get('status') or '').strip()
    if status in STATUSES: q = q.eq('status', status)
    rows = q.execute()
    return jsonify({'testimonials': rows.data or []})

# ── Authenticated: approve / reject / lightly edit a submission ────────────
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
        if s not in STATUSES:
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

# ── Public: what the website embed fetches ──────────────────────────────────
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