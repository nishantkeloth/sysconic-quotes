from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt, json, time
import urllib.request, urllib.error, urllib.parse

app = Flask(__name__)
CORS(app)

JWT_SECRET         = os.environ.get('JWT_SECRET')
ZOHO_CLIENT_ID     = os.environ.get('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.environ.get('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.environ.get('ZOHO_REFRESH_TOKEN')
ZOHO_ORG_ID        = os.environ.get('ZOHO_ORG_ID')

ZOHO_ACCOUNTS = 'https://accounts.zoho.com'   # .com data center
ZOHO_API      = 'https://www.zohoapis.com/books/v3'

def verify_token(req):
    auth = req.headers.get('Authorization','')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

# Access-token cache — survives across requests on a warm serverless instance
_tok = {'value': None, 'exp': 0}

def zoho_access_token():
    if _tok['value'] and time.time() < _tok['exp'] - 60:
        return _tok['value']
    params = urllib.parse.urlencode({
        'refresh_token': ZOHO_REFRESH_TOKEN,
        'client_id':     ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SECRET,
        'grant_type':    'refresh_token',
    }).encode('utf-8')
    req = urllib.request.Request(ZOHO_ACCOUNTS + '/oauth/v2/token', data=params, method='POST')
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    if 'access_token' not in data:
        raise RuntimeError('Zoho token refresh failed: ' + json.dumps(data)[:200])
    _tok['value'] = data['access_token']
    _tok['exp']   = time.time() + int(data.get('expires_in', 3600))
    return _tok['value']

def zoho_get(path, params):
    params = dict(params or {})
    params['organization_id'] = ZOHO_ORG_ID
    url = f"{ZOHO_API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={'Authorization': 'Zoho-oauthtoken ' + zoho_access_token()})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode('utf-8'))

def zoho_ready():
    return all([ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID])

# ── Connection status (for quick verification) ────────────────────────────────
@app.route('/api/zoho/status', methods=['GET'])
def status():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not zoho_ready():
        return jsonify({'error': 'Zoho is not configured (missing ZOHO_* environment variables)'}), 500
    try:
        data = zoho_get('/contacts', {'contact_type': 'customer', 'per_page': 1})
        total = (data.get('page_context') or {}).get('total', 0)
        return jsonify({'ok': True, 'customers_in_zoho': total})
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'ignore')[:200]
        return jsonify({'error': f'Zoho API error ({e.code}): {detail}'}), 502
    except Exception as e:
        return jsonify({'error': f'Zoho connection failed: {str(e)[:200]}'}), 502

# ── Customer search (autocomplete) ────────────────────────────────────────────
@app.route('/api/zoho/customers', methods=['GET'])
def customers():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not zoho_ready():
        return jsonify({'error': 'Zoho is not configured'}), 500

    search = (request.args.get('search') or '').strip()
    if len(search) < 2:
        return jsonify({'customers': []})

    try:
        data = zoho_get('/contacts', {
            'contact_type': 'customer',
            'search_text': search,
            'per_page': 10,
            'sort_column': 'contact_name',
        })
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _tok['value'] = None  # force token refresh next call
        detail = e.read().decode('utf-8', 'ignore')[:200]
        return jsonify({'error': f'Zoho API error ({e.code})'}), 502
    except Exception:
        return jsonify({'error': 'Zoho is unreachable right now'}), 502

    out = []
    for ct in data.get('contacts', [])[:10]:
        out.append({
            'id':      ct.get('contact_id'),
            'name':    ct.get('contact_name') or ct.get('company_name') or '',
            'company': ct.get('company_name') or '',
            'email':   ct.get('email') or '',
            'phone':   ct.get('phone') or ct.get('mobile') or '',
        })
    return jsonify({'customers': out})

if __name__ == '__main__':
    app.run(debug=True)
