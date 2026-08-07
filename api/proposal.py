from flask import Flask, request, jsonify
from flask_cors import CORS
import os, jwt, json, time, io, textwrap
import urllib.request, urllib.error, urllib.parse
from supabase import create_client

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
                                Table, TableStyle, Image, KeepTogether, NextPageTemplate, PageBreak)
from reportlab.lib.utils import ImageReader

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI_MODEL   = 'gemini-3.5-flash'
GEMINI_URL     = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
ALLOWED_GEMINI_MODELS = ['gemini-3.5-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-3.6-flash']
def _gemini_url_for(model):
    m = model if model in ALLOWED_GEMINI_MODELS else GEMINI_MODEL
    return f'https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent'

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

BUCKET = 'proposals'
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024  # 8 MB

# TEN-002 fix: proposals is a PRIVATE bucket now (or about to become one --
# see docs/saas-audit/REMEDIATION-ROADMAP.md). Unlike product images/logos,
# a generated proposal is normally downloaded once shortly after generation
# rather than embedded long-term into another record, so a shorter (but
# still generous) expiry is used here. Same response-shape caveat as
# api/products.py's _signed_url() -- verify once against a real upload.
PROPOSAL_SIGNED_URL_EXPIRES_SECONDS = 7 * 24 * 60 * 60  # 7 days

def _signed_url(bucket, path, expires_in=PROPOSAL_SIGNED_URL_EXPIRES_SECONDS):
    res = sb.storage.from_(bucket).create_signed_url(path, expires_in)
    url = res.get('signedURL') or res.get('signedUrl') or res.get('signed_url') or res.get('url')
    if not url:
        raise RuntimeError(f'Unexpected create_signed_url() response shape: {res!r}')
    if url.startswith('http://') or url.startswith('https://'):
        return url
    return f"{SUPABASE_URL}/storage/v1{url}"

def verify_token(req):
    auth = req.headers.get('Authorization', '')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

# ── Colors (matches the modernized Quotation PDF) ───────────────────────────────
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
NAVY    = HexColor('#16294f')
NAVY2   = HexColor('#1a3c6e')
GOLD    = HexColor('#f4b400')
TINT    = HexColor('#f2f6fc')
CARDBG  = HexColor('#f5f7fb')
ZEBRA   = HexColor('#f8fafc')
GRAYB   = HexColor('#e2e6ec')
TXT     = HexColor('#1c1f26')
GRAY    = HexColor('#7c8798')
LIGHTBLU= HexColor('#c7d3ea')

# ── Template styles: whole-document theming ─────────────────────────────────────
# Each style pairs a cover-page LAYOUT (the physical arrangement of logo/title/
# stats — see COVER_LAYOUTS below) with a color THEME. The original 4 styles
# (modern/corporate/technical/executive) keep their original navy/gold palette
# exactly, so existing proposals look identical. The 4 new styles reuse those
# same 4 layout shapes but with a genuinely different palette each, so picking
# one changes the color scheme of the ENTIRE document (cover, section badges,
# cards, tables, footer) rather than just the cover page as before.
def _hexset(navy, navy2, gold, tint, cardbg, zebra, grayb, txt, gray, lightblu):
    return {'navy': HexColor(navy), 'navy2': HexColor(navy2), 'gold': HexColor(gold),
            'tint': HexColor(tint), 'cardbg': HexColor(cardbg), 'zebra': HexColor(zebra),
            'grayb': HexColor(grayb), 'txt': HexColor(txt), 'gray': HexColor(gray),
            'lightblu': HexColor(lightblu)}

THEMES = {
    # Original 4 -- unchanged navy/gold palette.
    'modern':     _hexset('#16294f','#1a3c6e','#f4b400','#f2f6fc','#f5f7fb','#f8fafc','#e2e6ec','#1c1f26','#7c8798','#c7d3ea'),
    'corporate':  _hexset('#16294f','#1a3c6e','#f4b400','#f2f6fc','#f5f7fb','#f8fafc','#e2e6ec','#1c1f26','#7c8798','#c7d3ea'),
    'technical':  _hexset('#16294f','#1a3c6e','#f4b400','#f2f6fc','#f5f7fb','#f8fafc','#e2e6ec','#1c1f26','#7c8798','#c7d3ea'),
    'executive':  _hexset('#16294f','#1a3c6e','#f4b400','#f2f6fc','#f5f7fb','#f8fafc','#e2e6ec','#1c1f26','#7c8798','#c7d3ea'),
    # New -- distinct, more colorful palettes, each on one of the layouts above.
    'vibrant':    _hexset('#5b21b6','#7c3aed','#f97316','#f5f3ff','#faf5ff','#f5f3ff','#e9d5ff','#1e1b2e','#8b7aa8','#ddd6fe'),  # purple + orange, on the Modern full-bleed layout
    'minimal':    _hexset('#111827','#374151','#0d9488','#f3f4f6','#f9fafb','#f3f4f6','#e5e7eb','#111827','#6b7280','#a7f3d0'),  # slate + teal, on the Corporate layout
    'classic':    _hexset('#7a1f2b','#9a2b3a','#c9a227','#faf6ee','#fdfaf3','#faf6ee','#e8dfc8','#2b2018','#8a7a63','#f0d9a8'),  # burgundy + antique gold, on the Executive layout
    'midnight':   _hexset('#0b1220','#111827','#38bdf8','#f8fafc','#eff6ff','#f1f5f9','#dbeafe','#0f172a','#64748b','#bae6fd'),  # near-black navy + bright cyan, on the Technical layout
}
COVER_LAYOUT_FOR_STYLE = {
    'modern': 'modern', 'vibrant': 'modern',
    'corporate': 'corporate', 'minimal': 'corporate',
    'executive': 'executive', 'classic': 'executive',
    'technical': 'technical', 'midnight': 'technical',
}

def _theme_for(style):
    return THEMES.get(style) or THEMES['modern']

def _hex(color):
    """Return a 6-digit hex string (no '#') for a reportlab Color, for use inside <font color=...> markup."""
    try:
        return color.hexval()[2:]
    except Exception:
        return '16294f'

# ── Per-company branding (same shape as api/pdf.py — duplicated since Vercel
# doesn't bundle sibling modules; see that file for the fuller explanation) ────
def build_company_dict(co, fallback_name):
    co = co or {}
    name = (co.get('legal_name') or fallback_name or '').strip()
    addr_bits = [b for b in [co.get('address')] if b]
    trn_tel_bits = []
    if co.get('trn'):   trn_tel_bits.append(f"TRN: {co['trn']}")
    if co.get('phone'): trn_tel_bits.append(f"Tel: {co['phone']}")
    return {
        'name': name or 'Your Company',
        'sub':  '  |  '.join(addr_bits),
        'trn_tel': '  |  '.join(trn_tel_bits),
        'web': co.get('website') or '',
        'phone': co.get('phone') or '',
        'address': co.get('address') or '',
        'trn': co.get('trn') or '',
        'email': co.get('email') or '',
        'certifications': co.get('certifications') or '',
        'founded_year': co.get('founded_year') or '',
        'notable_clients': co.get('notable_clients') or '',
    }

def _fetch_bytes(url, timeout=15):
    if not url: return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (SysconicQuotes Proposal)'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None

# ── Pricing math (mirrors api/pdf.py's calc_item/calc_opt) ──────────────────────
def _num(v, d=0):
    try: return float(v or 0)
    except: return 0.0

def calc_item(it, pricing_type='markup'):
    # Mirrors index.html's calcItem(): Markup = cost x (1+pct) (default),
    # Margin = cost / (1-pct) capped at 95%. int(x+0.5) == JS Math.round.
    cad = _num(it.get('cost')) * (1 - _num(it.get('disc'))/100.0) * (1 - _num(it.get('discAdd'))/100.0)
    m = _num(it.get('margin'))
    if pricing_type == 'margin':
        mm = min(0.95, max(0.0, m))
        up = int(cad / (1 - mm) + 0.5)
    else:
        up = int(cad * (1 + m) + 0.5)
    qty = _num(it.get('qty'))
    return up, up * qty

def calc_opt(o, pricing_type='markup'):
    ts = 0.0
    for s in (o.get('sections') or []):
        for it in (s.get('items') or []):
            ts += calc_item(it, pricing_type)[1]
    vat_on = bool(o.get('vatEnabled'))
    rate = _num(o.get('vatRate') or 5)
    vat = ts * rate/100.0 if vat_on else 0.0
    return ts, vat_on, rate, vat, ts + vat

def fmt(n):
    return f"{n:,.2f}"

# ── Attachment text extraction (PDF / DOCX / plain text) ────────────────────────
def extract_attachment_text(filename, data):
    ext = (filename or '').lower().rsplit('.', 1)[-1] if filename and '.' in filename else ''
    try:
        if ext == 'pdf':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            out = []
            for p in reader.pages[:30]:
                out.append(p.extract_text() or '')
            return '\n'.join(out)[:10000]
        elif ext == 'docx':
            from docx import Document as DocxDocument
            d = DocxDocument(io.BytesIO(data))
            return '\n'.join(p.text for p in d.paragraphs)[:10000]
        else:
            return data.decode('utf-8', 'ignore')[:10000]
    except Exception:
        return ''

# ── Gemini: draft the technical narrative content ───────────────────────────────
# Content depth scales with the real scope of the quote instead of asking for
# the same fixed counts (4 feature cards, 6 architecture items, etc.) no
# matter how small or large the job is -- see _scope_tier()/SCOPE_TARGETS
# below. total_line_items already comes from get_equipment_summary(), which
# reads the quote's real bill of materials, so this can't be gamed by a
# verbose brief on a tiny job or vice versa.
SCOPE_TIERS = [  # (min_line_items, tier) — checked in order, first match wins
    (60, 'enterprise'),
    (25, 'large'),
    (10, 'medium'),
    (0,  'small'),
]
SCOPE_TARGETS = {
    'small':      {'feature_cards': 4, 'scope_cards': 4, 'control_testing': 2, 'architecture_items': 6,  'mobilization_phases': 3, 'exclusions': 3},
    'medium':     {'feature_cards': 4, 'scope_cards': 4, 'control_testing': 2, 'architecture_items': 6,  'mobilization_phases': 4, 'exclusions': 3},
    'large':      {'feature_cards': 6, 'scope_cards': 6, 'control_testing': 3, 'architecture_items': 9,  'mobilization_phases': 5, 'exclusions': 4},
    'enterprise': {'feature_cards': 8, 'scope_cards': 8, 'control_testing': 4, 'architecture_items': 12, 'mobilization_phases': 6, 'exclusions': 5},
}

def _scope_tier(equipment_summary):
    n = (equipment_summary or {}).get('total_line_items') or 0
    for min_items, tier in SCOPE_TIERS:
        if n >= min_items:
            return tier
    return 'small'

_TIER_GUIDANCE = {
    'small': "This is a SMALL, focused scope. Keep content tight and specific — don't pad it out with generic filler to look bigger than it is.",
    'medium': "This is a MODERATE scope covering several subsystems. Give each section clear, specific detail grounded in the real equipment.",
    'large': "This is a LARGE, multi-subsystem scope. Write a genuinely comprehensive proposal: break the architecture and scope out into more granular, specific items rather than broad generalizations.",
    'enterprise': "This is an ENTERPRISE-scale, multi-system deployment. Write an in-depth, comprehensive proposal that covers every major subsystem/category distinctly — favor specificity and granularity throughout rather than brevity.",
}

def _build_system_prompt(tier, targets):
    summary_len = '2-4 sentences' if tier in ('small', 'medium') else '4-6 sentences, genuinely comprehensive'
    note_len = '1-2 sentences' if tier in ('small', 'medium') else '2-4 sentences, covering how the subsystems integrate with each other'
    closing = ('Favor conciseness — this is a smaller job and padding it out with generic language will look inflated.'
               if tier == 'small' else
               'Favor genuine depth and specificity throughout — this is a substantial scope and the proposal should read as comprehensive, not a shortened summary.')
    return f"""You are a senior AV/IT solutions consultant drafting the TEXT CONTENT for a professional technical proposal document (not the pricing — that's handled separately). Given a project brief, optional reference material, and a summary of the actual equipment being proposed (brands/models/categories from a real bill of materials), draft compelling, specific, professional proposal content.

This project's scope has been assessed as {tier.upper()} based on its real bill of materials (line item count, section count, and brand diversity). {_TIER_GUIDANCE[tier]}

Return ONLY valid JSON matching exactly this shape (no markdown fences, no commentary):
{{
  "title": "Short punchy solution title, e.g. 'Signage & Interactive Flat Panel (IFP) Solution'",
  "subtitle": "One descriptive line about what the system does and where",
  "stats": [{{"label":"SHORT LABEL","value":"short value e.g. 3 or 20+"}} ...exactly 4 items, derived from the real equipment summary provided...],
  "executive_summary": "{summary_len} professional paragraph summarizing the solution",
  "feature_cards": [{{"title":"...", "description":"1-2 sentences"}} ...exactly {targets['feature_cards']} items, each describing a distinct key component/subsystem actually present in the equipment summary...],
  "scope_cards": [{{"title":"...", "description":"1-2 sentences"}} ...exactly {targets['scope_cards']} items describing distinct scope of work streams (supply, installation, integration, testing, training, etc.)...],
  "control_testing": [{{"title":"...","bullets":["...","..."]}} ...exactly {targets['control_testing']} items — distinct control/testing/assurance topics (e.g. Control & Automation, Testing & Commissioning, and for larger scopes also Redundancy & Failover, Cybersecurity & Network Hardening, Documentation & As-Builts)...],
  "architecture_items": [{{"label":"Category e.g. Display","value":"brands/models e.g. Samsung QM43C"}} ...exactly {targets['architecture_items']} items, one per distinct subsystem/category actually present in the equipment summary — for larger scopes, break categories out more granularly (e.g. separate Displays / Video Processing / Audio DSP / Amplification / Control / Network / Cabling Infrastructure / Power & Racks) rather than lumping them together...],
  "architecture_note": "{note_len}",
  "mobilization_phases": [{{"title":"Project Initiation","duration":"e.g. 3-5 business days, scaled to this project's real scope","bullets":["...","...","..."]}} ...exactly {targets['mobilization_phases']} phases, standard AV project rollout phases scaled to project complexity...],
  "warranty_years": "e.g. 1 Year or 3 Years, infer from context or default to 1 Year",
  "support_bullets": ["...", "...", "... 4-6 short bullet points of what's covered during warranty (more for larger scopes)"],
  "exclusions": [{{"title":"Mishandling / misuse","text":"..."}} ...exactly {targets['exclusions']} items...],
  "payment_terms": ["...", "... 3-5 short standard payment milestone bullets (e.g. advance on order confirmation, on delivery, on completion/handover), proportioned sensibly for this project's scale"],
  "general_notes": ["...", "... 4-6 short standard proposal disclaimer bullet points"]
}}

Ground every claim in the real equipment summary provided — never invent brands/models that aren't in it. Keep tone professional, matching enterprise AV/IT proposal writing. {closing}"""

def _gemini_request_with_retry(req):
    # Gemini occasionally returns 503 (overloaded) or 429 (rate-limited) even
    # under normal load. Retry a couple of times with a short backoff before
    # surfacing an error, instead of failing on the very first transient hiccup.
    last_err = None
    for attempt in range(3):
        try:
            return urllib.request.urlopen(req, timeout=55)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 2:
                last_err = e
                time.sleep(2 * (attempt + 1))
                continue
            if e.code == 429:
                raise RuntimeError('AI is rate-limited right now. Please wait a minute and try again.')
            detail = e.read().decode('utf-8', 'ignore')[:200]
            raise RuntimeError(f'AI service error ({e.code}). {detail}')
        except Exception as e:
            if attempt < 2:
                last_err = e
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError('AI service is unreachable right now. Please try again.')
    raise RuntimeError('AI service is currently overloaded. Please try again in a minute.')

def draft_proposal_content(brief, attachment_text, equipment_summary, currency, model=None):
    if not GEMINI_API_KEY:
        raise RuntimeError('Proposal generation is not configured yet (GEMINI_API_KEY is missing)')

    tier = _scope_tier(equipment_summary)
    targets = SCOPE_TARGETS[tier]
    system_prompt = _build_system_prompt(tier, targets)

    user_msg = json.dumps({
        'project_brief': brief[:4000],
        'reference_material': (attachment_text or '')[:6000],
        'equipment_summary': equipment_summary,
        'scope_tier': tier,
        'currency': currency,
    }, ensure_ascii=False)

    # Larger tiers are asked for meaningfully more content (more cards, more
    # architecture items, longer summaries) -- give them headroom so Gemini
    # doesn't truncate mid-JSON and produce an unparseable response.
    max_tokens = {'small': 8000, 'medium': 8000, 'large': 11000, 'enterprise': 14000}[tier]

    body = json.dumps({
        'systemInstruction': {'parts': [{'text': system_prompt}]},
        'contents': [{'role': 'user', 'parts': [{'text': user_msg}]}],
        'generationConfig': {
            'temperature': 0.4,
            'maxOutputTokens': max_tokens,
            'responseMimeType': 'application/json',
            'thinkingConfig': {'thinkingLevel': 'minimal'},
        },
    }).encode('utf-8')

    req = urllib.request.Request(_gemini_url_for(model), data=body, headers={
        'Content-Type': 'application/json', 'x-goog-api-key': GEMINI_API_KEY,
    }, method='POST')

    resp = _gemini_request_with_retry(req)
    data = json.loads(resp.read().decode('utf-8'))

    try:
        parts = data['candidates'][0]['content']['parts']
        text = ''.join(p.get('text', '') for p in parts).strip()
    except (KeyError, IndexError):
        raise RuntimeError('AI service returned an unexpected response. Please try again.')

    if text.startswith('```'):
        text = text.strip('`').strip()
        if text.lower().startswith('json'): text = text[4:].strip()

    try:
        parsed = json.loads(text)
    except Exception:
        raise RuntimeError('Could not parse the AI response. Please try again.')
    parsed['scope_tier'] = tier  # surfaced to the frontend so the review modal can show what depth was targeted
    return parsed

# ── Pull the linked quote's real data (never AI-invented) ───────────────────────
def get_equipment_summary(quote, which):
    opts = quote.get('quote_data') or []
    if which != 'all':
        try: opts = [opts[int(which)]]
        except: opts = opts[:1]
    if not opts: opts = [{'label': 'Option 1', 'sections': []}]

    brands = {}
    total_qty = 0
    section_names = []
    for o in opts:
        for s in (o.get('sections') or []):
            if s.get('name'): section_names.append(s['name'])
            for it in (s.get('items') or []):
                b = (it.get('brand') or '').strip()
                if b: brands[b] = brands.get(b, 0) + 1
                try: total_qty += int(_num(it.get('qty')))
                except: pass
    return {
        'sections': section_names,
        'brands': sorted(brands.keys()),
        'total_line_items': sum(len(s.get('items') or []) for o in opts for s in (o.get('sections') or [])),
        'total_qty': total_qty,
    }, opts

# ── Small flowable builders (Etihad-Rail-reference-style visual language) ──────
def section_badge(text, TH=None):
    TH = TH or _theme_for('modern')
    p = Paragraph(f'<font color="#{_hex(TH["navy"])}" size="7.5"><b>{text.upper()}</b></font>',
                  ParagraphStyle('badge', fontName='Helvetica-Bold', fontSize=7.5))
    t = Table([[p]], colWidths=[None])
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),TH['gold']),('TOPPADDING',(0,0),(-1,-1),4),
                           ('BOTTOMPADDING',(0,0),(-1,-1),4),('LEFTPADDING',(0,0),(-1,-1),8),
                           ('RIGHTPADDING',(0,0),(-1,-1),8)]))
    return t

def heading(text, S, TH=None):
    TH = TH or _theme_for('modern')
    return [Spacer(1, 2*mm), Paragraph(text, S['h1']),
            Table([['']], colWidths=[22*mm], rowHeights=[1.4],
                  style=TableStyle([('BACKGROUND',(0,0),(-1,-1),TH['gold'])])),
            Spacer(1, 3*mm)]

def _stat_val_size(v):
    # Shrinks the stat-card value font as text length grows, so longer
    # values (e.g. "4 Key Areas", "Maxhub & Sysconic") fit their column
    # instead of ReportLab hard-breaking mid-word (e.g. "4 Key Ar").
    L = len(str(v))
    if L <= 6: return 16
    if L <= 10: return 13
    if L <= 14: return 11
    return 9

def _fit_cover_stat_value(canvas, text, max_w, base_size, min_size=8, font='Helvetica-Bold'):
    """Cover-page stat card value: shrink the font to fit the card width
    instead of hard-truncating at a fixed character count (which silently cut
    real values like 'Full Turnkey' -> 'Full Tur' with no ellipsis, and made
    same-length-in-characters-but-different-width values like 'Maxhub' vs
    '3 Endpoints' render at visibly different effective sizes). Only falls
    back to a truncated value (with an ellipsis, so it's visibly incomplete
    rather than silently wrong) if it still doesn't fit at the minimum size."""
    text = str(text or '')
    size = base_size
    while size > min_size and canvas.stringWidth(text, font, size) > max_w:
        size -= 1
    if canvas.stringWidth(text, font, size) > max_w:
        while len(text) > 3 and canvas.stringWidth(text + '…', font, size) > max_w:
            text = text[:-1]
        text = text.rstrip() + '…'
    return text, size

def _fit_cover_stat_label(canvas, text, max_w, size, font='Helvetica-Bold'):
    """Cover-page stat card label: same truncate-with-ellipsis safety net as
    the value above, but labels stay at a fixed small size rather than
    shrinking further (they're already near the readability floor)."""
    text = str(text or '').upper()
    if canvas.stringWidth(text, font, size) <= max_w:
        return text
    while len(text) > 3 and canvas.stringWidth(text + '…', font, size) > max_w:
        text = text[:-1]
    return text.rstrip() + '…'

def stat_row(stats, CW, TH=None):
    TH = TH or _theme_for('modern')
    gold_hex = _hex(TH['gold']); light_hex = _hex(TH['lightblu'])
    n = max(1, len(stats))
    gap = 3*mm
    cw = (CW - gap*(n-1)) / n
    cells = []
    for s in stats:
        cells.append([Paragraph(f"<font color='#{gold_hex}' size='16'><b>{esc_p(s.get('value',''))}</b></font>", ParagraphStyle('sv', alignment=TA_CENTER, leading=19)),
                      Paragraph(f"<font color='#{light_hex}' size='6.5'><b>{esc_p(str(s.get('label','')).upper())}</b></font>", ParagraphStyle('sl', alignment=TA_CENTER, leading=8))])
    row = [Table([c], colWidths=[cw]) for c in [ [x] for x in cells ]]
    # Build as a single table with n columns, each cell a mini stacked Table
    inner_cells = []
    for s in stats:
        vsize = _stat_val_size(str(s.get('value','')))
        cell_tbl = Table([[Paragraph(f"<font color='#{gold_hex}' size='{vsize}'><b>{esc_p(str(s.get('value','')))}</b></font>", ParagraphStyle('sv2', alignment=TA_CENTER, leading=vsize+3))],
                           [Paragraph(f"<font color='#{light_hex}' size='6.5'><b>{esc_p(str(s.get('label','')).upper())}</b></font>", ParagraphStyle('sl2', alignment=TA_CENTER, leading=8))]],
                          colWidths=[cw])
        cell_tbl.setStyle(TableStyle([('ALIGN',(0,0),(-1,-1),'CENTER'),('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2)]))
        inner_cells.append(cell_tbl)
    outer = Table([inner_cells], colWidths=[cw]*n, spaceAfter=0)
    outer.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),TH['navy']),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
                               ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),
                               ('LINEAFTER',(0,0),(-2,0),0.5,TH['navy2'])]))
    return outer

def esc_p(s):
    return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

def _fixed_col_grid(items, CW, ncols, cell_builder, TH=None, colgap=4*mm, rowgap=3*mm, pad=10, box_w=0.6):
    """Lays `items` out into a grid of ncols columns, one flowable-list per card built
    by cell_builder(item). BACKGROUND/BOX are applied per-cell directly on THIS single
    outer table (via exact-cell TableStyle coordinates), not on a nested per-card Table --
    that's what makes every card in a row stretch its background/border to match the
    tallest card in that row when descriptions wrap to different line counts, instead of
    each card's box only covering its own (shorter) content height.
    Real gutters between cards are separate spacer columns/rows (not padding), so cards
    stay visually distinct with the page background showing through between them.
    """
    TH = TH or _theme_for('modern')
    n = len(items)
    cw = (CW - colgap*(ncols-1)) / ncols
    colWidths = []
    for i in range(ncols):
        colWidths.append(cw)
        if i < ncols-1: colWidths.append(colgap)
    total_cols = len(colWidths)
    nrows = (n + ncols - 1) // ncols
    data = []
    style_cmds = [('VALIGN',(0,0),(-1,-1),'TOP'),
                  ('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),
                  ('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),0)]
    for r in range(nrows):
        if r > 0:
            data.append([Spacer(1, rowgap)] * total_cols)
        row_idx = len(data)
        row_cells = [''] * total_cols
        for c in range(ncols):
            idx = r*ncols + c
            if idx >= n: continue
            col = c * 2
            row_cells[col] = cell_builder(items[idx])
            style_cmds += [
                ('BACKGROUND',(col,row_idx),(col,row_idx),TH['cardbg']),
                ('BOX',(col,row_idx),(col,row_idx),box_w,TH['grayb']),
                ('TOPPADDING',(col,row_idx),(col,row_idx),pad),
                ('BOTTOMPADDING',(col,row_idx),(col,row_idx),pad),
                ('LEFTPADDING',(col,row_idx),(col,row_idx),pad),
                ('RIGHTPADDING',(col,row_idx),(col,row_idx),pad),
            ]
        data.append(row_cells)
    data.append([Spacer(1, rowgap)] * total_cols)  # trailing margin before whatever follows
    t = Table(data, colWidths=colWidths)
    t.setStyle(TableStyle(style_cmds))
    return t

def card_grid(cards, CW, accent=None, TH=None):
    """2-column grid of feature/scope cards, each with a small accent block + title + description."""
    TH = TH or _theme_for('modern')
    accent = accent or TH['gold']
    S_title = ParagraphStyle('ct', fontName='Helvetica-Bold', fontSize=9.5, textColor=TH['navy'], leading=12)
    S_desc  = ParagraphStyle('cd', fontName='Helvetica', fontSize=8, textColor=TH['txt'], leading=11)
    def _build(c):
        return [
            Table([['']], colWidths=[6*mm], rowHeights=[6*mm], style=TableStyle([('BACKGROUND',(0,0),(-1,-1),accent)])),
            Spacer(1, 3*mm),
            Paragraph(esc_p(c.get('title','')), S_title),
            Spacer(1, 2*mm),
            Paragraph(esc_p(c.get('description','')), S_desc),
        ]
    return _fixed_col_grid(cards, CW, 2, _build, TH=TH, colgap=4*mm, pad=10)

def numbered_phases(phases, CW, TH=None):
    TH = TH or _theme_for('modern')
    S_title = ParagraphStyle('pt', fontName='Helvetica-Bold', fontSize=9.5, textColor=TH['navy'], leading=12)
    S_bul   = ParagraphStyle('pb', fontName='Helvetica', fontSize=8, textColor=TH['txt'], leading=11)
    rows = []
    for i, ph in enumerate(phases):
        num = Table([[Paragraph(f"<font color='white' size='11'><b>{i+1}</b></font>", ParagraphStyle('num', alignment=TA_CENTER))]],
                    colWidths=[9*mm], rowHeights=[9*mm])
        num.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),TH['navy']),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,-1),'CENTER')]))
        bullets = '<br/>'.join(f"•  {esc_p(b)}" for b in (ph.get('bullets') or []))
        title_html = esc_p(ph.get('title',''))
        if ph.get('duration'):
            title_html += f"  <font color='#{_hex(TH['gray'])}' size='8'>· {esc_p(ph['duration'])}</font>"
        body = [Paragraph(title_html, S_title), Paragraph(bullets, S_bul)]
        rows.append([num, body])
    t = Table(rows, colWidths=[12*mm, CW-12*mm])
    t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),4),
                          ('BOTTOMPADDING',(0,0),(-1,-1),8),('LEFTPADDING',(0,0),(-1,-1),0)]))
    return t

BOM_IMG_COL_MM = 15  # width of the BOM table's "Img" column
BOM_IMG_BOX_MM = 12  # square box the thumbnail itself is fit inside
BOM_IMG_FETCH_TIMEOUT = 5      # per-image network timeout (seconds)
BOM_IMG_MAX_COUNT = 40         # hard cap on distinct product photos fetched per document
BOM_IMG_TIME_BUDGET_SECONDS = 20  # wall-clock budget for ALL image fetches combined across one render

# api/proposal.py has a 60s maxDuration on Vercel (vercel.json) covering the AI
# draft call AND, separately, PDF rendering -- a BOM with many distinct product
# photos fetched serially could otherwise burn most of that budget on network
# I/O alone. The cap + time budget below make a missing/slow photo degrade
# gracefully (blank cell) instead of risking a function timeout on a large BOM.

def _bom_item_image(url, cache):
    """Fetches a product photo (the same signed product-images URL saved on the
    quote line as it.img) for one BOM row and returns a small fixed-size
    ReportLab Image flowable, or '' if there's no image, the fetch/decode
    fails, or the per-document image budget (count or wall-clock time) has
    been exhausted -- a missing/broken product photo must never break BOM
    generation or risk timing out the whole PDF render.
    `cache` is a dict shared across every bom_table() call within one PDF
    render so the same product repeated across sections/options is only
    downloaded once, and also carries the shared '__budget__' tracker."""
    if not url:
        return ''
    if url in cache:
        return cache[url]
    budget = cache.setdefault('__budget__', {'deadline': time.time() + BOM_IMG_TIME_BUDGET_SECONDS, 'fetched': 0})
    if budget['fetched'] >= BOM_IMG_MAX_COUNT or time.time() >= budget['deadline']:
        cache[url] = ''  # budget exhausted -- stop fetching further images for the rest of this render
        return ''
    flow = ''
    try:
        img_bytes = _fetch_bytes(url, timeout=BOM_IMG_FETCH_TIMEOUT)
        if img_bytes:
            reader = ImageReader(io.BytesIO(img_bytes))
            iw, ih = reader.getSize()
            if iw and ih:
                box = BOM_IMG_BOX_MM * mm
                dw, dh = _img_contain_rect(iw, ih, box, box)
                flow = Image(io.BytesIO(img_bytes), width=dw, height=dh)
    except Exception:
        flow = ''
    budget['fetched'] += 1
    cache[url] = flow
    return flow

def bom_table(sections, CW, cur, with_price, pricing_type='markup', TH=None, img_cache=None):
    TH = TH or _theme_for('modern')
    if img_cache is None: img_cache = {}
    S_th = ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=7.8, textColor=white)
    S_b  = ParagraphStyle('b', fontName='Helvetica', fontSize=8, textColor=TH['txt'], leading=10)
    S_bc = ParagraphStyle('bc', fontName='Helvetica', fontSize=8, textColor=TH['txt'], alignment=TA_CENTER)
    S_br = ParagraphStyle('br', fontName='Helvetica', fontSize=8, textColor=TH['txt'], alignment=TA_RIGHT)
    flow = []
    img_w = BOM_IMG_COL_MM * mm
    rem = CW - img_w
    for s in sections:
        if s.get('name'):
            flow.append(Paragraph(f"<b>{esc_p(s['name'])}</b>", ParagraphStyle('sn', fontName='Helvetica-Bold', fontSize=10, textColor=TH['navy'])))
            flow.append(Spacer(1, 1.5*mm))
        if with_price:
            hdr = ['#','Img','Brand','Model','Description', f'Unit Price ({cur})', 'Qty', 'UOM', f'Total ({cur})']
            pct = [0.04, 0.10, 0.13, 0.32, 0.13, 0.06, 0.06, 0.16]
        else:
            hdr = ['#','Img','Brand','Model','Description','Qty','UOM']
            pct = [0.05, 0.15, 0.19, 0.44, 0.08, 0.09]
        widths = [rem*pct[0], img_w] + [rem*p for p in pct[1:]]
        rows = [[Paragraph(h, S_th) for h in hdr]]
        rn = 1
        for it in (s.get('items') or []):
            up, tp = calc_item(it, pricing_type)
            img_flow = _bom_item_image(it.get('img'), img_cache)
            if with_price:
                rows.append([Paragraph(str(rn), S_bc), img_flow, Paragraph(esc_p(it.get('brand') or ''), S_b),
                             Paragraph(esc_p(it.get('model') or ''), S_b), Paragraph(esc_p(it.get('desc') or ''), S_b),
                             Paragraph(fmt(up), S_br), Paragraph(fmt(_num(it.get('qty'))), S_bc),
                             Paragraph(esc_p(it.get('uom') or 'Pcs'), S_bc), Paragraph(fmt(tp), S_br)])
            else:
                rows.append([Paragraph(str(rn), S_bc), img_flow, Paragraph(esc_p(it.get('brand') or ''), S_b),
                             Paragraph(esc_p(it.get('model') or ''), S_b), Paragraph(esc_p(it.get('desc') or ''), S_b),
                             Paragraph(fmt(_num(it.get('qty'))), S_bc), Paragraph(esc_p(it.get('uom') or 'Pcs'), S_bc)])
            rn += 1
        tbl = Table(rows, colWidths=widths, repeatRows=1)
        style = [('BACKGROUND',(0,0),(-1,0),TH['navy']),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
                 ('ALIGN',(1,0),(1,-1),'CENTER'),
                 ('LINEBELOW',(0,1),(-1,-1),0.4,TH['grayb']),('TOPPADDING',(0,0),(-1,-1),4.5),
                 ('BOTTOMPADDING',(0,0),(-1,-1),4.5),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5)]
        for ri in range(1, len(rows)):
            if (ri-1) % 2 == 1:
                style.append(('BACKGROUND',(0,ri),(-1,ri),TH['zebra']))
        tbl.setStyle(TableStyle(style))
        flow.append(tbl); flow.append(Spacer(1, 5*mm))
    return flow

# ── Cover page (drawn directly on the canvas — content is fixed, not flowing) ───
MAX_COVER_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB decoded

def _cover_image_reader(data_url):
    """Decode a base64 cover-photo data URL (uploaded client-side, same
    pattern as the schematic image) into a ReportLab ImageReader. Returns
    None on any problem -- the proposal must never fail because of a
    bad/missing cover image."""
    try:
        if not data_url:
            return None
        raw = data_url
        if ',' in raw: raw = raw.split(',', 1)[1]
        import base64
        img_bytes = base64.b64decode(raw)
        if not img_bytes or len(img_bytes) > MAX_COVER_IMAGE_BYTES:
            return None
        reader = ImageReader(io.BytesIO(img_bytes))
        if not reader.getSize()[0]:
            return None
        return reader
    except Exception:
        return None

def _img_cover_rect(iw, ih, box_w, box_h):
    """(draw_w, draw_h) that fully covers box_w x box_h, cropping overflow."""
    img_ratio = iw / float(ih); box_ratio = box_w / float(box_h)
    if img_ratio > box_ratio:
        draw_h = box_h; draw_w = draw_h * img_ratio
    else:
        draw_w = box_w; draw_h = draw_w / img_ratio
    return draw_w, draw_h

def _img_contain_rect(iw, ih, box_w, box_h):
    """(draw_w, draw_h) that fits inside box_w x box_h, letterboxed (no crop)."""
    img_ratio = iw / float(ih); box_ratio = box_w / float(box_h)
    if img_ratio > box_ratio:
        draw_w = box_w; draw_h = draw_w / img_ratio
    else:
        draw_h = box_h; draw_w = draw_h * img_ratio
    return draw_w, draw_h

COVER_STYLES = tuple(THEMES.keys())

def _draw_cover(canvas, doc):
    """Dispatches to the selected template style's cover LAYOUT (see
    COVER_LAYOUT_FOR_STYLE), drawn with that style's THEME colors. 'modern'
    (default) matches the original design exactly when no cover image is
    attached."""
    data = doc.proposal_data
    style = data.get('template_style') or 'modern'
    if style not in COVER_STYLES: style = 'modern'
    TH = _theme_for(style)
    cover_img = _cover_image_reader(data.get('cover_image'))
    {
        'modern': _draw_cover_modern,
        'corporate': _draw_cover_corporate,
        'technical': _draw_cover_technical,
        'executive': _draw_cover_executive,
    }[COVER_LAYOUT_FOR_STYLE.get(style, 'modern')](canvas, doc, cover_img, TH)

def _draw_cover_modern(canvas, doc, cover_img, TH=None):
    TH = TH or _theme_for('modern')
    data = doc.proposal_data; company = doc.proposal_company; logo_bytes = doc.proposal_logo
    w, h = A4
    canvas.saveState()
    if cover_img:
        try:
            iw, ih = cover_img.getSize()
            dw, dh = _img_cover_rect(iw, ih, w, h)
            canvas.drawImage(cover_img, (w-dw)/2, (h-dh)/2, dw, dh, mask='auto')
        except Exception:
            cover_img = None
        canvas.setFillColor(TH['navy']); canvas.setFillAlpha(0.60 if cover_img else 1.0)
        canvas.rect(0, 0, w, h, fill=1, stroke=0)
    else:
        canvas.setFillColor(TH['navy']); canvas.rect(0, 0, w, h, fill=1, stroke=0)
    canvas.restoreState()
    # Decorative circles removed -- they clipped/washed out the "Prepared
    # for" client-name card that used to float in this same top-right area,
    # producing an unreadable half-navy/half-white collision. Flat navy
    # background now, so nothing behind the header row can fight with it.

    if logo_bytes:
        try:
            canvas.saveState()
            canvas.setFillColor(white)
            canvas.roundRect(15*mm, h-38*mm, 46*mm, 18*mm, 3*mm, fill=1, stroke=0)
            img = ImageReader(io.BytesIO(logo_bytes))
            iw, ih = img.getSize(); ratio = iw/float(ih)
            dh = 11*mm; dw = dh*ratio
            if dw > 38*mm: dw = 38*mm; dh = dw/ratio
            canvas.drawImage(img, 15*mm+(46*mm-dw)/2, h-38*mm+(18*mm-dh)/2, dw, dh, mask='auto')
            canvas.restoreState()
        except Exception:
            pass

    # Prominent client name -- solid white card mirroring the logo box on
    # the opposite side of the same header row. Guarantees contrast no
    # matter what's behind it (navy fill, a cover photo, anything), unlike
    # the previous floating text which had no such guarantee.
    client_name = (data.get('customer_name') or '').strip()
    if client_name:
        # Card width follows the actual text instead of a fixed guess --
        # shrink the font first (down to 8pt) if the name is long, and only
        # truncate with an ellipsis as a last resort once it still can't fit
        # in the space available to the right of the logo box.
        max_card_w = w - 15*mm - (15*mm + 46*mm + 8*mm)
        pad_x = 8
        name_txt = client_name
        fs = 11
        while fs > 8 and canvas.stringWidth(name_txt, 'Helvetica-Bold', fs) + pad_x*2 > max_card_w:
            fs -= 1
        while canvas.stringWidth(name_txt, 'Helvetica-Bold', fs) + pad_x*2 > max_card_w and len(name_txt) > 4:
            name_txt = name_txt[:-1]
        if name_txt != client_name:
            name_txt = name_txt.rstrip() + '…'
        card_w = min(max_card_w, max(60*mm, canvas.stringWidth(name_txt, 'Helvetica-Bold', fs) + pad_x*2))
        card_h = 18*mm
        card_x, card_y = w-15*mm-card_w, h-38*mm
        canvas.setFillColor(white); canvas.roundRect(card_x, card_y, card_w, card_h, 3*mm, fill=1, stroke=0)
        canvas.setFont('Helvetica', 7); canvas.setFillColor(TH['gray'])
        canvas.drawString(card_x+pad_x, card_y+card_h-6.5*mm, 'PREPARED FOR')
        canvas.setFont('Helvetica-Bold', fs); canvas.setFillColor(TH['navy'])
        canvas.drawString(card_x+pad_x, card_y+4.5*mm, name_txt)

    badge = (data.get('doc_label') or 'TECHNICAL PROPOSAL').upper()
    canvas.setFont('Helvetica-Bold', 9)
    bw = canvas.stringWidth(badge, 'Helvetica-Bold', 9) + 16
    by = h - 96*mm
    canvas.setFillColor(TH['gold']); canvas.roundRect(15*mm, by, bw, 8*mm, 4*mm, fill=1, stroke=0)
    canvas.setFillColor(TH['navy']); canvas.drawString(15*mm+8, by+2.6*mm, badge)

    canvas.setFillColor(white); canvas.setFont('Helvetica-Bold', 25)
    ty = h - 118*mm
    for ln in textwrap.wrap(data.get('title') or 'Technical Proposal', width=26)[:3]:
        canvas.drawString(15*mm, ty, ln); ty -= 10.5*mm

    canvas.setFont('Helvetica', 10.5); canvas.setFillColor(TH['lightblu'])
    for ln in textwrap.wrap(data.get('subtitle') or '', width=78)[:2]:
        ty -= 6*mm
        canvas.drawString(15*mm, ty, ln)

    stats = (data.get('stats') or [])[:4]
    if stats:
        n = len(stats); gap = 8*mm
        cw = (w - 30*mm - gap*(n-1)) / n
        cx = 15*mm; cy = 52*mm
        canvas.saveState(); canvas.setFillAlpha(0.13)
        for s in stats:
            canvas.setFillColor(white)
            canvas.roundRect(cx, cy, cw, 24*mm, 2*mm, fill=1, stroke=0)
            cx += cw + gap
        canvas.restoreState()
        cx = 15*mm
        for s in stats:
            max_w = cw - 6*mm
            vtxt, vsize = _fit_cover_stat_value(canvas, s.get('value',''), max_w, 16)
            canvas.setFillColor(TH['gold']); canvas.setFont('Helvetica-Bold', vsize)
            canvas.drawCentredString(cx+cw/2, cy+14*mm, vtxt)
            ltxt = _fit_cover_stat_label(canvas, s.get('label',''), max_w, 6.3)
            canvas.setFillColor(TH['lightblu']); canvas.setFont('Helvetica-Bold', 6.3)
            canvas.drawCentredString(cx+cw/2, cy+6.5*mm, ltxt)
            cx += cw + gap

    canvas.setStrokeColor(TH['navy2']); canvas.setLineWidth(0.6)
    canvas.line(15*mm, 30*mm, w-15*mm, 30*mm)
    canvas.setFont('Helvetica', 8); canvas.setFillColor(TH['lightblu'])
    canvas.drawString(15*mm, 24*mm, 'Prepared for')
    canvas.setFont('Helvetica-Bold', 10.5); canvas.setFillColor(white)
    canvas.drawString(15*mm, 18.5*mm, (data.get('customer_name') or '')[:60])
    canvas.setFont('Helvetica', 8); canvas.setFillColor(TH['lightblu'])
    canvas.drawRightString(w-15*mm, 24*mm, f"Date: {data.get('date') or ''}")
    canvas.setFont('Helvetica-Bold', 9); canvas.setFillColor(white)
    canvas.drawRightString(w-15*mm, 18.5*mm, f"Prepared by: {(data.get('prepared_by') or company.get('name') or '')[:50]}")

def _draw_cover_corporate(canvas, doc, cover_img, TH=None):
    TH = TH or _theme_for('corporate')
    data = doc.proposal_data; company = doc.proposal_company; logo_bytes = doc.proposal_logo
    w, h = A4
    canvas.saveState()
    canvas.setFillColor(white); canvas.rect(0, 0, w, h, fill=1, stroke=0)
    canvas.restoreState()
    canvas.setStrokeColor(TH['gold']); canvas.setLineWidth(2.2)
    canvas.line(15*mm, h-20*mm, w-15*mm, h-20*mm)

    if logo_bytes:
        try:
            img = ImageReader(io.BytesIO(logo_bytes))
            iw, ih = img.getSize(); ratio = iw/float(ih)
            dh = 12*mm; dw = dh*ratio
            if dw > 44*mm: dw = 44*mm; dh = dw/ratio
            canvas.drawImage(img, 15*mm, h-20*mm-dh-6*mm, dw, dh, mask='auto')
        except Exception:
            pass

    client_name = (data.get('customer_name') or '').strip()
    if client_name:
        canvas.setFont('Helvetica', 8); canvas.setFillColor(TH['gray'])
        canvas.drawRightString(w-15*mm, h-24*mm, 'PREPARED FOR')
        canvas.setFont('Helvetica-Bold', 14); canvas.setFillColor(TH['navy'])
        canvas.drawRightString(w-15*mm, h-31*mm, client_name[:45])

    badge = (data.get('doc_label') or 'TECHNICAL PROPOSAL').upper()
    canvas.setFont('Helvetica-Bold', 8.5)
    bw = canvas.stringWidth(badge, 'Helvetica-Bold', 8.5) + 16
    by = h - 96*mm
    canvas.setStrokeColor(TH['navy']); canvas.setLineWidth(0.9)
    canvas.roundRect(15*mm, by, bw, 7*mm, 3.5*mm, fill=0, stroke=1)
    canvas.setFillColor(TH['navy']); canvas.drawString(15*mm+8, by+2.3*mm, badge)

    canvas.setFillColor(TH['navy']); canvas.setFont('Helvetica-Bold', 23)
    ty = h - 118*mm
    for ln in textwrap.wrap(data.get('title') or 'Technical Proposal', width=28)[:3]:
        canvas.drawString(15*mm, ty, ln); ty -= 9.5*mm

    canvas.setFont('Helvetica', 10); canvas.setFillColor(TH['gray'])
    for ln in textwrap.wrap(data.get('subtitle') or '', width=82)[:2]:
        ty -= 6*mm
        canvas.drawString(15*mm, ty, ln)

    if cover_img:
        try:
            iw, ih = cover_img.getSize()
            box_w, box_h = w - 30*mm, 46*mm
            box_y = 82*mm
            dw, dh = _img_contain_rect(iw, ih, box_w - 4*mm, box_h - 4*mm)
            canvas.setStrokeColor(TH['grayb']); canvas.setLineWidth(0.8)
            canvas.rect(15*mm, box_y, box_w, box_h, fill=0, stroke=1)
            canvas.drawImage(cover_img, 15*mm+(box_w-dw)/2, box_y+(box_h-dh)/2, dw, dh, mask='auto')
        except Exception:
            pass

    stats = (data.get('stats') or [])[:4]
    if stats:
        n = len(stats); gap = 8*mm
        cw = (w - 30*mm - gap*(n-1)) / n
        cx = 15*mm; cy = 52*mm
        for s in stats:
            canvas.setFillColor(TH['tint'])
            canvas.roundRect(cx, cy, cw, 24*mm, 2*mm, fill=1, stroke=0)
            max_w = cw - 6*mm
            vtxt, vsize = _fit_cover_stat_value(canvas, s.get('value',''), max_w, 15)
            canvas.setFillColor(TH['navy']); canvas.setFont('Helvetica-Bold', vsize)
            canvas.drawCentredString(cx+cw/2, cy+14*mm, vtxt)
            ltxt = _fit_cover_stat_label(canvas, s.get('label',''), max_w, 6.2)
            canvas.setFillColor(TH['gray']); canvas.setFont('Helvetica-Bold', 6.2)
            canvas.drawCentredString(cx+cw/2, cy+6.5*mm, ltxt)
            cx += cw + gap

    canvas.setStrokeColor(TH['grayb']); canvas.setLineWidth(0.6)
    canvas.line(15*mm, 30*mm, w-15*mm, 30*mm)
    canvas.setFont('Helvetica', 8); canvas.setFillColor(TH['gray'])
    canvas.drawString(15*mm, 24*mm, 'Prepared for')
    canvas.setFont('Helvetica-Bold', 10.5); canvas.setFillColor(TH['navy'])
    canvas.drawString(15*mm, 18.5*mm, (data.get('customer_name') or '')[:60])
    canvas.setFont('Helvetica', 8); canvas.setFillColor(TH['gray'])
    canvas.drawRightString(w-15*mm, 24*mm, f"Date: {data.get('date') or ''}")
    canvas.setFont('Helvetica-Bold', 9); canvas.setFillColor(TH['navy'])
    canvas.drawRightString(w-15*mm, 18.5*mm, f"Prepared by: {(data.get('prepared_by') or company.get('name') or '')[:50]}")

def _draw_cover_technical(canvas, doc, cover_img, TH=None):
    TH = TH or _theme_for('technical')
    data = doc.proposal_data; company = doc.proposal_company; logo_bytes = doc.proposal_logo
    w, h = A4
    SB = 34*mm
    canvas.saveState()
    canvas.setFillColor(white); canvas.rect(0, 0, w, h, fill=1, stroke=0)
    canvas.setFillColor(TH['navy']); canvas.rect(0, 0, SB, h, fill=1, stroke=0)
    canvas.restoreState()

    badge = (data.get('doc_label') or 'TECHNICAL PROPOSAL').upper()
    canvas.saveState()
    canvas.translate(SB/2 + 3, h/2)
    canvas.rotate(90)
    canvas.setFillColor(TH['gold']); canvas.setFont('Helvetica-Bold', 10)
    canvas.drawCentredString(0, 0, badge)
    canvas.restoreState()

    if logo_bytes:
        try:
            img = ImageReader(io.BytesIO(logo_bytes))
            iw, ih = img.getSize(); ratio = iw/float(ih)
            dw = SB - 10*mm; dh = dw/ratio
            if dh > 16*mm: dh = 16*mm; dw = dh*ratio
            canvas.drawImage(img, (SB-dw)/2, h-16*mm-dh, dw, dh, mask='auto')
        except Exception:
            pass

    MX = SB + 15*mm
    CWd = w - MX - 15*mm

    client_name = (data.get('customer_name') or '').strip()
    if client_name:
        canvas.setFont('Helvetica', 8); canvas.setFillColor(TH['gray'])
        canvas.drawRightString(w-15*mm, h-16*mm, 'PREPARED FOR')
        canvas.setFont('Helvetica-Bold', 13); canvas.setFillColor(TH['navy'])
        canvas.drawRightString(w-15*mm, h-22*mm, client_name[:40])

    canvas.setFillColor(TH['navy']); canvas.setFont('Helvetica-Bold', 20)
    ty = h - 40*mm
    for ln in textwrap.wrap(data.get('title') or 'Technical Proposal', width=32)[:3]:
        canvas.drawString(MX, ty, ln); ty -= 8.5*mm

    canvas.setFont('Helvetica', 9.5); canvas.setFillColor(TH['gray'])
    for ln in textwrap.wrap(data.get('subtitle') or '', width=88)[:2]:
        ty -= 5.5*mm
        canvas.drawString(MX, ty, ln)

    ty -= 6*mm
    canvas.setStrokeColor(TH['grayb']); canvas.setLineWidth(0.6)
    canvas.line(MX, ty, w-15*mm, ty)

    if cover_img:
        try:
            iw, ih = cover_img.getSize()
            box_w, box_h = CWd, 60*mm
            box_y = ty - 8*mm - box_h
            dw, dh = _img_cover_rect(iw, ih, box_w, box_h)
            canvas.saveState()
            p = canvas.beginPath(); p.rect(MX, box_y, box_w, box_h)
            canvas.clipPath(p, stroke=0, fill=0)
            canvas.drawImage(cover_img, MX+(box_w-dw)/2, box_y+(box_h-dh)/2, dw, dh, mask='auto')
            canvas.restoreState()
            canvas.setStrokeColor(TH['grayb']); canvas.setLineWidth(0.6)
            canvas.rect(MX, box_y, box_w, box_h, fill=0, stroke=1)
        except Exception:
            pass

    stats = (data.get('stats') or [])[:4]
    if stats:
        n = len(stats); gap = 6*mm
        cw = (CWd - gap*(n-1)) / n
        cx = MX; cy = 30*mm
        for s in stats:
            canvas.setFillColor(TH['cardbg']); canvas.rect(cx, cy, cw, 20*mm, fill=1, stroke=0)
            max_w = cw - 6*mm
            vtxt, vsize = _fit_cover_stat_value(canvas, s.get('value',''), max_w, 13)
            canvas.setFillColor(TH['navy']); canvas.setFont('Helvetica-Bold', vsize)
            canvas.drawCentredString(cx+cw/2, cy+12*mm, vtxt)
            ltxt = _fit_cover_stat_label(canvas, s.get('label',''), max_w, 5.8)
            canvas.setFillColor(TH['gray']); canvas.setFont('Helvetica-Bold', 5.8)
            canvas.drawCentredString(cx+cw/2, cy+5.5*mm, ltxt)
            cx += cw + gap

    canvas.setFont('Helvetica', 7.5); canvas.setFillColor(TH['gray'])
    canvas.drawString(MX, 14*mm, f"Prepared for {(data.get('customer_name') or '')[:50]}")
    canvas.drawRightString(w-15*mm, 14*mm, f"Date: {data.get('date') or ''}")

def _draw_cover_executive(canvas, doc, cover_img, TH=None):
    TH = TH or _theme_for('executive')
    data = doc.proposal_data; company = doc.proposal_company; logo_bytes = doc.proposal_logo
    w, h = A4
    canvas.saveState()
    canvas.setFillColor(white); canvas.rect(0, 0, w, h, fill=1, stroke=0)
    canvas.restoreState()

    if logo_bytes:
        try:
            img = ImageReader(io.BytesIO(logo_bytes))
            iw, ih = img.getSize(); ratio = iw/float(ih)
            dh = 13*mm; dw = dh*ratio
            if dw > 46*mm: dw = 46*mm; dh = dw/ratio
            canvas.drawImage(img, (w-dw)/2, h-45*mm, dw, dh, mask='auto')
        except Exception:
            pass

    client_name = (data.get('customer_name') or '').strip()
    if client_name:
        canvas.setFont('Helvetica', 8); canvas.setFillColor(TH['gray'])
        canvas.drawCentredString(w/2, h-50*mm, 'PREPARED FOR')
        canvas.setFont('Helvetica-Bold', 13); canvas.setFillColor(TH['navy'])
        canvas.drawCentredString(w/2, h-56*mm, client_name[:45])

    badge = (data.get('doc_label') or 'TECHNICAL PROPOSAL').upper()
    canvas.setFont('Helvetica-Bold', 8); canvas.setFillColor(TH['gray'])
    canvas.drawCentredString(w/2, h-62*mm, badge)

    canvas.setFillColor(TH['navy']); canvas.setFont('Helvetica-Bold', 26)
    ty = h - 90*mm
    for ln in textwrap.wrap(data.get('title') or 'Technical Proposal', width=24)[:3]:
        canvas.drawCentredString(w/2, ty, ln); ty -= 11*mm

    canvas.setFont('Helvetica', 10.5); canvas.setFillColor(TH['gray'])
    for ln in textwrap.wrap(data.get('subtitle') or '', width=70)[:2]:
        ty -= 6.5*mm
        canvas.drawCentredString(w/2, ty, ln)

    canvas.setStrokeColor(TH['gold']); canvas.setLineWidth(1.4)
    canvas.line(w/2-14*mm, ty-8*mm, w/2+14*mm, ty-8*mm)

    stats = (data.get('stats') or [])[:2]
    if stats:
        n = len(stats); gap = 14*mm; cw = 42*mm
        total_w = cw*n + gap*(n-1)
        cx = (w-total_w)/2; cy = ty - 34*mm
        for s in stats:
            max_w = cw - 6*mm
            vtxt, vsize = _fit_cover_stat_value(canvas, s.get('value',''), max_w, 17)
            canvas.setFillColor(TH['navy']); canvas.setFont('Helvetica-Bold', vsize)
            canvas.drawCentredString(cx+cw/2, cy+7*mm, vtxt)
            ltxt = _fit_cover_stat_label(canvas, s.get('label',''), max_w, 6.5)
            canvas.setFillColor(TH['gray']); canvas.setFont('Helvetica-Bold', 6.5)
            canvas.drawCentredString(cx+cw/2, cy, ltxt)
            cx += cw + gap

    if cover_img:
        try:
            iw, ih = cover_img.getSize()
            box_w, box_h = 60*mm, 34*mm
            box_x, box_y = (w-box_w)/2, 26*mm
            dw, dh = _img_contain_rect(iw, ih, box_w, box_h)
            canvas.drawImage(cover_img, box_x+(box_w-dw)/2, box_y+(box_h-dh)/2, dw, dh, mask='auto')
        except Exception:
            pass

    canvas.setStrokeColor(TH['grayb']); canvas.setLineWidth(0.6)
    canvas.line(15*mm, 16*mm, w-15*mm, 16*mm)
    canvas.setFont('Helvetica', 8); canvas.setFillColor(TH['gray'])
    canvas.drawCentredString(w/2, 11*mm, f"Prepared for {(data.get('customer_name') or '')[:40]}  ·  {data.get('date') or ''}")

def _content_footer(canvas, doc):
    company = doc.proposal_company
    style = doc.proposal_data.get('template_style') or 'modern'
    TH = _theme_for(style)
    w, h = A4
    canvas.saveState()
    canvas.setStrokeColor(TH['gold']); canvas.setLineWidth(1.1)
    canvas.line(15*mm, 14*mm, w-15*mm, 14*mm)
    canvas.setFont('Helvetica', 6.8); canvas.setFillColor(TH['gray'])
    label = doc.proposal_data.get('doc_label') or 'TECHNICAL PROPOSAL'
    canvas.drawString(15*mm, 10*mm, f"{label} · {company.get('name','')}"[:110])
    canvas.setFont('Helvetica', 6.8); canvas.setFillColor(TH['gray'])
    canvas.drawCentredString(w/2, 6*mm, f"Page {doc.page}")
    if company.get('web'):
        canvas.setFont('Helvetica-Bold', 6.8); canvas.setFillColor(TH['navy2'])
        canvas.drawRightString(w-15*mm, 10*mm, company['web'])
    canvas.restoreState()

# ── Full document builder ────────────────────────────────────────────────────────
def _section_on(content, key):
    return (content.get('sections_enabled') or {}).get(key, True)

MAX_SCHEMATIC_BYTES = 4 * 1024 * 1024  # 4 MB decoded

def _schematic_image(data_url, CW):
    """Decode a base64 PNG data URL into a ReportLab Image scaled to the
    content width (capped to a portrait-page-friendly height). Returns None
    on any problem — the proposal must never fail because of the diagram."""
    try:
        raw = data_url
        if ',' in raw: raw = raw.split(',', 1)[1]
        import base64
        img_bytes = base64.b64decode(raw)
        if not img_bytes or len(img_bytes) > MAX_SCHEMATIC_BYTES:
            return None
        reader = ImageReader(io.BytesIO(img_bytes))
        iw, ih = reader.getSize()
        if not iw or not ih:
            return None
        w = CW
        h = w * ih / float(iw)
        max_h = 200*mm
        if h > max_h:
            h = max_h
            w = h * iw / float(ih)
        return Image(io.BytesIO(img_bytes), width=w, height=h)
    except Exception:
        return None

def build_proposal_pdf(kind, content, quote, opts, company, logo_bytes, cur, doc_label):
    """kind: 'technical' | 'commercial' | 'combined'"""
    pt = (quote or {}).get('pricing_type') or 'markup'
    TH = _theme_for(content.get('template_style') or 'modern')
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=15*mm, rightMargin=15*mm,
                          topMargin=14*mm, bottomMargin=18*mm, title=content.get('title', 'Proposal'))
    doc.proposal_data = dict(content); doc.proposal_data['doc_label'] = doc_label
    doc.proposal_company = company
    doc.proposal_logo = logo_bytes

    cover_frame = Frame(0, 0, A4[0], A4[1], id='cover')
    content_frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='c')
    doc.addPageTemplates([
        PageTemplate(id='cover', frames=[cover_frame], onPage=_draw_cover),
        PageTemplate(id='content', frames=[content_frame], onPage=_content_footer),
    ])

    CW = doc.width
    bom_img_cache = {}  # shared across every bom_table() call below so a product repeated across sections/options is only fetched once
    S = {
        'h1': ParagraphStyle('h1', fontName='Helvetica-Bold', fontSize=18, textColor=TH['navy'], leading=22, spaceBefore=2, spaceAfter=2),
        'body': ParagraphStyle('body', fontName='Helvetica', fontSize=9, textColor=TH['txt'], leading=13),
        'small': ParagraphStyle('small', fontName='Helvetica', fontSize=8, textColor=TH['gray'], leading=11),
    }
    # Right-aligned variant of 'body', used for the Subtotal/VAT amount column in
    # the totals box so those figures line up with the right-aligned Total (AED)
    # column above them in the BOM table, instead of sitting flush-left in their cell.
    S['body_r'] = ParagraphStyle('body_r', parent=S['body'], alignment=TA_RIGHT)

    E = [NextPageTemplate('content'), Spacer(1, 1), PageBreak()]

    if kind in ('technical', 'combined'):
        _ov_start = len(E)
        E += [section_badge('OVERVIEW', TH)] + heading('Executive Summary', S, TH)
        E.append(Paragraph(content.get('executive_summary') or '', S['body']))
        E.append(Spacer(1, 4*mm))
        stats2 = content.get('stats') or []
        if stats2:
            E.append(stat_row(stats2, CW, TH)); E.append(Spacer(1, 5*mm))
        if content.get('feature_cards'):
            E.append(card_grid(content['feature_cards'], CW, TH=TH))

        E.append(PageBreak())
        if not _section_on(content, 'overview'): del E[_ov_start:]
        _sc_start = len(E)
        E += [section_badge('DELIVERY', TH)] + heading('Scope of Work', S, TH)
        if content.get('scope_cards'):
            E.append(card_grid(content['scope_cards'], CW, TH=TH))
        if not _section_on(content, 'scope'): del E[_sc_start:]
        if _section_on(content, 'quality') and content.get('control_testing'):
            E += [section_badge('QUALITY', TH)] + heading('Control, Testing & Documentation', S, TH)
            # Wraps 2-per-row instead of a fixed single row of exactly 2 --
            # larger-scope proposals ask the AI for 3-4 distinct control/
            # testing topics (see SCOPE_TARGETS), which a hardcoded 2-column
            # single row would have silently dropped past the 2nd item.
            ct_rows, ct_row = [], []
            for ct in content['control_testing']:
                bullets = '<br/>'.join(f"•  {esc_p(b)}" for b in (ct.get('bullets') or []))
                ct_row.append(Paragraph(f"<b>{esc_p(ct.get('title',''))}</b><br/>{bullets}", S['body']))
                if len(ct_row) == 2: ct_rows.append(ct_row); ct_row = []
            if ct_row:
                ct_row.append(Paragraph('', S['body']))
                ct_rows.append(ct_row)
            t = Table(ct_rows, colWidths=[CW/2, CW/2])
            t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('RIGHTPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
            E.append(t)

        if _section_on(content, 'system_design') and content.get('architecture_items'):
            E.append(PageBreak())
            E += [section_badge('SYSTEM DESIGN', TH)] + heading('Solution Architecture', S, TH)
            # No cap here (previously hardcoded to [:6]) -- the grid below
            # already wraps to as many rows of 3 as needed, so a larger-scope
            # proposal's extra architecture_items (see SCOPE_TARGETS) render
            # in full instead of being silently cut off past the 6th.
            items = content['architecture_items']
            S_al = ParagraphStyle('al', fontName='Helvetica-Bold', fontSize=8.5, textColor=TH['navy'])
            S_av = ParagraphStyle('av', fontName='Helvetica', fontSize=8, textColor=TH['txt'], leading=10)
            def _arch_cell(it):
                return [Paragraph(f"<b>{esc_p(it.get('label',''))}</b>", S_al), Spacer(1, 1.5*mm),
                        Paragraph(esc_p(it.get('value','')), S_av)]
            E.append(_fixed_col_grid(items, CW, 3, _arch_cell, TH=TH, colgap=3*mm, pad=8, box_w=0.5))
            if content.get('architecture_note'):
                E.append(Spacer(1, 2*mm)); E.append(Paragraph(content['architecture_note'], S['body']))

        # System Schematic: one or more Mermaid diagrams rendered to PNG in the
        # browser (see index.html's diagCodeToPng) and shipped inside content —
        # the server only places the image(s); it never generates them.
        # schematic_pngs is a list of {name, png} — one entry per room/section,
        # so a multi-room quote gets a separate schematic per room instead of
        # one diagram with every room's equipment interleaved together.
        # schematic_png (singular) is kept as a fallback for older saved drafts
        # that predate the per-room split.
        schem_pngs = content.get('schematic_pngs') or []
        if not schem_pngs and content.get('schematic_png'):
            schem_pngs = [{'name': '', 'png': content['schematic_png']}]
        if _section_on(content, 'schematic') and schem_pngs:
            for sp in schem_pngs:
                img_flow = _schematic_image(sp.get('png'), CW)
                if not img_flow:
                    continue
                E.append(PageBreak())
                title = 'System Schematic'
                if sp.get('name'):
                    title += f" — {sp['name']}"
                E += [section_badge('SYSTEM DESIGN', TH)] + heading(esc_p(title), S, TH)
                E.append(Paragraph('Signal flow of the proposed system. Solid lines carry AV signal, heavy lines carry audio/video over the network, dotted lines are control.', S['small']))
                E.append(Spacer(1, 3*mm))
                E.append(img_flow)

        E.append(PageBreak())
        E += [section_badge('BILL OF MATERIALS', TH)] + heading('Bill of Materials', S, TH)
        for o in opts:
            secs = o.get('sections') or []
            if len(opts) > 1 and o.get('label'):
                E.append(Paragraph(f"<b>{esc_p(o['label'])}</b>", ParagraphStyle('optl', fontName='Helvetica-Bold', fontSize=11, textColor=TH['navy'])))
                E.append(Spacer(1, 2*mm))
            E += bom_table(secs, CW, cur, with_price=(kind == 'combined'), pricing_type=pt, TH=TH, img_cache=bom_img_cache)

        if kind == 'combined':
            for o in opts:
                ts, vat_on, rate, vat, grand = calc_opt(o, pt)
                trows = [[Paragraph('Subtotal', S['body']), Paragraph(f"{cur} {fmt(ts)}", S['body_r'])],
                         [Paragraph(f"VAT {rate:g}%" if vat_on else "VAT", S['body']),
                          Paragraph(f"{cur} {fmt(vat)}" if vat_on else "Not applied", S['body_r'])],
                         [Paragraph('<b>Grand Total</b>', ParagraphStyle('gt', parent=S['body'], textColor=white, fontSize=10.5)),
                          Paragraph(f"<b>{cur} {fmt(grand)}</b>", ParagraphStyle('gt2', parent=S['body_r'], textColor=white, fontSize=10.5))]]
                tot = Table(trows, colWidths=[CW*0.72, CW*0.28])
                tot.setStyle(TableStyle([('BOX',(0,0),(-1,1),0.6,TH['grayb']),('LINEBELOW',(0,0),(-1,1),0.4,TH['grayb']),
                                         ('BACKGROUND',(0,2),(-1,2),TH['navy']),('TOPPADDING',(0,0),(-1,-1),6),
                                         ('BOTTOMPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),10)]))
                E.append(KeepTogether(tot)); E.append(Spacer(1, 5*mm))

        if _section_on(content, 'payment_terms') and content.get('payment_terms'):
            E += [section_badge('COMMERCIAL', TH)] + heading('Payment Terms', S, TH)
            for pt_bit in content['payment_terms']:
                E.append(Paragraph(f"•  {esc_p(pt_bit)}", S['body']))
            E.append(Spacer(1, 4*mm))

        if _section_on(content, 'rollout') and content.get('mobilization_phases'):
            E.append(PageBreak())
            E += [section_badge('ROLLOUT PLAN', TH)] + heading('Mobilization Plan', S, TH)
            E.append(numbered_phases(content['mobilization_phases'], CW, TH))

        E.append(PageBreak())
        _wr_start = len(E)
        E += [section_badge('ASSURANCE', TH)] + heading('Warranty, Support & General Notes', S, TH)
        left_bits = [Paragraph(f"<b>Warranty Coverage — {esc_p(content.get('warranty_years') or '1 Year')}</b>", ParagraphStyle('wc', fontName='Helvetica-Bold', fontSize=9.5, textColor=white))]
        left_bits.append(Spacer(1, 2*mm))
        for b in (content.get('support_bullets') or [])[:12]:
            left_bits.append(Paragraph(f"•  {esc_p(b)}", ParagraphStyle('wb', fontName='Helvetica', fontSize=8, textColor=white, leading=11)))
        left = Table([[left_bits]], colWidths=[CW*0.48])
        left.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),TH['navy']),('TOPPADDING',(0,0),(-1,-1),10),
                                  ('BOTTOMPADDING',(0,0),(-1,-1),10),('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10)]))
        right_rows = []
        colors_ex = [HexColor('#fdf3d8'), HexColor('#fdeceb'), HexColor('#e9eefb')]
        # No hardcoded cap here (previously [:3]) -- the boxes just stack
        # vertically in this single-column table, so a larger-scope
        # proposal's extra exclusions (see SCOPE_TARGETS) render in full.
        for i, ex in enumerate(content.get('exclusions') or []):
            box = Table([[Paragraph(f"<b>{esc_p(ex.get('title',''))}</b><br/>{esc_p(ex.get('text',''))}", ParagraphStyle('exb', fontName='Helvetica', fontSize=8, textColor=TH['txt'], leading=11))]], colWidths=[CW*0.48])
            box.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors_ex[i % 3]),('TOPPADDING',(0,0),(-1,-1),7),
                                     ('BOTTOMPADDING',(0,0),(-1,-1),7),('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),9)]))
            right_rows.append([box])
        two_col = Table([[left, Table(right_rows, colWidths=[CW*0.48]) if right_rows else Spacer(1,1)]], colWidths=[CW*0.5, CW*0.5])
        two_col.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(1,0),(1,0),8)]))
        E.append(two_col); E.append(Spacer(1, 5*mm))

        if content.get('general_notes'):
            E += [section_badge('FINE PRINT', TH)] + heading('General Notes', S, TH)
            for n in content['general_notes']:
                E.append(Paragraph(f"•  {esc_p(n)}", S['body']))
            E.append(Spacer(1, 4*mm))

        footer_bits = [company.get('name',''), company.get('address',''), company.get('trn_tel','')]
        footer_bits = [b for b in footer_bits if b]
        if footer_bits:
            E.append(Paragraph(' · '.join(footer_bits), S['small']))

        # Credentials/certifications line -- moved here from right under the
        # Executive Summary (top of the document) to a closing credibility
        # statement at the end, just before the Acceptance page.
        cred_bits = []
        if company.get('founded_year'): cred_bits.append(f"Founded {company['founded_year']}")
        if company.get('certifications'): cred_bits.append(company['certifications'])
        if company.get('notable_clients'): cred_bits.append(f"Trusted by {company['notable_clients']}")
        if cred_bits:
            E.append(Spacer(1, 2*mm))
            E.append(Paragraph(' &nbsp;|&nbsp; '.join(esc_p(b) for b in cred_bits), ParagraphStyle('cred', fontName='Helvetica-Oblique', fontSize=8, textColor=TH['gray'])))

    if not _section_on(content, 'warranty'): del E[_wr_start:]

    if kind in ('commercial', 'combined') and kind != 'combined':
        # standalone commercial-only document: pricing table + totals (BOM already
        # rendered above when kind=='combined', so this branch only fires for
        # kind=='commercial')
        E += [section_badge('COMMERCIAL', TH)] + heading('Commercial Proposal', S, TH)
        for o in opts:
            secs = o.get('sections') or []
            if len(opts) > 1 and o.get('label'):
                E.append(Paragraph(f"<b>{esc_p(o['label'])}</b>", ParagraphStyle('optl2', fontName='Helvetica-Bold', fontSize=11, textColor=TH['navy'])))
                E.append(Spacer(1, 2*mm))
            E += bom_table(secs, CW, cur, with_price=True, pricing_type=pt, TH=TH, img_cache=bom_img_cache)
            ts, vat_on, rate, vat, grand = calc_opt(o, pt)
            trows = [[Paragraph('Subtotal', S['body']), Paragraph(f"{cur} {fmt(ts)}", S['body_r'])],
                     [Paragraph(f"VAT {rate:g}%" if vat_on else "VAT", S['body']),
                      Paragraph(f"{cur} {fmt(vat)}" if vat_on else "Not applied", S['body_r'])],
                     [Paragraph('<b>Grand Total</b>', ParagraphStyle('gt3', parent=S['body'], textColor=white, fontSize=10.5)),
                      Paragraph(f"<b>{cur} {fmt(grand)}</b>", ParagraphStyle('gt4', parent=S['body_r'], textColor=white, fontSize=10.5))]]
            tot = Table(trows, colWidths=[CW*0.72, CW*0.28])
            tot.setStyle(TableStyle([('BOX',(0,0),(-1,1),0.6,TH['grayb']),('LINEBELOW',(0,0),(-1,1),0.4,TH['grayb']),
                                     ('BACKGROUND',(0,2),(-1,2),TH['navy']),('TOPPADDING',(0,0),(-1,-1),6),
                                     ('BOTTOMPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),10)]))
            E.append(KeepTogether(tot)); E.append(Spacer(1, 6*mm))
        E.append(Paragraph('Pricing valid for 30 days from the date of this proposal, subject to the Terms & Conditions of the associated quotation.', S['small']))

    # Signature & Acceptance -- appended to every generated document
    # (technical, commercial, combined) regardless of which narrative
    # sections were toggled off above, so there's always somewhere for the
    # client to formally accept against or reference a PO number.
    def _sig_column(title, sub_lines=None):
        rows = [[Paragraph(f"<b>{esc_p(title)}</b>", ParagraphStyle('sigh', fontName='Helvetica-Bold', fontSize=10, textColor=TH['navy']))]]
        if sub_lines:
            rows.append([Paragraph('<br/>'.join(esc_p(s) for s in sub_lines), ParagraphStyle('sigsub', fontName='Helvetica', fontSize=8, textColor=TH['gray'], leading=11))])
        rows.append([Spacer(1, 4*mm)])
        line_rows = []  # row indices (within this column's table) that get an underline
        for label in ('Name', 'Signature', 'Date'):
            rows.append([Paragraph(label, ParagraphStyle('sigl', fontName='Helvetica', fontSize=8.5, textColor=TH['gray']))])
            rows.append([Spacer(1, 8*mm)])
            line_rows.append(len(rows) - 1)
        t = Table(rows, colWidths=[CW*0.46])
        style = [('TOPPADDING',(0,0),(-1,-1),0), ('BOTTOMPADDING',(0,0),(-1,-1),1), ('LEFTPADDING',(0,0),(-1,-1),0)]
        for r in line_rows:
            style.append(('LINEBELOW', (0,r), (0,r), 0.6, TH['grayb']))
        t.setStyle(TableStyle(style))
        return t

    E.append(PageBreak())
    E += [section_badge('ACCEPTANCE', TH)] + heading('Authorization & Sign-Off', S, TH)
    E.append(Paragraph('This proposal is accepted subject to the terms, pricing, and exclusions outlined above. Please sign and return, or issue a Purchase Order referencing this document, to proceed.', S['body']))
    E.append(Spacer(1, 6*mm))
    left_bits = [b for b in [content.get('prepared_by'), content.get('prepared_by_email'), content.get('prepared_by_phone')] if b]
    right_bits = [b for b in [content.get('client_contact_email'), content.get('client_contact_phone')] if b]
    right_bits.append('Purchase Order / Reference No.: _______________')
    left_sig = _sig_column(company.get('name') or 'Sysconic Technologies LLC', sub_lines=left_bits)
    right_sig = _sig_column(content.get('client_contact_name') or content.get('customer_name') or 'Client', sub_lines=right_bits)
    sig_row = Table([[left_sig, right_sig]], colWidths=[CW*0.5, CW*0.5])
    sig_row.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'), ('LEFTPADDING',(1,0),(1,0),8)]))
    E.append(KeepTogether(sig_row))

    doc.build(E)
    return buf.getvalue()

# ── Routes ────────────────────────────────────────────────────────────────────
# Two-step flow: draft the AI content first (no PDF yet) so it can be reviewed
# and edited in the UI, then render whichever document(s) are confirmed —
# Technical, Commercial, and Combined can each be generated independently.

def _load_quote(claims, quote_id):
    row = sb.table('quotes').select('*').eq('id', quote_id).eq('company_id', claims['company_id']).execute()
    if not row.data: return None
    quote = row.data[0]
    for f in ('quote_data', 'terms_data', 'vendor_data'):
        if isinstance(quote.get(f), str):
            try: quote[f] = json.loads(quote[f])
            except: quote[f] = []
    return quote

def _load_company(claims):
    co_row = sb.table('companies').select('name,legal_name,address,trn,phone,website,logo_url,certifications,founded_year,notable_clients').eq('id', claims['company_id']).execute()
    co_raw = co_row.data[0] if co_row.data else {}
    company = build_company_dict(co_raw, co_raw.get('name'))
    logo_bytes = _fetch_bytes(co_raw.get('logo_url'))
    return company, logo_bytes

@app.route('/api/proposal/draft', methods=['POST'])
def draft_proposal():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    d = request.json or {}
    quote_id = d.get('quote_id')
    brief = (d.get('brief') or '').strip()
    which = str(d.get('which') or 'all')
    attachment = d.get('attachment') or None  # {filename, data (base64)}

    if not quote_id:
        return jsonify({'error': 'No quote selected'}), 400
    if not brief and not attachment:
        return jsonify({'error': 'Describe the project or attach a reference document'}), 400

    quote = _load_quote(claims, quote_id)
    if not quote: return jsonify({'error': 'Quote not found'}), 404

    attachment_text = ''
    if attachment and attachment.get('data'):
        try:
            raw = attachment['data']
            if ',' in raw: raw = raw.split(',', 1)[1]
            import base64
            file_bytes = base64.b64decode(raw)
            if len(file_bytes) > MAX_ATTACHMENT_BYTES:
                return jsonify({'error': 'Attachment is larger than 8 MB. Please use a smaller file.'}), 400
            attachment_text = extract_attachment_text(attachment.get('filename'), file_bytes)
        except Exception:
            attachment_text = ''

    equipment_summary, opts = get_equipment_summary(quote, which)
    cur = quote.get('currency') or 'AED'

    try:
        requested_model = d.get('model')
        model = requested_model if requested_model in ALLOWED_GEMINI_MODELS else None
        content = draft_proposal_content(brief, attachment_text, equipment_summary, cur, model)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 502

    content.setdefault('title', quote.get('title') or 'Technical Proposal')
    content.setdefault('payment_terms', [
        '50% advance payment on order confirmation',
        '40% on delivery of equipment to site',
        '10% on completion, testing and handover',
    ])
    content['customer_name'] = quote.get('customer') or ''
    content['date'] = (opts[0].get('date') if opts else '') or time.strftime('%d %B %Y')
    content['prepared_by'] = (opts[0].get('by') if opts else '') or ''

    # Best-effort client contact lookup -- quotes only store the customer's
    # name as free text (no customer_id FK on this table), so this is a
    # case-insensitive exact-name match against the Customer Master. Left
    # blank on no match or an ambiguous multi-match rather than guessing --
    # every one of these fields is editable in the proposal customizer
    # before the PDF is actually generated.
    content.setdefault('client_contact_name', '')
    content.setdefault('client_contact_email', '')
    content.setdefault('client_contact_phone', '')
    cust_name = (quote.get('customer') or '').strip()
    if cust_name:
        try:
            cm = sb.table('customers').select('name,email,phone') \
                .eq('company_id', claims['company_id']).ilike('name', cust_name).execute()
            if cm.data and len(cm.data) == 1:
                content['client_contact_email'] = cm.data[0].get('email') or ''
                content['client_contact_phone'] = cm.data[0].get('phone') or ''
        except Exception:
            pass

    # Preparer contact -- pulled from the logged-in user's account and the
    # company profile so it doesn't have to be typed in on every proposal.
    content.setdefault('prepared_by_email', '')
    content.setdefault('prepared_by_phone', '')
    try:
        ur = sb.table('users').select('email').eq('id', claims['user_id']).execute()
        if ur.data: content['prepared_by_email'] = ur.data[0].get('email') or ''
    except Exception:
        pass
    try:
        cor = sb.table('companies').select('phone').eq('id', claims['company_id']).execute()
        if cor.data: content['prepared_by_phone'] = cor.data[0].get('phone') or ''
    except Exception:
        pass

    return jsonify({'content': content})

@app.route('/api/proposal/render', methods=['POST'])
def render_proposal():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    d = request.json or {}
    quote_id = d.get('quote_id')
    which = str(d.get('which') or 'all')
    kind = d.get('kind') or 'combined'
    content = d.get('content') or {}

    if not quote_id:
        return jsonify({'error': 'No quote selected'}), 400
    if kind not in ('technical', 'commercial', 'combined'):
        return jsonify({'error': 'Invalid document type'}), 400
    if not content.get('title'):
        return jsonify({'error': 'Missing proposal content — please draft it first'}), 400

    quote = _load_quote(claims, quote_id)
    if not quote: return jsonify({'error': 'Quote not found'}), 404
    company, logo_bytes = _load_company(claims)

    # Pricing is always pulled fresh from the live quote, never from the AI draft.
    _, opts = get_equipment_summary(quote, which)
    cur = quote.get('currency') or 'AED'

    labels = {'technical': 'TECHNICAL PROPOSAL', 'commercial': 'COMMERCIAL PROPOSAL', 'combined': 'TECHNICAL & COMMERCIAL PROPOSAL'}
    label_map = {'technical': 'Technical-Proposal', 'commercial': 'Commercial-Proposal', 'combined': 'Proposal'}

    try:
        pdf_bytes = build_proposal_pdf(kind, content, quote, opts, company, logo_bytes, cur, labels[kind])
    except Exception as e:
        return jsonify({'error': f'Proposal generation failed while building the PDF: {str(e)[:250]}'}), 500

    ts = int(time.time())
    safe_title = ''.join(c if c.isalnum() or c in ' -_' else '' for c in (content.get('title') or 'Proposal')).strip().replace(' ', '-')[:60]
    path = f"{claims['company_id']}/{safe_title}-{label_map[kind]}-{ts}.pdf"
    try:
        sb.storage.from_(BUCKET).upload(path, pdf_bytes, {'content-type': 'application/pdf'})
        url = _signed_url(BUCKET, path)
    except Exception as e:
        return jsonify({'error': f'Could not save the generated proposal: {str(e)[:250]}'}), 502

    return jsonify({'file': {'name': f"{label_map[kind]}.pdf", 'url': url}})

if __name__ == '__main__':
    app.run(debug=True)
