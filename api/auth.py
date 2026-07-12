from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt, bcrypt, uuid, re, traceback, requests, base64, time
import urllib.request
from datetime import datetime, timedelta
from supabase import create_client

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')
MS_TENANT_ID = os.environ.get('MS_TENANT_ID', 'b36855d2-9d26-43a4-bec6-82268a7713fb')
MS_CLIENT_ID = os.environ.get('MS_CLIENT_ID', '491f22c7-9dee-4c30-b828-acf8ba8d948c')
MS_CLIENT_SECRET = os.environ.get('MS_CLIENT_SECRET')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'nishant@sysconic.com')
def get_app_url():
    """Prefer an explicit APP_URL override; otherwise self-detect from the
    incoming request so invite/reset links always point at whichever
    environment (dev/staging/production) actually sent them. A hardcoded
    fallback here previously meant emails sent from staging could link to
    production instead, since the token they contain only exists in the
    environment that issued it."""
    override = os.environ.get('APP_URL')
    if override:
        return override.rstrip('/')
    return request.url_root.rstrip('/')

def get_sb():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def make_token(user_id, company_id, role, is_platform_admin=False):
    return jwt.encode({
        'user_id': str(user_id),
        'company_id': str(company_id),
        'role': role,
        'is_platform_admin': bool(is_platform_admin),
        'exp': datetime.utcnow() + timedelta(days=30)
    }, JWT_SECRET, algorithm='HS256')

def verify_token(req):
    auth = req.headers.get('Authorization', '')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

def slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

def get_ms_token():
    """Get Microsoft Graph access token using client credentials"""
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
    """Send invite email via Microsoft Graph"""
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
                <p style="color:#555;line-height:1.6;margin-bottom:28px">
                    Sysconic Quote Manager helps your team create professional LED wall installation 
                    quotations quickly and efficiently.
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
                <p style="font-size:12px;color:#aaa;margin:0">
                    This invite expires in 7 days. If you didn't expect this invitation, you can ignore this email.
                </p>
            </div>
            <div style="text-align:center;padding:16px;font-size:11px;color:#aaa">
                Sysconic Technologies LLC · Abu Dhabi, UAE · www.sysconic.com
            </div>
        </body></html>
        """

        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_email}}],
                "from": {"emailAddress": {"address": SENDER_EMAIL}}
            }
        }

        r = requests.post(
            f'https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail',
            json=payload,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        )
        print(f'Email send status: {r.status_code}')
        return r.status_code == 202
    except Exception as e:
        print(f'Email error: {e}')
        return False

def send_reset_email(to_email, reset_url, name):
    """Send a password reset email via Microsoft Graph"""
    try:
        token = get_ms_token()
        if not token:
            print('Failed to get MS token')
            return False

        subject = "Reset your Sysconic Quote Manager password"
        body = f"""
        <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1a1a1a;max-width:600px;margin:0 auto;padding:20px">
            <div style="background:#16294f;padding:24px;border-radius:8px 8px 0 0;text-align:center">
                <div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:.02em">SYSCONIC</div>
                <div style="font-size:12px;color:rgba(255,255,255,.6);margin-top:4px">Quote Manager · SaaS Platform</div>
            </div>
            <div style="background:#fff;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:32px">
                <h2 style="color:#16294f;margin:0 0 16px">Reset your password</h2>
                <p style="color:#555;line-height:1.6;margin-bottom:28px">
                    Hi {name or ''}, we received a request to reset your password. Click below to choose a new one.
                    This link expires in 1 hour.
                </p>
                <div style="text-align:center;margin-bottom:28px">
                    <a href="{reset_url}" style="background:#16294f;color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-size:15px;font-weight:700;display:inline-block">
                        Reset Password →
                    </a>
                </div>
                <div style="background:#f8f9fc;border-radius:6px;padding:14px;margin-bottom:20px">
                    <p style="font-size:12px;color:#888;margin:0 0 6px">Or copy this link:</p>
                    <p style="font-size:12px;color:#16294f;word-break:break-all;margin:0">{reset_url}</p>
                </div>
                <p style="font-size:12px;color:#aaa;margin:0">
                    If you didn't request this, you can safely ignore this email — your password will not change.
                </p>
            </div>
            <div style="text-align:center;padding:16px;font-size:11px;color:#aaa">
                Sysconic Technologies LLC · Abu Dhabi, UAE · www.sysconic.com
            </div>
        </body></html>
        """

        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_email}}],
                "from": {"emailAddress": {"address": SENDER_EMAIL}}
            }
        }

        r = requests.post(
            f'https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail',
            json=payload,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        )
        print(f'Reset email send status: {r.status_code}')
        return r.status_code == 202
    except Exception as e:
        print(f'Reset email error: {e}')
        return False

def create_reset_token(sb, user_id):
    token = str(uuid.uuid4())
    expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    sb.table('password_resets').insert({'user_id': user_id, 'token': token, 'expires_at': expires}).execute()
    return token

# ── Register ──────────────────────────────────────────────────────────────────
@app.route('/api/auth/register', methods=['POST'])
def register():
    try:
        sb = get_sb()
        d = request.json or {}
        email    = (d.get('email') or '').strip().lower()
        password = d.get('password', '')
        name     = d.get('name', '')
        company  = d.get('company', '')

        if not all([email, password, name, company]):
            return jsonify({'error': 'All fields required'}), 400
        if len(password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters'}), 400

        existing = sb.table('users').select('id').eq('email', email).execute()
        if existing.data:
            return jsonify({'error': 'Email already registered'}), 409

        # If this email has a pending (unexpired) invite to a company, honor it
        # instead of spinning up a brand-new tenant — otherwise someone who
        # signs up directly (rather than via the invite link) would silently
        # orphan the invite (it stays "Pending" forever) and land in their own
        # disconnected company instead of the one they were actually invited to.
        pending_invite = sb.table('invites').select('*').eq('email', email).eq('accepted', False)\
            .gt('expires_at', datetime.utcnow().isoformat()).order('created_at', desc=True).limit(1).execute()

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        if pending_invite.data:
            invite = pending_invite.data[0]
            co = sb.table('companies').select('*').eq('id', invite['company_id']).execute().data[0]
            user = sb.table('users').insert({
                'company_id': co['id'], 'email': email, 'name': name,
                'role': invite['role'], 'password_hash': pw_hash, 'invited_by': invite['invited_by']
            }).execute().data[0]
            sb.table('invites').update({'accepted': True}).eq('token', invite['token']).execute()
            token = make_token(user['id'], co['id'], user['role'], is_platform_admin=False)
            return jsonify({
                'token': token,
                'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user['role'], 'is_platform_admin': False, 'ms365_email': user.get('ms365_email')},
                'company': {'id': co['id'], 'name': co['name'], 'slug': co['slug']}
            })

        slug = slugify(company)
        slug_check = sb.table('companies').select('id').eq('slug', slug).execute()
        if slug_check.data:
            slug = slug + '-' + str(uuid.uuid4())[:4]

        co = sb.table('companies').insert({'name': company, 'slug': slug}).execute().data[0]
        user = sb.table('users').insert({
            'company_id': co['id'], 'email': email, 'name': name,
            'role': 'admin', 'password_hash': pw_hash
        }).execute().data[0]

        # Self-service signups are never platform admins — that flag can only be
        # set directly by an existing platform admin (or, for the very first one,
        # directly in the database).
        token = make_token(user['id'], co['id'], 'admin', is_platform_admin=False)
        return jsonify({
            'token': token,
            'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': 'admin', 'is_platform_admin': False, 'ms365_email': user.get('ms365_email')},
            'company': {'id': co['id'], 'name': co['name'], 'slug': co['slug']}
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ── Login ─────────────────────────────────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def login():
    try:
        sb = get_sb()
        d = request.json or {}
        email    = (d.get('email') or '').strip().lower()
        password = d.get('password', '')

        if not email or not password:
            return jsonify({'error': 'Email and password required'}), 400

        rows = sb.table('users').select('*').eq('email', email).execute()
        if not rows.data:
            return jsonify({'error': 'Invalid email or password'}), 401

        user = rows.data[0]
        pw_hash = user.get('password_hash', '')
        if not pw_hash or not bcrypt.checkpw(password.encode(), pw_hash.encode()):
            return jsonify({'error': 'Invalid email or password'}), 401

        co = sb.table('companies').select('*').eq('id', user['company_id']).execute().data[0]
        if co.get('status') == 'suspended':
            return jsonify({'error': "This company's account has been suspended. Contact your administrator."}), 403

        token = make_token(user['id'], co['id'], user['role'], user.get('is_platform_admin', False))

        return jsonify({
            'token': token,
            'user': {'id': user['id'], 'name': user.get('name', ''), 'email': user['email'], 'role': user['role'], 'is_platform_admin': bool(user.get('is_platform_admin')), 'ms365_email': user.get('ms365_email')},
            'company': {'id': co['id'], 'name': co['name'], 'slug': co['slug']}
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ── Me ────────────────────────────────────────────────────────────────────────
@app.route('/api/auth/me', methods=['GET'])
def me():
    try:
        sb = get_sb()
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        user = sb.table('users').select('id,name,email,role,company_id,is_platform_admin,ms365_email').eq('id', claims['user_id']).execute().data[0]
        co   = sb.table('companies').select('*').eq('id', claims['company_id']).execute().data[0]
        if co.get('status') == 'suspended':
            return jsonify({'error': "This company's account has been suspended. Contact your administrator."}), 403
        return jsonify({'user': user, 'company': co})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Self-service: set the Microsoft 365 mailbox this user sends from ───────────
# Separate from the app login email, which may not be a real mailbox in their
# tenant (e.g. a Gmail address used just to sign in). Any authenticated user
# can set their own — no admin gate needed, it only touches their own row.
@app.route('/api/auth/me/ms365-email', methods=['PUT'])
def set_ms365_email():
    try:
        sb = get_sb()
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        d = request.json or {}
        ms365_email = (d.get('ms365_email') or '').strip()
        if not ms365_email:
            return jsonify({'error': 'Email is required'}), 400
        sb.table('users').update({'ms365_email': ms365_email}).eq('id', claims['user_id']).execute()
        return jsonify({'ok': True, 'ms365_email': ms365_email})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Invite ────────────────────────────────────────────────────────────────────
@app.route('/api/auth/invite', methods=['POST'])
def invite():
    try:
        sb = get_sb()
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403

        d     = request.json or {}
        email = (d.get('email') or '').strip().lower()
        role  = d.get('role', 'user')

        if not email:
            return jsonify({'error': 'Email required'}), 400

        # Check if already a member
        existing = sb.table('users').select('id').eq('email', email).eq('company_id', claims['company_id']).execute()
        if existing.data:
            return jsonify({'error': 'This email is already a team member'}), 409

        token = str(uuid.uuid4())
        sb.table('invites').insert({
            'company_id': claims['company_id'],
            'email': email, 'role': role,
            'token': token, 'invited_by': claims['user_id']
        }).execute()

        invite_url = f"{get_app_url()}?invite={token}"

        # Get inviter details for email
        inviter = sb.table('users').select('name').eq('id', claims['user_id']).execute()
        inviter_name = inviter.data[0]['name'] if inviter.data else 'A team member'
        company = sb.table('companies').select('name').eq('id', claims['company_id']).execute()
        company_name = company.data[0]['name'] if company.data else 'Sysconic'

        # Send email via Microsoft 365
        email_sent = send_invite_email(email, invite_url, company_name, inviter_name, role)

        return jsonify({
            'invite_url': invite_url,
            'token': token,
            'email_sent': email_sent,
            'message': f"Invite {'sent to ' + email if email_sent else 'created (email failed — share link manually)'}"
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ── Accept invite ─────────────────────────────────────────────────────────────
@app.route('/api/auth/accept-invite', methods=['POST'])
def accept_invite():
    try:
        sb = get_sb()
        d        = request.json or {}
        token    = d.get('token', '')
        name     = d.get('name', '')
        password = d.get('password', '')

        inv = sb.table('invites').select('*').eq('token', token).eq('accepted', False).execute()
        if not inv.data:
            return jsonify({'error': 'Invalid or expired invite'}), 400

        invite = inv.data[0]
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        user = sb.table('users').insert({
            'company_id': invite['company_id'], 'email': invite['email'],
            'name': name, 'role': invite['role'],
            'password_hash': pw_hash, 'invited_by': invite['invited_by']
        }).execute().data[0]

        sb.table('invites').update({'accepted': True}).eq('token', token).execute()
        co  = sb.table('companies').select('*').eq('id', invite['company_id']).execute().data[0]
        tok = make_token(user['id'], co['id'], user['role'], is_platform_admin=False)

        return jsonify({
            'token': tok,
            'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user['role'], 'is_platform_admin': False},
            'company': {'id': co['id'], 'name': co['name']}
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ── Invite details (public — lets the accept-invite screen show who/what before
# the user commits to setting a password) ───────────────────────────────────────
@app.route('/api/auth/invite-details', methods=['GET'])
def invite_details():
    try:
        sb = get_sb()
        token = request.args.get('token', '')
        inv = sb.table('invites').select('*').eq('token', token).eq('accepted', False).execute()
        if not inv.data:
            return jsonify({'error': 'This invite link is invalid or has already been used'}), 400
        invite = inv.data[0]
        co = sb.table('companies').select('name').eq('id', invite['company_id']).execute().data[0]
        return jsonify({'email': invite['email'], 'role': invite['role'], 'company_name': co['name']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Forgot / reset password ───────────────────────────────────────────────────
# forgot-password always returns a generic success message regardless of whether
# the email exists, so this endpoint can't be used to enumerate registered users.
@app.route('/api/auth/forgot-password', methods=['POST'])
def forgot_password():
    try:
        sb = get_sb()
        email = ((request.json or {}).get('email') or '').strip().lower()
        if email:
            rows = sb.table('users').select('id,name,email').eq('email', email).execute()
            if rows.data:
                user = rows.data[0]
                token = create_reset_token(sb, user['id'])
                reset_url = f"{get_app_url()}?reset={token}"
                send_reset_email(user['email'], reset_url, user.get('name', ''))
        return jsonify({'message': "If that email is registered, we've sent a password reset link."})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    try:
        sb = get_sb()
        d = request.json or {}
        token = d.get('token', '')
        password = d.get('password', '')
        if not token:
            return jsonify({'error': 'Missing reset token'}), 400
        if len(password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters'}), 400

        rows = sb.table('password_resets').select('*').eq('token', token).eq('used', False).execute()
        if not rows.data:
            return jsonify({'error': 'This reset link is invalid or has already been used'}), 400
        reset = rows.data[0]
        from datetime import timezone
        expires_at = datetime.fromisoformat(reset['expires_at'].replace('Z', '+00:00'))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            return jsonify({'error': 'This reset link has expired. Please request a new one.'}), 400

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        sb.table('users').update({'password_hash': pw_hash}).eq('id', reset['user_id']).execute()
        sb.table('password_resets').update({'used': True}).eq('token', token).execute()
        return jsonify({'message': 'Password updated — you can now sign in.'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ── Admin-triggered reset for a team member ──────────────────────────────────
@app.route('/api/auth/team/<user_id>/reset-password', methods=['POST'])
def admin_reset_password(user_id):
    try:
        sb = get_sb()
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403

        target = sb.table('users').select('id,name,email,is_platform_admin').eq('id', user_id).eq('company_id', claims['company_id']).execute()
        if not target.data:
            return jsonify({'error': 'Team member not found'}), 404
        if target.data[0].get('is_platform_admin'):
            # Platform-admin accounts are managed at the platform level, not by a
            # tenant's company admin, even if the row happens to share a company_id.
            return jsonify({'error': 'This account is managed at the platform level'}), 403
        user = target.data[0]

        token = create_reset_token(sb, user['id'])
        reset_url = f"{get_app_url()}?reset={token}"
        email_sent = send_reset_email(user['email'], reset_url, user.get('name', ''))
        return jsonify({
            'reset_url': reset_url,
            'email_sent': email_sent,
            'message': f"Reset link {'emailed to ' + user['email'] if email_sent else 'created (email failed — share the link manually)'}"
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ── Team ──────────────────────────────────────────────────────────────────────
@app.route('/api/auth/team', methods=['GET'])
def team():
    try:
        sb = get_sb()
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403
        members = sb.table('users').select('id,name,email,role,created_at,is_platform_admin').eq('company_id', claims['company_id']).execute()
        # Platform-admin accounts are excluded even if their row happens to share
        # this company_id — they're managed at the platform level, not visible
        # to (or manageable by) a regular tenant's company admin.
        visible_members = [m for m in members.data if not m.get('is_platform_admin')]
        pending = sb.table('invites').select('token,email,role,created_at,expires_at').eq('company_id', claims['company_id']).eq('accepted', False).execute()
        return jsonify({'members': visible_members, 'pending': pending.data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Change an existing team member's role ──────────────────────────────────────
# Admin-only. Blocks demoting the company's last remaining admin, and blocks
# touching any platform-admin account entirely (see note above).
@app.route('/api/auth/team/<user_id>/role', methods=['PUT'])
def update_team_role(user_id):
    try:
        sb = get_sb()
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403

        new_role = (request.json or {}).get('role', '').strip().lower()
        if new_role not in ('admin', 'user'):
            return jsonify({'error': 'Role must be admin or user'}), 400

        target = sb.table('users').select('id,role,is_platform_admin').eq('id', user_id).eq('company_id', claims['company_id']).execute()
        if not target.data:
            return jsonify({'error': 'Team member not found'}), 404
        if target.data[0].get('is_platform_admin'):
            return jsonify({'error': 'This account is managed at the platform level'}), 403

        if target.data[0]['role'] == 'admin' and new_role != 'admin':
            admins = sb.table('users').select('id', count='exact').eq('company_id', claims['company_id']).eq('role', 'admin').eq('is_platform_admin', False).execute()
            if (admins.count or 0) <= 1:
                return jsonify({'error': 'Cannot remove the last admin. Promote someone else first.'}), 400

        row = sb.table('users').update({'role': new_role}).eq('id', user_id).execute()
        return jsonify({'user': {'id': row.data[0]['id'], 'role': row.data[0]['role']}})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ── Change the role on a pending (not-yet-accepted) invite ─────────────────────
@app.route('/api/auth/invites/<token>/role', methods=['PUT'])
def update_invite_role(token):
    try:
        sb = get_sb()
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403

        new_role = (request.json or {}).get('role', '').strip().lower()
        if new_role not in ('admin', 'user'):
            return jsonify({'error': 'Role must be admin or user'}), 400

        existing = sb.table('invites').select('id').eq('token', token).eq('company_id', claims['company_id']).eq('accepted', False).execute()
        if not existing.data:
            return jsonify({'error': 'Invite not found'}), 404

        sb.table('invites').update({'role': new_role}).eq('token', token).execute()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ── Company Profile (branding used on the Quotation PDF / proposals) ───────────
# Every company gets its own logo, address, TRN, phone, website and bank
# details instead of one hardcoded identity baked into the PDF generator —
# closes the multi-tenancy branding gap flagged in the earlier SaaS-readiness
# review. Editing is admin-only; any logged-in user can read it (needed so the
# PDF/proposal generation flow can pull it for the current company).
COMPANY_PROFILE_FIELDS = [
    'legal_name', 'address', 'trn', 'phone', 'website', 'logo_url',
    'bank_name', 'bank_account_name', 'bank_account_no', 'bank_iban', 'bank_swift', 'bank_branch',
]
LOGO_BUCKET = 'company-assets'
MAX_LOGO_BYTES = 4 * 1024 * 1024  # 4 MB

@app.route('/api/auth/company-profile', methods=['GET'])
def get_company_profile():
    try:
        sb = get_sb()
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        row = sb.table('companies').select('id,name,' + ','.join(COMPANY_PROFILE_FIELDS)).eq('id', claims['company_id']).execute()
        if not row.data: return jsonify({'error': 'Not found'}), 404
        return jsonify({'profile': row.data[0]})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/company-profile', methods=['PUT'])
def update_company_profile():
    try:
        sb = get_sb()
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403

        d = request.json or {}
        update = {}
        for f in COMPANY_PROFILE_FIELDS:
            if f in d and f != 'logo_url':  # logo is set separately via the upload endpoint
                update[f] = str(d[f] or '').strip()[:500]
        if update:
            sb.table('companies').update(update).eq('id', claims['company_id']).execute()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/company-profile/logo', methods=['POST'])
def upload_company_logo():
    try:
        sb = get_sb()
        claims = verify_token(request)
        if not claims: return jsonify({'error': 'Unauthorized'}), 401
        if claims['role'] != 'admin': return jsonify({'error': 'Admin only'}), 403

        d = request.json or {}
        raw = str(d.get('data') or '')
        if not raw: return jsonify({'error': 'No image data provided'}), 400
        if ',' in raw: raw = raw.split(',', 1)[1]  # strip data: prefix
        try:
            data = base64.b64decode(raw)
        except Exception:
            return jsonify({'error': 'Invalid image data'}), 400

        ctype = str(d.get('content_type') or 'image/png').split(';')[0].strip().lower()
        ext = {'image/jpeg':'jpg','image/jpg':'jpg','image/png':'png','image/webp':'webp'}.get(ctype)
        if not ext:
            return jsonify({'error': f'Unsupported image type ({ctype or "unknown"}). Use JPG, PNG or WEBP.'}), 400
        if len(data) > MAX_LOGO_BYTES:
            return jsonify({'error': 'Logo is larger than 4 MB. Please use a smaller image.'}), 400

        path = f"{claims['company_id']}/logo-{int(time.time())}.{ext}"
        try:
            sb.storage.from_(LOGO_BUCKET).upload(path, data, {'content-type': ctype})
        except Exception:
            return jsonify({'error': 'Could not save logo to storage. Check that the company-assets bucket exists and is public.'}), 502

        public_url = sb.storage.from_(LOGO_BUCKET).get_public_url(path)
        if isinstance(public_url, str): public_url = public_url.rstrip('?')

        sb.table('companies').update({'logo_url': public_url}).eq('id', claims['company_id']).execute()
        return jsonify({'logo_url': public_url})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
