# Zoho Books adapter — one of possibly several integration providers.
# Deliberately takes credentials as a parameter rather than reading global
# env vars, so each company can connect their own Zoho org (see
# api/integrations.py, which looks up per-company credentials from the
# company_integrations table before calling into this module).
#
# This file defines no Flask routes/app — it's a plain shared module imported
# by api/integrations.py.

import json, time
import urllib.request, urllib.error, urllib.parse

ZOHO_ACCOUNTS = 'https://accounts.zoho.com'   # .com data center
ZOHO_API      = 'https://www.zohoapis.com/books/v3'

REQUIRED_FIELDS = ['client_id', 'client_secret', 'refresh_token', 'org_id']

def is_configured(creds):
    creds = creds or {}
    return all(creds.get(f) for f in REQUIRED_FIELDS)

# Access-token cache keyed by refresh_token, so multiple companies' tokens
# don't collide on a warm serverless instance.
_tok_cache = {}

def _access_token(creds):
    key = creds['refresh_token']
    cached = _tok_cache.get(key)
    if cached and time.time() < cached['exp'] - 60:
        return cached['value']
    params = urllib.parse.urlencode({
        'refresh_token': creds['refresh_token'],
        'client_id':     creds['client_id'],
        'client_secret': creds['client_secret'],
        'grant_type':    'refresh_token',
    }).encode('utf-8')
    req = urllib.request.Request(ZOHO_ACCOUNTS + '/oauth/v2/token', data=params, method='POST')
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    if 'access_token' not in data:
        raise RuntimeError('Zoho token refresh failed: ' + json.dumps(data)[:200])
    _tok_cache[key] = {'value': data['access_token'], 'exp': time.time() + int(data.get('expires_in', 3600))}
    return data['access_token']

def _get(creds, path, params):
    params = dict(params or {})
    params['organization_id'] = creds['org_id']
    url = f"{ZOHO_API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={'Authorization': 'Zoho-oauthtoken ' + _access_token(creds)})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode('utf-8'))

def _fetch_contacts(creds, contact_type):
    out, page = [], 1
    while page <= 10:  # safety cap: 10 pages x 200 = 2000 contacts
        data = _get(creds, '/contacts', {'contact_type': contact_type, 'per_page': 200,
                                          'page': page, 'sort_column': 'contact_name'})
        for ct in data.get('contacts', []):
            out.append({
                'id':      ct.get('contact_id'),
                'name':    ct.get('contact_name') or ct.get('company_name') or '',
                'company': ct.get('company_name') or '',
                'email':   ct.get('email') or '',
                'phone':   ct.get('phone') or ct.get('mobile') or '',
            })
        if not (data.get('page_context') or {}).get('has_more_page'):
            break
        page += 1
    return out

def fetch_customers(creds):
    return _fetch_contacts(creds, 'customer')

def fetch_vendors(creds):
    return _fetch_contacts(creds, 'vendor')

def test_connection(creds):
    """Used by the 'Connect' flow to verify credentials before saving as connected."""
    contacts = fetch_customers(creds)
    return {'ok': True, 'sample_count': len(contacts)}
