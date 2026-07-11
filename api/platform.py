from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt, bcrypt, uuid, re, traceback
from supabase import create_client

# ── Global (platform-level) admin panel ──────────────────────────────────────────
# This is a different trust boundary from the per-company `admin` role used
# everywhere else in the app: a platform admin can see and act across EVERY
# company, not just their own. It's a boolean flag (`is_platform_admin`) on a
# user row, embedded in their JWT at login (see api/auth.py), rather than a
# separate login system — reuses the same auth the rest of the app already has.
#
# Self-contained (no cross-file imports) for the same reason every other route
# file here is: Vercel's Python builder only bundles the single entrypoint file
# per route, so shared helpers get duplicated rather than imported.

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

def verify_token(req):
    auth = req.headers.get('Authorization', '')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

def require_platform_admin(claims):
    return bool(claims) and bool(claims.get('is_platform_admin'))

PLATFORM_ADMIN_ONLY = {'error': 'Platform admin only.'}

def slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

# ── Internal/platform company (houses platform-admin accounts only) ────────────
# Standard SaaS pattern: platform staff shouldn't belong to any real customer
# tenant. Since `users.company_id` is NOT NULL in the schema, every user still
# needs *some* company row — so instead of parking platform admins inside an
# actual customer's company (which is what happened before this fix, and is
# why a tenant's own Team page could see/reset/promote the platform account),
# they get their own dedicated, non-customer company that's flagged
# `is_internal = true` and excluded from every customer-facing list.
INTERNAL_COMPANY_NAME = 'Sysconic Platform (Internal)'
INTERNAL_COMPANY_SLUG = 'sysconic-platform-internal'

def get_or_create_internal_company():
    row = sb.table('companies').select('id').eq('is_internal', True).limit(1).execute()
    if row.data:
        return row.data[0]['id']
    co = sb.table('companies').insert({
        'name': INTERNAL_COMPANY_NAME, 'slug': INTERNAL_COMPANY_SLUG,
        'plan': 'internal', 'status': 'active', 'is_internal': True,
    }).execute().data[0]
    return co['id']

# ── List every company on the platform, with basic usage counts ────────────────
@app.route('/api/platform/companies', methods=['GET'])
def list_companies():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not require_platform_admin(claims): return jsonify(PLATFORM_ADMIN_ONLY), 403

    companies = sb.table('companies').select('*').eq('is_internal', False).order('created_at', desc=True).execute().data or []
    users = sb.table('users').select('id,company_id').execute().data or []
    quotes = sb.table('quotes').select('id,company_id').execute().data or []

    user_counts = {}
    for u in users:
        user_counts[u['company_id']] = user_counts.get(u['company_id'], 0) + 1
    quote_counts = {}
    for q in quotes:
        quote_counts[q['company_id']] = quote_counts.get(q['company_id'], 0) + 1

    out = []
    for co in companies:
        out.append({
            'id': co['id'], 'name': co['name'], 'slug': co.get('slug'),
            'plan': co.get('plan', 'free'), 'status': co.get('status', 'active'),
            'created_at': co.get('created_at'),
            'user_count': user_counts.get(co['id'], 0),
            'quote_count': quote_counts.get(co['id'], 0),
        })
    return jsonify({'companies': out})

# ── Onboard a new company (manual, by a platform admin) ─────────────────────────
@app.route('/api/platform/companies', methods=['POST'])
def create_company():
    try:
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        if not require_platform_admin(claims): return jsonify(PLATFORM_ADMIN_ONLY), 403

        d = request.json or {}
        company_name  = (d.get('company_name') or '').strip()
        admin_name    = (d.get('admin_name') or '').strip()
        admin_email   = (d.get('admin_email') or '').strip().lower()
        admin_password = d.get('admin_password') or ''
        plan = (d.get('plan') or 'free').strip().lower()

        if not all([company_name, admin_name, admin_email, admin_password]):
            return jsonify({'error': 'Company name, admin name, admin email and a password are all required'}), 400
        if len(admin_password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters'}), 400

        existing = sb.table('users').select('id').eq('email', admin_email).execute()
        if existing.data:
            return jsonify({'error': 'That email is already registered to an account'}), 409

        slug = slugify(company_name)
        slug_check = sb.table('companies').select('id').eq('slug', slug).execute()
        if slug_check.data:
            slug = slug + '-' + str(uuid.uuid4())[:4]

        co = sb.table('companies').insert({'name': company_name, 'slug': slug, 'plan': plan, 'status': 'active'}).execute().data[0]
        pw_hash = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode()
        user = sb.table('users').insert({
            'company_id': co['id'], 'email': admin_email, 'name': admin_name,
            'role': 'admin', 'password_hash': pw_hash, 'is_platform_admin': False,
        }).execute().data[0]

        return jsonify({
            'company': {'id': co['id'], 'name': co['name'], 'slug': co['slug'], 'plan': co['plan']},
            'admin_user': {'id': user['id'], 'name': user['name'], 'email': user['email']},
        }), 201
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ── Suspend / reactivate a company's access ─────────────────────────────────────
@app.route('/api/platform/companies/<cid>/suspend', methods=['POST'])
def suspend_company(cid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not require_platform_admin(claims): return jsonify(PLATFORM_ADMIN_ONLY), 403
    sb.table('companies').update({'status': 'suspended'}).eq('id', cid).execute()
    return jsonify({'ok': True})

@app.route('/api/platform/companies/<cid>/reactivate', methods=['POST'])
def reactivate_company(cid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not require_platform_admin(claims): return jsonify(PLATFORM_ADMIN_ONLY), 403
    sb.table('companies').update({'status': 'active'}).eq('id', cid).execute()
    return jsonify({'ok': True})

# ── Create a new platform-admin account ─────────────────────────────────────────
# Always assigns the internal company, never a real tenant's company_id — the
# structural fix for how "Global Admin" ended up inside Sysconic Technologies'
# own company in the first place.
@app.route('/api/platform/admins', methods=['POST'])
def create_platform_admin():
    try:
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        if not require_platform_admin(claims): return jsonify(PLATFORM_ADMIN_ONLY), 403

        d = request.json or {}
        name = (d.get('name') or '').strip()
        email = (d.get('email') or '').strip().lower()
        password = d.get('password') or ''

        if not all([name, email, password]):
            return jsonify({'error': 'Name, email and a password are all required'}), 400
        if len(password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters'}), 400

        existing = sb.table('users').select('id').eq('email', email).execute()
        if existing.data:
            return jsonify({'error': 'That email is already registered to an account'}), 409

        internal_company_id = get_or_create_internal_company()
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        user = sb.table('users').insert({
            'company_id': internal_company_id, 'email': email, 'name': name,
            'role': 'admin', 'password_hash': pw_hash, 'is_platform_admin': True,
        }).execute().data[0]

        return jsonify({'admin_user': {'id': user['id'], 'name': user['name'], 'email': user['email']}}), 201
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ── List platform-admin accounts ────────────────────────────────────────────────
@app.route('/api/platform/admins', methods=['GET'])
def list_platform_admins():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not require_platform_admin(claims): return jsonify(PLATFORM_ADMIN_ONLY), 403
    rows = sb.table('users').select('id,name,email,created_at').eq('is_platform_admin', True).execute()
    return jsonify({'admins': rows.data or []})

if __name__ == '__main__':
    app.run(debug=True)
