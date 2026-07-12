from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import os, jwt, json, time, re, socket, ipaddress, traceback
import urllib.request, urllib.error, urllib.parse
from supabase import create_client

app = Flask(__name__)
CORS(app)

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    return jsonify({'error': str(e)}), 500

SUPABASE_URL   = os.environ.get('SUPABASE_URL')
SUPABASE_KEY   = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET     = os.environ.get('JWT_SECRET')
GOOGLE_CSE_ID  = os.environ.get('GOOGLE_CSE_ID')
GOOGLE_CSE_KEY = os.environ.get('GOOGLE_CSE_KEY')

BUCKET = 'product-images'
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

def is_safe_public_url(url):
    """Basic SSRF guard for the user-supplied image URL in set_image(): resolve
    the hostname and reject anything that lands on a private/loopback/link-local
    address (e.g. internal infra, cloud metadata endpoints at 169.254.169.254)
    before the server ever fetches it. Not bulletproof against DNS-rebinding
    attacks, but blocks the straightforward case."""
    try:
        host = urllib.parse.urlparse(url).hostname
        if not host:
            return False
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False

def verify_token(req):
    auth = req.headers.get('Authorization','')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

VALID_CURRENCIES = {'AED','USD','EUR','GBP','SAR','QAR'}

def clean_product(d):
    """Whitelist + normalise incoming product fields."""
    out = {}
    for k in ('brand','model','description','sku','category','vendor_part_number','lead_time','datasheet_url'):
        if k in d: out[k] = str(d.get(k) or '').strip()[:500]
    if 'specs' in d:
        out['specs'] = str(d.get('specs') or '').strip()[:4000]
    if 'default_cost' in d:
        try: out['default_cost'] = max(0, float(d.get('default_cost') or 0))
        except: out['default_cost'] = 0
    if 'default_margin' in d:
        try: out['default_margin'] = min(0.95, max(0, float(d.get('default_margin') or 0)))
        except: out['default_margin'] = 0.2
    if 'cost_currency' in d:
        cur = str(d.get('cost_currency') or 'AED').strip().upper()
        out['cost_currency'] = cur if cur in VALID_CURRENCIES else 'AED'
    if 'is_active' in d:
        out['is_active'] = bool(d.get('is_active'))
    if 'vendor_id' in d:
        vid = (d.get('vendor_id') or '').strip()
        out['vendor_id'] = vid or None
    # Stale-pricing visibility: timestamp only when the cost itself changes,
    # not on every save, so editing an unrelated field doesn't reset it.
    if 'default_cost' in d:
        out['cost_updated_at'] = 'now()'
    return out

# ── List / search products ─────────────────────────────────────────────────────
@app.route('/api/products', methods=['GET'])
def list_products():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    search = (request.args.get('search') or '').strip()
    try: limit = min(100, max(1, int(request.args.get('limit', 50))))
    except: limit = 50
    try: offset = max(0, int(request.args.get('offset', 0)))
    except: offset = 0

    q = sb.table('products').select('*', count='exact').eq('company_id', claims['company_id'])
    if search:
        s = search.replace('%',' ').replace(',',' ')
        q = q.or_(f'brand.ilike.%{s}%,model.ilike.%{s}%,description.ilike.%{s}%,sku.ilike.%{s}%,category.ilike.%{s}%')
    rows = q.order('brand').order('model').range(offset, offset + limit - 1).execute()
    return jsonify({'products': rows.data, 'total': rows.count or 0})

# ── Create product ─────────────────────────────────────────────────────────────
@app.route('/api/products', methods=['POST'])
def create_product():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    d = clean_product(request.json or {})
    if not d.get('model') and not d.get('brand'):
        return jsonify({'error': 'Brand or model is required'}), 400
    d['company_id'] = claims['company_id']
    d['created_by'] = claims['user_id']
    row = sb.table('products').insert(d).execute()
    return jsonify({'product': row.data[0]}), 201

# ── Update product ─────────────────────────────────────────────────────────────
@app.route('/api/products/<pid>', methods=['PUT'])
def update_product(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('products').select('id').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    d = clean_product(request.json or {})
    d['updated_at'] = 'now()'
    row = sb.table('products').update(d).eq('id', pid).execute()
    return jsonify({'product': row.data[0]})

# ── Delete product ─────────────────────────────────────────────────────────────
@app.route('/api/products/<pid>', methods=['DELETE'])
def delete_product(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('products').select('id').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    sb.table('products').delete().eq('id', pid).execute()
    return jsonify({'ok': True})

# ── Bulk import ────────────────────────────────────────────────────────────────
@app.route('/api/products/bulk', methods=['POST'])
def bulk_import():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    items = (request.json or {}).get('products') or []
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'No products provided'}), 400
    if len(items) > 500:
        return jsonify({'error': 'Maximum 500 products per import'}), 400

    rows, skipped = [], 0
    for it in items:
        d = clean_product(it or {})
        if not d.get('model') and not d.get('brand'):
            skipped += 1; continue
        d.setdefault('description', '')
        d.setdefault('default_cost', 0)
        d.setdefault('default_margin', 0.2)
        d['company_id'] = claims['company_id']
        d['created_by'] = claims['user_id']
        rows.append(d)

    if not rows: return jsonify({'error': 'No valid rows found'}), 400
    sb.table('products').insert(rows).execute()
    return jsonify({'imported': len(rows), 'skipped': skipped})

# ── Image search (Google Custom Search) ───────────────────────────────────────
@app.route('/api/products/image-search', methods=['GET'])
def image_search():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not GOOGLE_CSE_ID or not GOOGLE_CSE_KEY:
        return jsonify({'error': 'Image search is not configured (missing Google CSE keys)'}), 500

    q = (request.args.get('q') or '').strip()
    if not q: return jsonify({'error': 'No search query'}), 400

    params = urllib.parse.urlencode({
        'key': GOOGLE_CSE_KEY, 'cx': GOOGLE_CSE_ID, 'q': q,
        'searchType': 'image', 'num': 8, 'safe': 'active',
    })
    try:
        with urllib.request.urlopen('https://www.googleapis.com/customsearch/v1?' + params, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return jsonify({'error': 'Daily image search limit reached. Try again tomorrow or upload manually.'}), 502
        return jsonify({'error': f'Image search failed ({e.code})'}), 502
    except Exception:
        return jsonify({'error': 'Image search service unreachable'}), 502

    images = []
    for it in data.get('items', [])[:8]:
        img = it.get('image') or {}
        images.append({
            'url': it.get('link'),
            'thumb': img.get('thumbnailLink') or it.get('link'),
            'source': img.get('contextLink') or '',
            'title': (it.get('title') or '')[:100],
        })
    return jsonify({'images': images})

# ── Set product image (from web URL or base64 upload) ─────────────────────────
@app.route('/api/products/<pid>/image', methods=['POST'])
def set_image(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('products').select('id').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    d = request.json or {}
    data, ctype = None, None

    if d.get('url'):
        url = str(d['url'])
        if not url.startswith(('http://','https://')):
            return jsonify({'error': 'Invalid image URL'}), 400
        if not is_safe_public_url(url):
            return jsonify({'error': 'That URL cannot be fetched. Please use a direct public image link.'}), 400
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (SysconicQuotes ImageFetch)'})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
                data = resp.read(MAX_IMAGE_BYTES + 1)
        except Exception:
            return jsonify({'error': 'Could not download that image. Try another result or upload manually.'}), 502
    elif d.get('data'):
        import base64
        raw = str(d['data'])
        if ',' in raw: raw = raw.split(',', 1)[1]  # strip data: prefix
        try:
            data = base64.b64decode(raw)
        except Exception:
            return jsonify({'error': 'Invalid image data'}), 400
        ctype = str(d.get('content_type') or 'image/jpeg').split(';')[0].strip().lower()
    else:
        return jsonify({'error': 'Provide either url or data'}), 400

    if len(data) > MAX_IMAGE_BYTES:
        return jsonify({'error': 'Image is larger than 4 MB. Please use a smaller image.'}), 400
    if len(data) < 100:
        return jsonify({'error': 'That does not look like a valid image.'}), 400

    ext = {'image/jpeg':'jpg','image/jpg':'jpg','image/png':'png','image/webp':'webp','image/gif':'gif'}.get(ctype)
    if not ext:
        return jsonify({'error': f'Unsupported image type ({ctype or "unknown"}). Use JPG, PNG, WEBP or GIF.'}), 400

    path = f"{claims['company_id']}/{pid}-{int(time.time())}.{ext}"
    try:
        sb.storage.from_(BUCKET).upload(path, data, {'content-type': ctype})
    except Exception as e:
        return jsonify({'error': 'Could not save image to storage. Check that the product-images bucket exists and is public.'}), 502

    public_url = sb.storage.from_(BUCKET).get_public_url(path)
    if isinstance(public_url, str): public_url = public_url.rstrip('?')

    row = sb.table('products').update({'image_url': public_url, 'updated_at': 'now()'}).eq('id', pid).execute()
    return jsonify({'product': row.data[0]})

# ── Remove product image ───────────────────────────────────────────────────────
@app.route('/api/products/<pid>/image', methods=['DELETE'])
def remove_image(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('products').select('id').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    row = sb.table('products').update({'image_url': None, 'updated_at': 'now()'}).eq('id', pid).execute()
    return jsonify({'product': row.data[0]})

if __name__ == '__main__':
    app.run(debug=True)
