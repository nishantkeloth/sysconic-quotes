from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt, json, time
import urllib.request, urllib.error

app = Flask(__name__)
CORS(app)

JWT_SECRET     = os.environ.get('JWT_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

GEMINI_MODEL = 'gemini-3.5-flash'
GEMINI_URL   = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'

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

SYSTEM_PROMPT = """You are a senior AV systems design engineer. You receive the bill of materials (BOM) of an AV / LED / IT installation quote as JSON: sections with line items (brand, model, description, qty). Your job is to produce a professional SYSTEM SCHEMATIC (signal flow diagram) as Mermaid flowchart code.

Rules for the diagram:
- Start with exactly: flowchart LR
- Infer each item's role from its description: source, processing (switcher/DSP/controller/processor), display, audio output, power, mounting/rigging, control, network. Ignore pure consumables/labour/spares/mounting hardware — they don't appear in a signal flow.
- Group nodes into subgraphs by zone, e.g. SOURCES, CONFERENCING, PROCESSING - RACK, DISPLAY, AUDIO, CONTROL & NETWORK, POWER. Only create zones that apply.
- Node label format: "Friendly role name<br>Brand Model" and include qty when more than 1 (e.g. "8x Ceiling Speakers<br>JBL Control 26CT"). Always double-quote node labels.
- Edges must be labeled with the realistic cable/protocol: HDMI, HDBaseT, Cat6 data, Dante, USB, RS-232, IP Control, PoE, 70V speaker line, power cable, etc.
- Line conventions: solid arrows ( -- label --> ) for AV signal, thick arrows ( == label ==> ) for audio/video over IP or network, dotted ( -. label .- ) for control.
- Every device node must have at least one connection. If a required piece of the chain is obviously missing from the BOM (e.g. panels but no processor), still draw the chain with what exists — never invent devices that are not in the BOM.
- Style the diagram: end with classDef lines using these fills and assign every node a class:
  sources #dbeafe stroke #2563eb, conferencing #ede9fe stroke #7c3aed, processing #fef3c7 stroke #d97706, display #dcfce7 stroke #16a34a, audio #ffe4e6 stroke #e11d48, control #e2e8f0 stroke #475569, power #fee2e2 stroke #b91c1c. stroke-width:2px on all.
- Node IDs: short uppercase alphanumeric (PC, SW1, LED). No special characters in IDs. Never use | ( ) [ ] { } # or " inside label text — spell them out or omit.
- Keep it readable: maximum ~25 nodes. Merge identical repeated items into one node with a qty prefix.

Respond ONLY with valid JSON, no markdown fences, no commentary, in exactly this shape:
{"mermaid":"flowchart LR\\n ..."}"""

MAX_ITEMS = 120

@app.route('/api/diagram/generate', methods=['POST'])
def generate_diagram():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    if not GEMINI_API_KEY:
        return jsonify({'error': 'Diagram generation is not configured yet (GEMINI_API_KEY is missing)'}), 500

    d = request.json or {}
    quote = d.get('quote') or {}
    sections = quote.get('sections') or []
    if not any((s.get('items') or []) for s in sections):
        return jsonify({'error': 'This option has no line items yet — add the BOM first.'}), 400

    # Slim the payload: only what the schematic needs, hard-capped
    slim, count = [], 0
    for s in sections[:20]:
        items = []
        for it in (s.get('items') or []):
            if count >= MAX_ITEMS: break
            items.append({
                'brand': str(it.get('brand') or '')[:120],
                'model': str(it.get('model') or '')[:120],
                'description': str(it.get('desc') or it.get('description') or '')[:300],
                'qty': it.get('qty') or 1,
            })
            count += 1
        if items:
            slim.append({'name': str(s.get('name') or '')[:100], 'items': items})

    user_msg = json.dumps({
        'title': str(quote.get('title') or '')[:200],
        'sections': slim,
    }, ensure_ascii=False)

    body = json.dumps({
        'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
        'contents': [{'role': 'user', 'parts': [{'text': 'Draw the system schematic for this BOM:\n' + user_msg}]}],
        'generationConfig': {
            'temperature': 0.2,
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

    # Same transient-retry approach as api/proposal.py
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=55) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1)); continue
            if e.code == 429:
                return jsonify({'error': 'Diagram generation is rate-limited right now. Please wait a minute and try again.'}), 502
            detail = e.read().decode('utf-8', 'ignore')[:200]
            return jsonify({'error': f'AI service error ({e.code}). {detail}'}), 502
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1)); continue
            return jsonify({'error': 'AI service is unreachable right now. Please try again.'}), 502
    if data is None:
        return jsonify({'error': 'AI service is currently overloaded. Please try again in a minute.'}), 502

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
        code = str(result.get('mermaid') or '').strip()
    except Exception:
        # Model occasionally returns bare mermaid despite the JSON instruction
        code = text.strip()

    # Strip mermaid fences if present, then validate
    if code.startswith('```'):
        code = code.strip('`').strip()
        if code.lower().startswith('mermaid'):
            code = code[7:].strip()
    if not (code.startswith('flowchart') or code.startswith('graph')):
        return jsonify({'error': 'Could not generate a valid diagram from this BOM. Please try again.'}), 502
    if len(code) > 20000:
        code = code[:20000]

    return jsonify({'mermaid': code})

if __name__ == '__main__':
    app.run(debug=True)
