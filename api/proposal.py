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

def calc_item(it):
    cad = _num(it.get('cost')) * (1 - _num(it.get('disc'))/100.0) * (1 - _num(it.get('discAdd'))/100.0)
    m = _num(it.get('margin'))
    up = round(cad * (1 + m))
    qty = _num(it.get('qty'))
    return up, up * qty

def calc_opt(o):
    ts = 0.0
    for s in (o.get('sections') or []):
        for it in (s.get('items') or []):
            ts += calc_item(it)[1]
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
SYSTEM_PROMPT = """You are a senior AV/IT solutions consultant drafting the TEXT CONTENT for a professional technical proposal document (not the pricing — that's handled separately). Given a project brief, optional reference material, and a summary of the actual equipment being proposed (brands/models/categories from a real bill of materials), draft compelling, specific, professional proposal content.

Return ONLY valid JSON matching exactly this shape (no markdown fences, no commentary):
{
  "title": "Short punchy solution title, e.g. 'Signage & Interactive Flat Panel (IFP) Solution'",
  "subtitle": "One descriptive line about what the system does and where",
  "stats": [{"label":"SHORT LABEL","value":"short value e.g. 3 or 20+"} ...exactly 4 items, derived from the real equipment summary provided...],
  "executive_summary": "2-4 sentence professional paragraph summarizing the solution",
  "feature_cards": [{"title":"...", "description":"1-2 sentences"} ...exactly 4 items describing the key components/subsystems...],
  "scope_cards": [{"title":"...", "description":"1-2 sentences"} ...exactly 4 items describing scope of work streams (supply, installation, integration, testing etc.)...],
  "control_testing": [{"title":"Control & Automation","bullets":["...","..."]}, {"title":"Testing & Commissioning","bullets":["...","..."]}],
  "architecture_items": [{"label":"Category e.g. Display","value":"brands/models e.g. Samsung QM43C"} ...exactly 6 items...],
  "architecture_note": "1-2 sentence paragraph about how the layers fit together",
  "mobilization_phases": [{"title":"Project Initiation","bullets":["...","...","..."]} ...exactly 4 phases, standard AV project rollout phases...],
  "warranty_years": "e.g. 1 Year or 3 Years, infer from context or default to 1 Year",
  "support_bullets": ["...", "...", "... 4-6 short bullet points of what's covered during warranty"],
  "exclusions": [{"title":"Mishandling / misuse","text":"..."}, {"title":"Environmental","text":"..."}, {"title":"Third-party scope","text":"..."}],
  "general_notes": ["...", "... 4-6 short standard proposal disclaimer bullet points"]
}

Ground every claim in the real equipment summary provided — never invent brands/models that aren't in it. Keep tone professional and concise, matching enterprise AV/IT proposal writing."""

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

    user_msg = json.dumps({
        'project_brief': brief[:4000],
        'reference_material': (attachment_text or '')[:6000],
        'equipment_summary': equipment_summary,
        'currency': currency,
    }, ensure_ascii=False)

    body = json.dumps({
        'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
        'contents': [{'role': 'user', 'parts': [{'text': user_msg}]}],
        'generationConfig': {
            'temperature': 0.4,
            'maxOutputTokens': 8000,
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
        return json.loads(text)
    except Exception:
        raise RuntimeError('Could not parse the AI response. Please try again.')

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
def section_badge(text):
    p = Paragraph(f'<font color="#16294f" size="7.5"><b>{text.upper()}</b></font>',
                  ParagraphStyle('badge', fontName='Helvetica-Bold', fontSize=7.5))
    t = Table([[p]], colWidths=[None])
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),GOLD),('TOPPADDING',(0,0),(-1,-1),4),
                           ('BOTTOMPADDING',(0,0),(-1,-1),4),('LEFTPADDING',(0,0),(-1,-1),8),
                           ('RIGHTPADDING',(0,0),(-1,-1),8)]))
    return t

def heading(text, S):
    return [Spacer(1, 2*mm), Paragraph(text, S['h1']),
            Table([['']], colWidths=[22*mm], rowHeights=[1.4],
                  style=TableStyle([('BACKGROUND',(0,0),(-1,-1),GOLD)])),
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

def stat_row(stats, CW):
    n = max(1, len(stats))
    gap = 3*mm
    cw = (CW - gap*(n-1)) / n
    cells = []
    for s in stats:
        cells.append([Paragraph(f"<font color='#f4b400' size='16'><b>{esc_p(s.get('value',''))}</b></font>", ParagraphStyle('sv', alignment=TA_CENTER, leading=19)),
                      Paragraph(f"<font color='#c7d3ea' size='6.5'><b>{esc_p(str(s.get('label','')).upper())}</b></font>", ParagraphStyle('sl', alignment=TA_CENTER, leading=8))])
    row = [Table([c], colWidths=[cw]) for c in [ [x] for x in cells ]]
    # Build as a single table with n columns, each cell a mini stacked Table
    inner_cells = []
    for s in stats:
        vsize = _stat_val_size(str(s.get('value','')))
        cell_tbl = Table([[Paragraph(f"<font color='#f4b400' size='{vsize}'><b>{esc_p(str(s.get('value','')))}</b></font>", ParagraphStyle('sv2', alignment=TA_CENTER, leading=vsize+3))],
                           [Paragraph(f"<font color='#c7d3ea' size='6.5'><b>{esc_p(str(s.get('label','')).upper())}</b></font>", ParagraphStyle('sl2', alignment=TA_CENTER, leading=8))]],
                          colWidths=[cw])
        cell_tbl.setStyle(TableStyle([('ALIGN',(0,0),(-1,-1),'CENTER'),('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2)]))
        inner_cells.append(cell_tbl)
    outer = Table([inner_cells], colWidths=[cw]*n, spaceAfter=0)
    outer.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),NAVY),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
                               ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),
                               ('LINEAFTER',(0,0),(-2,0),0.5,HexColor('#2c4270'))]))
    return outer

def esc_p(s):
    return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

def card_grid(cards, CW, accent=GOLD):
    """2-column grid of feature/scope cards, each with a small accent block + title + description."""
    S_title = ParagraphStyle('ct', fontName='Helvetica-Bold', fontSize=9.5, textColor=NAVY, leading=12)
    S_desc  = ParagraphStyle('cd', fontName='Helvetica', fontSize=8, textColor=TXT, leading=11)
    cells, row = [], []
    gap = 4*mm
    cw = (CW - gap) / 2
    for i, c in enumerate(cards):
        inner = Table([
            [Table([['']], colWidths=[6*mm], rowHeights=[6*mm], style=TableStyle([('BACKGROUND',(0,0),(-1,-1),accent)]))],
            [Paragraph(esc_p(c.get('title','')), S_title)],
            [Paragraph(esc_p(c.get('description','')), S_desc)],
        ], colWidths=[cw-16])
        inner.setStyle(TableStyle([('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(0,0),4),
                                   ('BOTTOMPADDING',(1,0),(-1,-1),3),('LEFTPADDING',(0,0),(-1,-1),0)]))
        card = Table([[inner]], colWidths=[cw])
        card.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),CARDBG),('BOX',(0,0),(-1,-1),0.6,GRAYB),
                                  ('TOPPADDING',(0,0),(-1,-1),10),('BOTTOMPADDING',(0,0),(-1,-1),10),
                                  ('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10)]))
        row.append(card)
        if len(row) == 2:
            cells.append(row); row = []
    if row:
        row.append(Spacer(1,1))
        cells.append(row)
    t = Table(cells, colWidths=[cw, gap, cw][:2] if False else [cw, cw], spaceBefore=0)
    style = [('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),
             ('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),4*mm),('VALIGN',(0,0),(-1,-1),'TOP')]
    t.setStyle(TableStyle(style))
    return t

def numbered_phases(phases, CW):
    S_title = ParagraphStyle('pt', fontName='Helvetica-Bold', fontSize=9.5, textColor=NAVY, leading=12)
    S_bul   = ParagraphStyle('pb', fontName='Helvetica', fontSize=8, textColor=TXT, leading=11)
    rows = []
    for i, ph in enumerate(phases):
        num = Table([[Paragraph(f"<font color='white' size='11'><b>{i+1}</b></font>", ParagraphStyle('num', alignment=TA_CENTER))]],
                    colWidths=[9*mm], rowHeights=[9*mm])
        num.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),NAVY),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,-1),'CENTER')]))
        bullets = '<br/>'.join(f"•  {esc_p(b)}" for b in (ph.get('bullets') or []))
        body = [Paragraph(esc_p(ph.get('title','')), S_title), Paragraph(bullets, S_bul)]
        rows.append([num, body])
    t = Table(rows, colWidths=[12*mm, CW-12*mm])
    t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),4),
                          ('BOTTOMPADDING',(0,0),(-1,-1),8),('LEFTPADDING',(0,0),(-1,-1),0)]))
    return t

def bom_table(sections, CW, cur, with_price):
    S_th = ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=7.8, textColor=white)
    S_b  = ParagraphStyle('b', fontName='Helvetica', fontSize=8, textColor=TXT, leading=10)
    S_bc = ParagraphStyle('bc', fontName='Helvetica', fontSize=8, textColor=TXT, alignment=TA_CENTER)
    S_br = ParagraphStyle('br', fontName='Helvetica', fontSize=8, textColor=TXT, alignment=TA_RIGHT)
    flow = []
    for s in sections:
        if s.get('name'):
            flow.append(Paragraph(f"<b>{esc_p(s['name'])}</b>", ParagraphStyle('sn', fontName='Helvetica-Bold', fontSize=10, textColor=NAVY)))
            flow.append(Spacer(1, 1.5*mm))
        if with_price:
            hdr = ['#','Brand','Model','Description', f'Unit Price ({cur})', 'Qty', 'UOM', f'Total ({cur})']
            widths = [CW*0.04, CW*0.10, CW*0.13, CW*0.32, CW*0.13, CW*0.06, CW*0.06, CW*0.16]
        else:
            hdr = ['#','Brand','Model','Description','Qty','UOM']
            widths = [CW*0.05, CW*0.15, CW*0.19, CW*0.44, CW*0.08, CW*0.09]
        rows = [[Paragraph(h, S_th) for h in hdr]]
        rn = 1
        for it in (s.get('items') or []):
            up, tp = calc_item(it)
            if with_price:
                rows.append([Paragraph(str(rn), S_bc), Paragraph(esc_p(it.get('brand') or ''), S_b),
                             Paragraph(esc_p(it.get('model') or ''), S_b), Paragraph(esc_p(it.get('desc') or ''), S_b),
                             Paragraph(fmt(up), S_br), Paragraph(fmt(_num(it.get('qty'))), S_bc),
                             Paragraph(esc_p(it.get('uom') or 'Pcs'), S_bc), Paragraph(fmt(tp), S_br)])
            else:
                rows.append([Paragraph(str(rn), S_bc), Paragraph(esc_p(it.get('brand') or ''), S_b),
                             Paragraph(esc_p(it.get('model') or ''), S_b), Paragraph(esc_p(it.get('desc') or ''), S_b),
                             Paragraph(fmt(_num(it.get('qty'))), S_bc), Paragraph(esc_p(it.get('uom') or 'Pcs'), S_bc)])
            rn += 1
        tbl = Table(rows, colWidths=widths, repeatRows=1)
        style = [('BACKGROUND',(0,0),(-1,0),NAVY),('VALIGN',(0,0),(-1,-1),'TOP'),
                 ('LINEBELOW',(0,1),(-1,-1),0.4,GRAYB),('TOPPADDING',(0,0),(-1,-1),4.5),
                 ('BOTTOMPADDING',(0,0),(-1,-1),4.5),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5)]
        for ri in range(1, len(rows)):
            if (ri-1) % 2 == 1:
                style.append(('BACKGROUND',(0,ri),(-1,ri),ZEBRA))
        tbl.setStyle(TableStyle(style))
        flow.append(tbl); flow.append(Spacer(1, 5*mm))
    return flow

# ── Cover page (drawn directly on the canvas — content is fixed, not flowing) ───
def _draw_cover(canvas, doc):
    data = doc.proposal_data; company = doc.proposal_company; logo_bytes = doc.proposal_logo
    w, h = A4
    canvas.saveState()
    canvas.setFillColor(NAVY); canvas.rect(0, 0, w, h, fill=1, stroke=0)
    canvas.setFillAlpha(0.10); canvas.setFillColor(white)
    canvas.circle(w-10*mm, h-30*mm, 95*mm, fill=1, stroke=0)
    canvas.circle(-10*mm, 25*mm, 70*mm, fill=1, stroke=0)
    canvas.restoreState()

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

    badge = (data.get('doc_label') or 'TECHNICAL PROPOSAL').upper()
    canvas.setFont('Helvetica-Bold', 9)
    bw = canvas.stringWidth(badge, 'Helvetica-Bold', 9) + 16
    by = h - 96*mm
    canvas.setFillColor(GOLD); canvas.roundRect(15*mm, by, bw, 8*mm, 4*mm, fill=1, stroke=0)
    canvas.setFillColor(NAVY); canvas.drawString(15*mm+8, by+2.6*mm, badge)

    canvas.setFillColor(white); canvas.setFont('Helvetica-Bold', 25)
    ty = h - 118*mm
    for ln in textwrap.wrap(data.get('title') or 'Technical Proposal', width=26)[:3]:
        canvas.drawString(15*mm, ty, ln); ty -= 10.5*mm

    canvas.setFont('Helvetica', 10.5); canvas.setFillColor(LIGHTBLU)
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
            canvas.setFillColor(GOLD); canvas.setFont('Helvetica-Bold', 16)
            canvas.drawCentredString(cx+cw/2, cy+14*mm, str(s.get('value',''))[:8])
            canvas.setFillColor(LIGHTBLU); canvas.setFont('Helvetica-Bold', 6.3)
            canvas.drawCentredString(cx+cw/2, cy+6.5*mm, str(s.get('label',''))[:22].upper())
            cx += cw + gap

    canvas.setStrokeColor(HexColor('#3a5080')); canvas.setLineWidth(0.6)
    canvas.line(15*mm, 30*mm, w-15*mm, 30*mm)
    canvas.setFont('Helvetica', 8); canvas.setFillColor(LIGHTBLU)
    canvas.drawString(15*mm, 24*mm, 'Prepared for')
    canvas.setFont('Helvetica-Bold', 10.5); canvas.setFillColor(white)
    canvas.drawString(15*mm, 18.5*mm, (data.get('customer_name') or '')[:60])
    canvas.setFont('Helvetica', 8); canvas.setFillColor(LIGHTBLU)
    canvas.drawRightString(w-15*mm, 24*mm, f"Date: {data.get('date') or ''}")
    canvas.setFont('Helvetica-Bold', 9); canvas.setFillColor(white)
    canvas.drawRightString(w-15*mm, 18.5*mm, f"Prepared by: {(data.get('prepared_by') or company.get('name') or '')[:50]}")

def _content_footer(canvas, doc):
    company = doc.proposal_company
    w, h = A4
    canvas.saveState()
    canvas.setStrokeColor(GOLD); canvas.setLineWidth(1.1)
    canvas.line(15*mm, 14*mm, w-15*mm, 14*mm)
    canvas.setFont('Helvetica', 6.8); canvas.setFillColor(GRAY)
    label = doc.proposal_data.get('doc_label') or 'TECHNICAL PROPOSAL'
    canvas.drawString(15*mm, 10*mm, f"{label} · {company.get('name','')}"[:110])
    canvas.setFont('Helvetica', 6.8); canvas.setFillColor(GRAY)
    canvas.drawCentredString(w/2, 6*mm, f"Page {doc.page}")
    if company.get('web'):
        canvas.setFont('Helvetica-Bold', 6.8); canvas.setFillColor(NAVY2)
        canvas.drawRightString(w-15*mm, 10*mm, company['web'])
    canvas.restoreState()

# ── Full document builder ────────────────────────────────────────────────────────
def build_proposal_pdf(kind, content, quote, opts, company, logo_bytes, cur, doc_label):
    """kind: 'technical' | 'commercial' | 'combined'"""
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
    S = {
        'h1': ParagraphStyle('h1', fontName='Helvetica-Bold', fontSize=18, textColor=NAVY, leading=22, spaceBefore=2, spaceAfter=2),
        'body': ParagraphStyle('body', fontName='Helvetica', fontSize=9, textColor=TXT, leading=13),
        'small': ParagraphStyle('small', fontName='Helvetica', fontSize=8, textColor=GRAY, leading=11),
    }

    E = [NextPageTemplate('content'), Spacer(1, 1), PageBreak()]

    if kind in ('technical', 'combined'):
        E += [section_badge('OVERVIEW')] + heading('Executive Summary', S)
        E.append(Paragraph(content.get('executive_summary') or '', S['body']))
        E.append(Spacer(1, 4*mm))
        stats2 = content.get('stats') or []
        if stats2:
            E.append(stat_row(stats2, CW)); E.append(Spacer(1, 5*mm))
        if content.get('feature_cards'):
            E.append(card_grid(content['feature_cards'], CW))

        E.append(PageBreak())
        E += [section_badge('DELIVERY')] + heading('Scope of Work', S)
        if content.get('scope_cards'):
            E.append(card_grid(content['scope_cards'], CW))
        if content.get('control_testing'):
            E += [section_badge('QUALITY')] + heading('Control, Testing & Documentation', S)
            cells = []
            for ct in content['control_testing'][:2]:
                bullets = '<br/>'.join(f"•  {esc_p(b)}" for b in (ct.get('bullets') or []))
                cells.append(Paragraph(f"<b>{esc_p(ct.get('title',''))}</b><br/>{bullets}", S['body']))
            while len(cells) < 2: cells.append(Paragraph('', S['body']))
            t = Table([cells], colWidths=[CW/2, CW/2])
            t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('RIGHTPADDING',(0,0),(0,0),8)]))
            E.append(t)

        if content.get('architecture_items'):
            E.append(PageBreak())
            E += [section_badge('SYSTEM DESIGN')] + heading('Solution Architecture', S)
            items = content['architecture_items'][:6]
            rows, row = [], []
            for it in items:
                cell = Table([[Paragraph(f"<b>{esc_p(it.get('label',''))}</b>", ParagraphStyle('al', fontName='Helvetica-Bold', fontSize=8.5, textColor=NAVY))],
                              [Paragraph(esc_p(it.get('value','')), ParagraphStyle('av', fontName='Helvetica', fontSize=8, textColor=TXT, leading=10))]],
                             colWidths=[(CW-6*mm)/3])
                cell.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),CARDBG),('BOX',(0,0),(-1,-1),0.5,GRAYB),
                                          ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),
                                          ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8)]))
                row.append(cell)
                if len(row) == 3: rows.append(row); row = []
            if row:
                while len(row) < 3: row.append(Spacer(1,1))
                rows.append(row)
            grid = Table(rows, colWidths=[(CW-6*mm)/3]*3, spaceBefore=2)
            grid.setStyle(TableStyle([('LEFTPADDING',(0,0),(-1,-1),0),('TOPPADDING',(0,0),(-1,-1),0),
                                      ('BOTTOMPADDING',(0,0),(-1,-1),3*mm),('VALIGN',(0,0),(-1,-1),'TOP')]))
            E.append(grid)
            if content.get('architecture_note'):
                E.append(Spacer(1, 2*mm)); E.append(Paragraph(content['architecture_note'], S['body']))

        E.append(PageBreak())
        E += [section_badge('BILL OF MATERIALS')] + heading('Bill of Materials', S)
        for o in opts:
            secs = o.get('sections') or []
            if len(opts) > 1 and o.get('label'):
                E.append(Paragraph(f"<b>{esc_p(o['label'])}</b>", ParagraphStyle('optl', fontName='Helvetica-Bold', fontSize=11, textColor=NAVY)))
                E.append(Spacer(1, 2*mm))
            E += bom_table(secs, CW, cur, with_price=(kind == 'combined'))

        if kind == 'combined':
            for o in opts:
                ts, vat_on, rate, vat, grand = calc_opt(o)
                trows = [[Paragraph('Subtotal', S['body']), Paragraph(f"{cur} {fmt(ts)}", S['body'])],
                         [Paragraph(f"VAT {rate:g}%" if vat_on else "VAT", S['body']),
                          Paragraph(f"{cur} {fmt(vat)}" if vat_on else "Not applied", S['body'])],
                         [Paragraph('<b>Grand Total</b>', ParagraphStyle('gt', parent=S['body'], textColor=white, fontSize=10.5)),
                          Paragraph(f"<b>{cur} {fmt(grand)}</b>", ParagraphStyle('gt2', parent=S['body'], textColor=white, fontSize=10.5))]]
                tot = Table(trows, colWidths=[CW*0.72, CW*0.28])
                tot.setStyle(TableStyle([('BOX',(0,0),(-1,1),0.6,GRAYB),('LINEBELOW',(0,0),(-1,1),0.4,GRAYB),
                                         ('BACKGROUND',(0,2),(-1,2),NAVY),('TOPPADDING',(0,0),(-1,-1),6),
                                         ('BOTTOMPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),10)]))
                E.append(KeepTogether(tot)); E.append(Spacer(1, 5*mm))

        if content.get('mobilization_phases'):
            E.append(PageBreak())
            E += [section_badge('ROLLOUT PLAN')] + heading('Mobilization Plan', S)
            E.append(numbered_phases(content['mobilization_phases'], CW))

        E.append(PageBreak())
        E += [section_badge('ASSURANCE')] + heading('Warranty, Support & General Notes', S)
        left_bits = [Paragraph(f"<b>Warranty Coverage — {esc_p(content.get('warranty_years') or '1 Year')}</b>", ParagraphStyle('wc', fontName='Helvetica-Bold', fontSize=9.5, textColor=white))]
        left_bits.append(Spacer(1, 2*mm))
        for b in (content.get('support_bullets') or [])[:8]:
            left_bits.append(Paragraph(f"•  {esc_p(b)}", ParagraphStyle('wb', fontName='Helvetica', fontSize=8, textColor=white, leading=11)))
        left = Table([[left_bits]], colWidths=[CW*0.48])
        left.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),NAVY),('TOPPADDING',(0,0),(-1,-1),10),
                                  ('BOTTOMPADDING',(0,0),(-1,-1),10),('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10)]))
        right_rows = []
        colors_ex = [HexColor('#fdf3d8'), HexColor('#fdeceb'), HexColor('#e9eefb')]
        for i, ex in enumerate((content.get('exclusions') or [])[:3]):
            box = Table([[Paragraph(f"<b>{esc_p(ex.get('title',''))}</b><br/>{esc_p(ex.get('text',''))}", ParagraphStyle('exb', fontName='Helvetica', fontSize=8, textColor=TXT, leading=11))]], colWidths=[CW*0.48])
            box.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors_ex[i % 3]),('TOPPADDING',(0,0),(-1,-1),7),
                                     ('BOTTOMPADDING',(0,0),(-1,-1),7),('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),9)]))
            right_rows.append([box])
        two_col = Table([[left, Table(right_rows, colWidths=[CW*0.48]) if right_rows else Spacer(1,1)]], colWidths=[CW*0.5, CW*0.5])
        two_col.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(1,0),(1,0),8)]))
        E.append(two_col); E.append(Spacer(1, 5*mm))

        if content.get('general_notes'):
            E += [section_badge('FINE PRINT')] + heading('General Notes', S)
            for n in content['general_notes']:
                E.append(Paragraph(f"•  {esc_p(n)}", S['body']))
            E.append(Spacer(1, 4*mm))

        footer_bits = [company.get('name',''), company.get('address',''), company.get('trn_tel','')]
        footer_bits = [b for b in footer_bits if b]
        if footer_bits:
            E.append(Paragraph(' · '.join(footer_bits), S['small']))

    if kind in ('commercial', 'combined') and kind != 'combined':
        # standalone commercial-only document: pricing table + totals (BOM already
        # rendered above when kind=='combined', so this branch only fires for
        # kind=='commercial')
        E += [section_badge('COMMERCIAL')] + heading('Commercial Proposal', S)
        for o in opts:
            secs = o.get('sections') or []
            if len(opts) > 1 and o.get('label'):
                E.append(Paragraph(f"<b>{esc_p(o['label'])}</b>", ParagraphStyle('optl2', fontName='Helvetica-Bold', fontSize=11, textColor=NAVY)))
                E.append(Spacer(1, 2*mm))
            E += bom_table(secs, CW, cur, with_price=True)
            ts, vat_on, rate, vat, grand = calc_opt(o)
            trows = [[Paragraph('Subtotal', S['body']), Paragraph(f"{cur} {fmt(ts)}", S['body'])],
                     [Paragraph(f"VAT {rate:g}%" if vat_on else "VAT", S['body']),
                      Paragraph(f"{cur} {fmt(vat)}" if vat_on else "Not applied", S['body'])],
                     [Paragraph('<b>Grand Total</b>', ParagraphStyle('gt3', parent=S['body'], textColor=white, fontSize=10.5)),
                      Paragraph(f"<b>{cur} {fmt(grand)}</b>", ParagraphStyle('gt4', parent=S['body'], textColor=white, fontSize=10.5))]]
            tot = Table(trows, colWidths=[CW*0.72, CW*0.28])
            tot.setStyle(TableStyle([('BOX',(0,0),(-1,1),0.6,GRAYB),('LINEBELOW',(0,0),(-1,1),0.4,GRAYB),
                                     ('BACKGROUND',(0,2),(-1,2),NAVY),('TOPPADDING',(0,0),(-1,-1),6),
                                     ('BOTTOMPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),10)]))
            E.append(KeepTogether(tot)); E.append(Spacer(1, 6*mm))
        E.append(Paragraph('Pricing valid for 30 days from the date of this proposal, subject to the Terms & Conditions of the associated quotation.', S['small']))

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
    co_row = sb.table('companies').select('name,legal_name,address,trn,phone,website,logo_url').eq('id', claims['company_id']).execute()
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
    content['customer_name'] = quote.get('customer') or ''
    content['date'] = (opts[0].get('date') if opts else '') or time.strftime('%d %B %Y')
    content['prepared_by'] = (opts[0].get('by') if opts else '') or ''

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
