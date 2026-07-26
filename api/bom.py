from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt, json
import urllib.request, urllib.error
from supabase import create_client

app = Flask(__name__)
CORS(app)

SUPABASE_URL   = os.environ.get('SUPABASE_URL')
SUPABASE_KEY   = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET     = os.environ.get('JWT_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

GEMINI_MODEL = 'gemini-3.5-flash'
GEMINI_URL   = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

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

@app.before_request
def _rbac_page_gate():
    claims = verify_token(request)
    if not claims:
        return None
    if not has_page_access(claims, 'quotes'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    return None
SYSTEM_PROMPT = """You are a senior estimator at an AV / LED wall installation company in the UAE, drafting a bill of materials (BOM) for a new project based on a short description from the salesperson.

You will be given:
1. A plain-language project description.
2. This company's existing product catalog (brand, model, description, cost, margin) as JSON — these are real products with real, verified costs the company already uses.

Your job: produce a complete, realistic BOM covering everything a project like this would need — not just the headline item. For an LED wall / AV install, typically consider: the display/panels themselves, controller/video processor, receiver cards, power supplies, mounting structure/rigging, cabling (power + data), spare modules (~2% for LED), and installation/testing/commissioning labour — but only include what's actually relevant to the described project.

Matching rule (critical): For each item, first check if something in the catalog is a good match (same or equivalent brand/model/category). If yes, set "source":"catalog", copy the cost and margin EXACTLY from that catalog entry, and set "product_id" to its id. If nothing in the catalog fits, set "source":"suggested", leave "product_id" null, and suggest a plausible real-world brand/model with a realistic UAE market cost in AED and margin 0.2. Never invent a cost for a catalog item — always reuse the catalog's own number.

Group items into logical sections (e.g. "Display", "Processing & Control", "Mounting & Structure", "Cabling", "Installation & Commissioning"). Only create sections that are relevant.

Respond ONLY with valid JSON, no markdown fences, no commentary, in exactly this shape:
{"sections":[{"name":"Display","items":[{"brand":"","model":"","description":"","cost":0,"margin":0.2,"qty":1,"source":"catalog","product_id":"uuid-or-null"}]}]}
Cost is a plain number in the project's stated currency. Margin is a decimal fraction (0.2 = 20%). Qty is an integer >= 1. Maximum 25 items total across all sections."""

@app.route('/api/bom/generate', methods=['POST'])
def generate_bom():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    if not GEMINI_API_KEY:
        return jsonify({'error': 'Auto-BOM is not configured yet (GEMINI_API_KEY is missing)'}), 500

    d = request.json or {}
    description = (d.get('description') or '').strip()
    if not description:
        return jsonify({'error': 'Describe the project first'}), 400
    if len(description) > 2000:
        description = description[:2000]
    currency = (d.get('currency') or 'AED').strip()[:10]

    # Pull this company's catalog for grounding (cap to keep the prompt small)
    try:
        catalog_rows = sb.table('products').select('id,brand,model,description,default_cost,default_margin') \
            .eq('company_id', claims['company_id']).order('brand').order('model').limit(300).execute()
        catalog = [{
            'id': r.get('id'),
            'brand': r.get('brand'),
            'model': r.get('model'),
            'description': r.get('description'),
            'cost': r.get('default_cost'),
            'margin': r.get('default_margin'),
        } for r in (catalog_rows.data or [])]
    except Exception:
        catalog = []

    user_msg = json.dumps({
        'project_description': description,
        'currency': currency,
        'catalog': catalog,
    }, ensure_ascii=False)

    body = json.dumps({
        'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
        'contents': [{'role': 'user', 'parts': [{'text': user_msg}]}],
        'generationConfig': {
            'temperature': 0.3,
            'maxOutputTokens': 6000,
            'responseMimeType': 'application/json',
            'thinkingConfig': {'thinkingBudget': 0},
        },
    }).encode('utf-8')

    req = urllib.request.Request(
        GEMINI_URL,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'x-goog-api-key': GEMINI_API_KEY,
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=55) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        code = e.code
        if code == 429:
            return jsonify({'error': 'Auto-BOM rate limit reached. Please wait a minute and try again.'}), 502
        detail = e.read().decode('utf-8', 'ignore')[:200]
        return jsonify({'error': f'AI service error ({code}). {detail}'}), 502
    except Exception:
        return jsonify({'error': 'AI service is unreachable right now. Please try again.'}), 502

    try:
        parts = data['candidates'][0]['content']['parts']
        text = ''.join(p.get('text', '') for p in parts).strip()
    except (KeyError, IndexError):
        return jsonify({'error': 'AI service returned an unexpected response. Please try again.'}), 502

    if text.startswith('```'):
        text = text.strip('`').strip()
        if text.lower().startswith('json'):
            text = text[4:].strip()

    try:
        result = json.loads(text)
    except Exception:
        return jsonify({'error': 'Could not parse the AI response. Please try again with a more specific description.'}), 502

    # Build a quick lookup so we can enforce the "catalog cost is authoritative" rule
    catalog_by_id = {c['id']: c for c in catalog if c.get('id')}

    sections = []
    for s in (result.get('sections') or [])[:10]:
        items = []
        for it in (s.get('items') or [])[:25]:
            pid = it.get('product_id')
            cat = catalog_by_id.get(pid) if pid else None
            if cat:
                items.append({
                    'brand': cat.get('brand') or '',
                    'model': cat.get('model') or '',
                    'description': cat.get('description') or '',
                    'cost': float(cat.get('cost') or 0),
                    'margin': float(cat.get('margin') if cat.get('margin') is not None else 0.2),
                    'qty': max(1, int(it.get('qty') or 1)),
                    'source': 'catalog',
                    'product_id': pid,
                })
            else:
                try: cost = max(0, float(it.get('cost') or 0))
                except: cost = 0
                try: margin = min(0.95, max(0, float(it.get('margin') or 0.2)))
                except: margin = 0.2
                items.append({
                    'brand': str(it.get('brand') or '')[:200],
                    'model': str(it.get('model') or '')[:200],
                    'description': str(it.get('description') or '')[:500],
                    'cost': cost,
                    'margin': margin,
                    'qty': max(1, int(it.get('qty') or 1)) if str(it.get('qty') or '').strip() else 1,
                    'source': 'suggested',
                    'product_id': None,
                })
        if items:
            sections.append({'name': str(s.get('name') or 'Suggested items')[:100], 'items': items})

    return jsonify({'sections': sections})

if __name__ == '__main__':
    app.run(debug=True)
