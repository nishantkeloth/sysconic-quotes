from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt, bcrypt, uuid, re, traceback, requests
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
MS_TENANT_ID = os.environ.get('MS_TENANT_ID', 'b36855d2-9d26-43a4-bec6-82268a7713fb')
MS_CLIENT_ID = os.environ.get('MS_CLIENT_ID', '491f22c7-9dee-4c30-b828-acf8ba8d948c')
MS_CLIENT_SECRET = os.environ.get('MS_CLIENT_SECRET')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'nishant@sysconic.com')

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

def get_app_url():
    override = os.environ.get('APP_URL')
    if override:
        return override.rstrip('/')
    return request.url_root.rstrip('/')

def get_ms_token():
    url = f'https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token'
    data = {
        'client_id': MS_CLIENT_ID,
        'client_secret': MS_CLIENT_SECRET,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials'
    }
    r = requests.post(url, data=data)
    return r.json().get('access_token')

def send_invite_email(to_email, invite_url, company_name, invited_by_name, role):
    """Send invite email via Microsoft Graph. Mirrors api/auth.py's version —
    duplicated rather than imported per the Vercel single-file-per-route constraint."""
    try:
        token = get_ms_token()
        if not token:
            print('Failed to get MS token')
            return False
        subject = f"You're invited to join {company_name} on Sysconic Quote Manager"
        body = f"""
        <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1a1a1a;max-width:600px;margin:0 auto;padding:20px">
            <div style="background:#1a3c6e;padding:24px;border-radius:8px 8px 0 0;text-align:center">
                <div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:.02em">SYSCONIC</div>
                <div style="font-size:12px;color:rgba(255,255,255,.6);margin-top:4px">Quote Manager · SaaS Platform</div>
            </div>
            <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:32px">
                <h2 style="color:#1a3c6e;margin:0 0 16px">You've been invited!</h2>
                <p style="color:#555;line-height:1.6;margin-bottom:20px">
                    <strong>{invited_by_name}</strong> has invited you to join <strong>{company_name}</strong>
                    on Sysconic Quote Manager as a <strong>{role}</strong>.
                </p>
                <div style="text-align:center;margin-bottom:28px">
                    <a href="{invite_url}" style="background:#1a3c6e;color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-size:15px;font-weight:700;display:inline-block">
                        Accept Invitation →
                    </a>
                </div>
                <div style="background:#f8f9fc;border-radius:6px;padding:14px;margin-bottom:20px">
                    <p style="font-size:12px;color:#888;margin:0 0 6px">Or copy this link:</p>
                    <p style="font-size:12px;color:#1a3c6e;word-break:break-all;margin:0">{invite_url}</p>
                </div>
                <p style="font-size:12px;color:#aaa;margin:0">This invite expires in 7 days. If you didn't expect this invitation, you can ignore this email.</p>
            </div>
            <div style="text-align:center;padding:16px;font-size:11px;color:#aaa">Sysconic Technologies LLC · Abu Dhabi, UAE · www.sysconic.com</div>
        </body></html>
        """
        resp = requests.post(
            f'https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'message': {
                'subject': subject,
                'body': {'contentType': 'HTML', 'content': body},
                'toRecipients': [{'emailAddress': {'address': to_email}}],
            }}
        )
        return resp.status_code == 202
    except Exception:
        traceback.print_exc()
        return False

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
    users = sb.table('users').select('id,name,email,role,company_id').execute().data or []
    quotes = sb.table('quotes').select('id,company_id').execute().data or []

    user_counts = {}
    admins_by_company = {}
    for u in users:
        user_counts[u['company_id']] = user_counts.get(u['company_id'], 0) + 1
        if u.get('role') == 'admin':
            admins_by_company.setdefault(u['company_id'], []).append({'name': u.get('name'), 'email': u.get('email')})
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
            'admins': admins_by_company.get(co['id'], []),
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

# ── Invite a user into a specific, explicitly-chosen existing company ──────────
# This is the actual fix for data segregation: a platform admin picks the exact
# target company from the list (`cid` in the URL) rather than the invite
# implicitly landing wherever the platform admin's own company_id happens to
# point. Reuses the same `invites` table + accept-invite flow that a regular
# company admin's invite goes through, so acceptance/onboarding behaves
# identically no matter who sent the invite.
@app.route('/api/platform/companies/<cid>/invite', methods=['POST'])
def invite_to_company(cid):
    try:
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        if not require_platform_admin(claims): return jsonify(PLATFORM_ADMIN_ONLY), 403

        co = sb.table('companies').select('id,name,is_internal').eq('id', cid).execute()
        if not co.data:
            return jsonify({'error': 'Company not found'}), 404
        if co.data[0].get('is_internal'):
            return jsonify({'error': 'Cannot invite a tenant user into the internal platform company'}), 400
        company_name = co.data[0]['name']

        d = request.json or {}
        email = (d.get('email') or '').strip().lower()
        # Platform admins may only onboard Company Admins -- end users are
        # invited by the Company Admin themselves afterwards, not by the
        # Global Admin. Ignore any role the client sends; always admin.
        role = 'admin'
        if not email:
            return jsonify({'error': 'Email required'}), 400

        existing = sb.table('users').select('id').eq('email', email).execute()
        if existing.data:
            return jsonify({'error': 'That email is already registered to an account'}), 409

        token = str(uuid.uuid4())
        sb.table('invites').insert({
            'company_id': cid, 'email': email, 'role': role,
            'token': token, 'invited_by': claims['user_id'],
        }).execute()

        invite_url = f"{get_app_url()}?invite={token}"
        inviter = sb.table('users').select('name').eq('id', claims['user_id']).execute()
        inviter_name = inviter.data[0]['name'] if inviter.data else 'The Sysconic Platform Team'
        email_sent = send_invite_email(email, invite_url, company_name, inviter_name, role)

        return jsonify({
            'invite_url': invite_url,
            'email_sent': email_sent,
            'message': f"Invite {'emailed to ' + email if email_sent else 'created (email failed — share the link manually)'}"
        }), 201
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

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
