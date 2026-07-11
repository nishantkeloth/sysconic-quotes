from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt, json
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

SYSTEM_PROMPT = """You are a senior estimator at an AV / LED wall installation company in the UAE. You review quotations for completeness and consistency before they are sent to clients.

You will receive a quote as JSON: title, customer, option label, currency, VAT settings, totals (sell, cost, gross profit, margin %, grand total), the terms & conditions list, and sections with line items (brand, model, description, qty, cost, margin %).

Check for the issues that commonly get missed in LED/AV quotes:
- Spare modules / spare parts line (typically ~2% for LED walls)
- LED controller / video processor present and plausible for the panel quantity
- Receiver cards, hub cards, power supplies
- Mounting structure / rigging / frame
- Installation, testing & commissioning labour
- Cabling (power and data)
- Line items with qty 0, cost 0, or an empty description
- Items with unusually low (below 10%), zero, or negative margin
- VAT disabled (5% VAT is standard for UAE-based clients)
- Terms & conditions missing any of: payment terms, delivery period, warranty, offer validity

Also give credit for what IS present: include 2-4 "ok" findings for important elements that are correctly covered.

Respond ONLY with valid JSON, no markdown fences, no commentary, in exactly this shape:
{"summary":"one or two sentence overall assessment","findings":[{"status":"warning","title":"short title","detail":"one sentence explanation"}]}
status must be "warning" or "ok". Put warnings first. Maximum 10 findings total. Never invent or suggest specific prices. If the quote looks complete, say so in the summary."""

@app.route('/api/ai/review', methods=['POST'])
def review_quote():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    if not GEMINI_API_KEY:
        return jsonify({'error': 'AI review is not configured yet (GEMINI_API_KEY is missing)'}), 500

    quote = (request.json or {}).get('quote')
    if not quote: return jsonify({'error': 'No quote data provided'}), 400

    body = json.dumps({
        'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
        'contents': [{'role': 'user', 'parts': [{'text': 'Review this quote:\n' + json.dumps(quote, ensure_ascii=False)}]}],
        'generationConfig': {
            'temperature': 0.2,
            'maxOutputTokens': 4000,
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
        with urllib.request.urlopen(req, timeout=50) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        code = e.code
        if code == 429:
            return jsonify({'error': 'AI review rate limit reached. Please wait a minute and try again.'}), 502
        detail = e.read().decode('utf-8', 'ignore')[:200]
        return jsonify({'error': f'AI service error ({code}). {detail}'}), 502
    except Exception:
        return jsonify({'error': 'AI service is unreachable right now. Please try again.'}), 502

    try:
        parts = data['candidates'][0]['content']['parts']
        text = ''.join(p.get('text', '') for p in parts).strip()
    except (KeyError, IndexError):
        return jsonify({'error': 'AI service returned an unexpected response. Please try again.'}), 502

    # Strip accidental markdown fences if the model added them anyway
    if text.startswith('```'):
        text = text.strip('`').strip()
        if text.lower().startswith('json'):
            text = text[4:].strip()

    try:
        result = json.loads(text)
    except Exception:
        # Fall back to showing the raw text as a summary rather than failing
        return jsonify({'summary': text[:600], 'findings': []})

    findings = []
    for f in (result.get('findings') or [])[:10]:
        status = f.get('status')
        if status not in ('ok', 'warning'): status = 'info'
        findings.append({
            'status': status,
            'title':  str(f.get('title', ''))[:120],
            'detail': str(f.get('detail', ''))[:300],
        })

    return jsonify({'summary': str(result.get('summary', ''))[:600], 'findings': findings})

if __name__ == '__main__':
    app.run(debug=True)
