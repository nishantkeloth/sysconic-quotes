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
BRAVE_API_KEY  = os.environ.get('BRAVE_API_KEY')  # Brave Search API (replaced Google CSE — closed to new customers in 2026)

BUCKET = 'product-images'
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB

# TEN-002 fix: product-images is a PRIVATE bucket now (or about to become one --
# see docs/saas-audit/REMEDIATION-ROADMAP.md). We no longer hand out permanent,
# unauthenticated get_public_url() links. Instead we mint a signed URL at
# upload time with a very long expiry (~10 years) -- long enough to behave like
# "permanent" for how this app actually uses the link (embedded directly into
# products.image_url and copied into quote_data line items, so it needs to
# keep working indefinitely once saved), while still requiring the signed
# token instead of being a guessable, world-readable path.
SIGNED_URL_EXPIRES_SECONDS = 10 * 365 * 24 * 60 * 60  # ~10 years

def _signed_url(bucket, path, expires_in=SIGNED_URL_EXPIRES_SECONDS):
    """Wraps sb.storage.from_(bucket).create_signed_url(). The supabase-py
    storage client's response shape has varied across versions ('signedURL'
    in most releases, occasionally 'signedUrl'/'signed_url'), and the value
    itself is sometimes a full URL and sometimes a path that still needs the
    project's storage host prefixed -- this defensively handles both. Verify
    once against your actual installed supabase-py version after deploying
    (upload one test image and confirm it loads) before relying on this."""
    res = sb.storage.from_(bucket).create_signed_url(path, expires_in)
    url = res.get('signedURL') or res.get('signedUrl') or res.get('signed_url') or res.get('url')
    if not url:
        raise RuntimeError(f'Unexpected create_signed_url() response shape: {res!r}')
    if url.startswith('http://') or url.startswith('https://'):
        return url
    return f"{SUPABASE_URL}/storage/v1{url}"

def _extract_storage_path(url, bucket):
    """Best-effort recovery of the object path from an already-stored
    image_url, whether it's an old-style public URL or a new-style signed
    URL -- both contain '/<bucket>/<path>' somewhere before an optional '?'
    query string. Used so remove_image() can actually delete the underlying
    file (REL-005) without needing a dedicated path column (a cleaner,
    longer-term fix -- track this in the REL-001 schema cleanup)."""
    if not url:
        return None
    m = re.search(rf'/{re.escape(bucket)}/([^?]+)', url)
    return m.group(1) if m else None

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

def brave_search(endpoint, q, count):
    """Call Brave Search API ('web' or 'images'). Returns (data, err_str).
    Free tier: ~1 req/sec, 2000 queries/month — comfortably covers low daily volume."""
    params = urllib.parse.urlencode({'q': q, 'count': count, 'safesearch': 'strict'})
    req = urllib.request.Request(
        f'https://api.search.brave.com/res/v1/{endpoint}/search?' + params,
        headers={'Accept': 'application/json', 'X-Subscription-Token': BRAVE_API_KEY or ''})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode('utf-8')), None
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, 'Search rate limit reached. Wait a moment and try again.'
        if e.code in (401, 403):
            return None, f'Search API key rejected (HTTP {e.code}). Check BRAVE_API_KEY.'
        return None, f'HTTP {e.code}: {e.read().decode("utf-8","ignore")[:300]}'
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'

def verify_token(req):
    auth = req.headers.get('Authorization','')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))

VALID_CURRENCIES = {'AED','USD','EUR','GBP','SAR','QAR'}

def normalize_key(s):
    """Collapse a brand/model string to a comparison key so 'IL-FISS-ORMV 3.9Pro',
    'IL FISS ORMV 3.9Pro', and 'ILFISSORMV3.9Pro' are all recognized as the same
    product. Strips spaces, hyphens, underscores, and case — but NOT digits or
    dots, so genuinely different models (e.g. 3.9Pro vs 39Pro) still stay distinct."""
    return re.sub(r'[\s\-_]+', '', (s or '').strip().lower())

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
# De-duplication: model numbers typed by hand (or picked up from a web search)
# routinely differ only in spacing/hyphenation — "IL-FISS-ORMV 3.9Pro" vs
# "IL FISS ORMV 3.9Pro" vs "ILFISSORMV3.9Pro". Comparing normalize_key() output
# (case/space/hyphen/underscore-insensitive) catches these as the same product
# before creating a new row. A matching database index (see migration) also
# rejects any duplicate that slips past this check under concurrent requests —
# on that rare race, we catch the conflict and return the existing row instead
# of a 500 error.
@app.route('/api/products', methods=['POST'])
def create_product():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    d = clean_product(request.json or {})
    if not d.get('model') and not d.get('brand'):
        return jsonify({'error': 'Brand or model is required'}), 400

    model_key = normalize_key(d.get('model'))
    brand_key = normalize_key(d.get('brand'))

    if model_key:
        existing = sb.table('products').select('*') \
            .eq('company_id', claims['company_id']) \
            .eq('model_key', model_key).eq('brand_key', brand_key).execute()
        if existing.data:
            hit = existing.data[0]
            # Fill in any details this call has that the existing record lacks,
            # so the catalog keeps improving instead of silently discarding
            # newer info — but never overwrite fields it already has.
            fill = {}
            for k in ('description', 'sku', 'category', 'vendor_part_number', 'lead_time', 'datasheet_url'):
                if d.get(k) and not hit.get(k): fill[k] = d[k]
            if d.get('default_cost') and not hit.get('default_cost'): fill['default_cost'] = d['default_cost']
            if fill:
                fill['updated_at'] = 'now()'
                row = sb.table('products').update(fill).eq('id', hit['id']).execute()
                hit = row.data[0]
            return jsonify({'product': hit, 'deduped': True}), 200

    d['company_id'] = claims['company_id']
    d['created_by'] = claims['user_id']
    d['model_key'] = model_key
    d['brand_key'] = brand_key
    try:
        row = sb.table('products').insert(d).execute()
    except Exception as e:
        # Unique-index race: another request created the same product between
        # our check above and this insert. Fetch and return that one instead
        # of surfacing a raw database error.
        if '23505' in str(e) or 'duplicate key' in str(e).lower():
            existing = sb.table('products').select('*') \
                .eq('company_id', claims['company_id']) \
                .eq('model_key', model_key).eq('brand_key', brand_key).execute()
            if existing.data:
                return jsonify({'product': existing.data[0], 'deduped': True}), 200
        raise
    return jsonify({'product': row.data[0]}), 201

# ── Update product ─────────────────────────────────────────────────────────────
@app.route('/api/products/<pid>', methods=['PUT'])
def update_product(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('products').select('id').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    d = clean_product(request.json or {})
    if 'model' in d: d['model_key'] = normalize_key(d['model'])
    if 'brand' in d: d['brand_key'] = normalize_key(d['brand'])
    d['updated_at'] = 'now()'
    row = sb.table('products').update(d).eq('id', pid).execute()
    return jsonify({'product': row.data[0]})

# ── Sync a quote line's cost back to the product it's linked to ────────────────
# Fixes a real gap: captureProduct() in index.html auto-creates a catalog
# product the moment a quote line's Brand/Model field is blurred, using
# whatever cost is in the row at that instant -- which is almost always still
# 0, since the user usually hasn't typed the cost yet. Nothing ever pushed the
# real cost back afterward. This endpoint does that, called when the Cost
# field is blurred on a row that's already linked to a product_id. Only fills
# in a cost that's currently missing/zero -- never overwrites a deliberately
# set catalog price just because one particular quote line (which may carry a
# special/negotiated cost) happened to be edited.
@app.route('/api/products/<pid>/sync-cost', methods=['POST'])
def sync_product_cost(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    try:
        cost = max(0, float((request.json or {}).get('cost') or 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid cost'}), 400
    if cost <= 0:
        return jsonify({'ok': True, 'updated': False})

    existing = sb.table('products').select('id,default_cost').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not existing.data:
        return jsonify({'error': 'Not found'}), 404
    if float(existing.data[0].get('default_cost') or 0) > 0:
        return jsonify({'ok': True, 'updated': False})

    sb.table('products').update({'default_cost': cost, 'cost_updated_at': 'now()'}).eq('id', pid).execute()
    return jsonify({'ok': True, 'updated': True})

# ── Sync a quote line's description back to the product it's linked to ────────
# Same gap as sync-cost above, but for description: captureProduct() in
# index.html links/creates the catalog product the moment Brand or Model is
# blurred -- almost always before the user has typed a description yet -- so
# without this, a product auto-created from a quote line permanently keeps a
# blank description even after the real description is typed in on that line.
# Only fills in a description that's currently missing -- never overwrites one
# the catalog already has, since that may have been deliberately curated.
@app.route('/api/products/<pid>/sync-description', methods=['POST'])
def sync_product_description(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    desc = str((request.json or {}).get('description') or '').strip()[:500]
    if not desc:
        return jsonify({'ok': True, 'updated': False})

    existing = sb.table('products').select('id,description').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not existing.data:
        return jsonify({'error': 'Not found'}), 404
    if (existing.data[0].get('description') or '').strip():
        return jsonify({'ok': True, 'updated': False})

    sb.table('products').update({'description': desc, 'updated_at': 'now()'}).eq('id', pid).execute()
    return jsonify({'ok': True, 'updated': True})

# ── Delete product ─────────────────────────────────────────────────────────────
@app.route('/api/products/<pid>', methods=['DELETE'])
def delete_product(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'products'): return jsonify({'error': 'You do not have access to this feature.'}), 403

    existing = sb.table('products').select('id').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    sb.table('products').delete().eq('id', pid).execute()
    return jsonify({'ok': True})

# ── Bulk import ────────────────────────────────────────────────────────────────
@app.route('/api/products/bulk', methods=['POST'])
def bulk_import():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not has_page_access(claims, 'products'): return jsonify({'error': 'You do not have access to this feature.'}), 403

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

# ── Image search (Brave Search API) ────────────────────────────────────────────
@app.route('/api/products/image-search', methods=['GET'])
def image_search():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not BRAVE_API_KEY:
        return jsonify({'error': 'Image search is not configured (missing BRAVE_API_KEY)'}), 500

    q = (request.args.get('q') or '').strip()
    if not q: return jsonify({'error': 'No search query'}), 400

    data, err = brave_search('images', q, 8)
    if err:
        return jsonify({'error': f'Image search failed. {err}'}), 502

    images = []
    for it in (data.get('results') or [])[:8]:
        props = it.get('properties') or {}
        images.append({
            'url': props.get('url') or it.get('url'),
            'thumb': (it.get('thumbnail') or {}).get('src') or props.get('url') or it.get('url'),
            'source': it.get('source') or '',
            'title': (it.get('title') or '')[:100],
        })
    return jsonify({'images': images})

# ── Online product lookup (manual entry: model not found in Product Master) ───
# Used by the quote-item "Search Online" flow: when a typed model number has no
# match in the company's own catalog, this does a live web + image search via
# Brave Search (grounded — not the model's own knowledge), and guesses a brand
# by checking the company's own existing catalog brand names against the top
# result's title (no LLM call — keeps this fast/cheap and avoids a second
# provider dependency). The frontend always shows this as an editable,
# user-confirmed draft before anything is saved — never auto-added.
@app.route('/api/products/online-search', methods=['GET'])
def online_search():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not BRAVE_API_KEY:
        return jsonify({'error': 'Online search is not configured (missing BRAVE_API_KEY)'}), 500

    q = (request.args.get('q') or '').strip()
    if not q: return jsonify({'error': 'No search query'}), 400
    q = q[:200]

    # Plain web results (title/snippet/link) — the entered model number is used
    # verbatim as the primary search reference, per spec, rather than being
    # rewritten or expanded.
    web_data, web_err = brave_search('web', q, 5)
    results = []
    if web_data:
        for it in ((web_data.get('web') or {}).get('results') or [])[:5]:
            results.append({
                'title': (it.get('title') or '')[:150],
                'snippet': (it.get('description') or '')[:400],
                'link': it.get('url'),
            })

    # Image results, same shape as /image-search — real photos from the web,
    # never AI-generated.
    img_data, img_err = brave_search('images', q, 8)
    images = []
    if img_data:
        for it in (img_data.get('results') or [])[:8]:
            props = it.get('properties') or {}
            images.append({
                'url': props.get('url') or it.get('url'),
                'thumb': (it.get('thumbnail') or {}).get('src') or props.get('url') or it.get('url'),
                'source': it.get('source') or '',
            })

    if not results and not images:
        # Surface the real cause when both calls actually failed (bad key,
        # quota, auth, etc.) rather than always claiming "no results" — a
        # genuinely empty query still gets the plain not-found message.
        if web_err or img_err:
            return jsonify({'error': f'Online search failed. Web: {web_err or "ok"} | Image: {img_err or "ok"}'}), 502
        return jsonify({'error': f'No online results found for "{q}". Enter the details manually below.'}), 200

    # Brand guess: does any brand already in this company's catalog appear in
    # the top result's title? Cheap and grounded in real data they already use.
    guessed_brand = ''
    if results:
        title_l = (results[0].get('title') or '').lower()
        try:
            brand_rows = sb.table('products').select('brand').eq('company_id', claims['company_id']) \
                .not_.is_('brand', 'null').limit(200).execute()
            seen = set()
            for r in (brand_rows.data or []):
                b = (r.get('brand') or '').strip()
                if b and b.lower() not in seen:
                    seen.add(b.lower())
                    if b.lower() in title_l:
                        guessed_brand = b
                        break
        except Exception:
            pass

    return jsonify({'query': q, 'results': results, 'images': images, 'guessed_brand': guessed_brand})

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
        return jsonify({'error': 'Could not save image to storage. Check that the product-images bucket exists.'}), 502

    try:
        signed_url = _signed_url(BUCKET, path)
    except Exception as e:
        return jsonify({'error': f'Image uploaded but could not generate its access URL: {str(e)[:200]}'}), 502

    row = sb.table('products').update({'image_url': signed_url, 'updated_at': 'now()'}).eq('id', pid).execute()
    return jsonify({'product': row.data[0]})

# ── Remove product image ───────────────────────────────────────────────────────
@app.route('/api/products/<pid>/image', methods=['DELETE'])
def remove_image(pid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    existing = sb.table('products').select('id,image_url').eq('id', pid).eq('company_id', claims['company_id']).execute()
    if not existing.data: return jsonify({'error': 'Not found'}), 404

    # REL-005 fix: actually delete the underlying storage object instead of
    # only nulling the DB column, so removed images don't linger indefinitely.
    old_path = _extract_storage_path(existing.data[0].get('image_url'), BUCKET)
    if old_path:
        try:
            sb.storage.from_(BUCKET).remove([old_path])
        except Exception:
            pass  # best-effort -- don't block the DB update if storage cleanup fails

    row = sb.table('products').update({'image_url': None, 'updated_at': 'now()'}).eq('id', pid).execute()
    return jsonify({'product': row.data[0]})

if __name__ == '__main__':
    app.run(debug=True)
