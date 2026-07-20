from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import os, jwt, base64, json, re
import urllib.request, urllib.error
from supabase import create_client

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
JWT_SECRET   = os.environ.get('JWT_SECRET')
PDFSHIFT_API_KEY = os.environ.get('PDFSHIFT_API_KEY')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

def verify_token(req):
    auth = req.headers.get('Authorization','')
    if not auth.startswith('Bearer '): return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except:
        return None

# AUTH-001 fix: duplicated from api/quotes.py (same one-file-per-route
# constraint as every other cross-file duplication in this codebase -- see
# e.g. calc_item/calc_opt, the Zoho adapter). quote_pdf() below was
# company-scoped but not visibility-scoped, letting any teammate download
# another user's private quote as a PDF. Keep these three in sync by hand
# with api/quotes.py if the visibility rules ever change there.
def _can_view_all_quotes(claims):
    if claims['role'] == 'admin':
        return True
    u = sb.table('users').select('can_view_all_quotes').eq('id', claims['user_id']).execute()
    return bool(u.data and u.data[0].get('can_view_all_quotes'))

def _is_assigned_reviewer_on_quote(qid, user_id):
    q = sb.table('quotes').select('current_version').eq('id', qid).execute()
    if not q.data: return False
    vrows = sb.table('quote_versions').select('id').eq('quote_id', qid)\
        .eq('version_number', q.data[0].get('current_version')).execute()
    if not vrows.data: return False
    vid = vrows.data[0]['id']
    mine = sb.table('quote_version_reviewers').select('id').eq('version_id', vid).eq('user_id', user_id).execute()
    return bool(mine.data)

def _can_view_quote(qid, quote, claims):
    if quote.get('created_by') == claims['user_id'] or _can_view_all_quotes(claims):
        return True
    return _is_assigned_reviewer_on_quote(qid, claims['user_id'])


# ══ PDF builder (mirrors the app print design) ══════════════════════════════
import io, base64
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
                                Table, TableStyle, Image, KeepTogether)

NAVY   = HexColor('#16294f')   # matches the app's default "Navy Classic" print theme —
NAVY2  = HexColor('#16294f')   # this PDF always renders in that theme regardless of which
PILLBG = HexColor('#eaf1fc')   # color theme is selected on screen (reportlab can't easily
PILLTX = HexColor('#1a4fa0')   # reuse the CSS theme system used by the on-screen/print view)
GRAYPILLBG = HexColor('#f2f6fc')
GRAYPILLTX = HexColor('#5a6475')
AMBERBG = HexColor('#fef3c7')
AMBERTX = HexColor('#92400e')
TINT   = HexColor('#eaf1fc')
ZEBRA  = HexColor('#f8fafc')
GRAYB  = HexColor('#e2e6ec')
BANKBG = HexColor('#eef3fb')
BANKBR = HexColor('#d3e0f2')
TXT    = HexColor('#1c1f26')
GRAY   = HexColor('#7c8798')

# Per-company branding is built at request time from the `companies` table
# profile (see build_company_dict below) instead of one hardcoded identity —
# every tenant gets their own name/logo/address/TRN/bank details on their PDFs.
def build_company_dict(co, fallback_name):
    """co is a row from `companies` (may have empty profile fields if the admin
    hasn't filled in Company Profile yet); fallback_name is companies.name."""
    co = co or {}
    name = (co.get('legal_name') or fallback_name or '').upper()
    addr_bits = [b for b in [co.get('address')] if b]
    trn_tel_bits = []
    if co.get('trn'):   trn_tel_bits.append(f"TRN: {co['trn']}")
    if co.get('phone'): trn_tel_bits.append(f"Tel: {co['phone']}")
    bank_pairs = []
    for label, key in [('Bank Name:', 'bank_name'), ('Account Name:', 'bank_account_name'),
                        ('Account No:', 'bank_account_no'), ('IBAN:', 'bank_iban'),
                        ('Swift Code:', 'bank_swift'), ('Branch:', 'bank_branch')]:
        if co.get(key): bank_pairs.append((label, co[key]))
    footer_bits = addr_bits + ([f"TRN: {co['trn']}"] if co.get('trn') else [])
    return {
        'name': name or 'YOUR COMPANY',
        'sub':  '  |  '.join(addr_bits),
        'trn_tel': '  |  '.join(trn_tel_bits),
        'web': co.get('website') or '',
        'phone': co.get('phone') or '',
        'footer': '  |  '.join(footer_bits),
        'bank': bank_pairs,  # may be empty — bank block is skipped if so
    }

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
    return f"{n:,.0f}"

S = {
    'base':   ParagraphStyle('base', fontName='Helvetica', fontSize=8.5, leading=11, textColor=TXT),
    'baseR':  ParagraphStyle('baseR', fontName='Helvetica', fontSize=8.5, leading=11, textColor=TXT, alignment=TA_RIGHT),
    'baseC':  ParagraphStyle('baseC', fontName='Helvetica', fontSize=8.5, leading=11, textColor=TXT, alignment=TA_CENTER),
    'th':     ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=white),
    'co':     ParagraphStyle('co', fontName='Helvetica-Bold', fontSize=15, leading=18, textColor=NAVY),
    'cosub':  ParagraphStyle('cosub', fontName='Helvetica', fontSize=7.5, leading=10, textColor=GRAY),
    'lbl':    ParagraphStyle('lbl', fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=NAVY),
    'cust':   ParagraphStyle('cust', fontName='Helvetica-Bold', fontSize=11, leading=13, textColor=TXT),
    'term':   ParagraphStyle('term', fontName='Helvetica', fontSize=8, leading=11, textColor=TXT),
    'sig':    ParagraphStyle('sig', fontName='Helvetica-Bold', fontSize=8.5, leading=11, textColor=NAVY),
    'sigsub': ParagraphStyle('sigsub', fontName='Helvetica', fontSize=7.5, leading=10, textColor=GRAY),
}

def _footer(canvas, doc):
    company = getattr(doc, 'company', {}) or {}
    canvas.saveState()
    w, h = A4
    canvas.setStrokeColor(NAVY); canvas.setLineWidth(1.1)
    canvas.line(15*mm, 14*mm, w-15*mm, 14*mm)
    canvas.setFont('Helvetica', 6.8); canvas.setFillColor(GRAY)
    canvas.drawString(15*mm, 10*mm, company.get('footer') or '')
    if company.get('web'):
        canvas.setFont('Helvetica-Bold', 6.8); canvas.setFillColor(NAVY2)
        canvas.drawRightString(w-15*mm, 10*mm, company['web'])
    canvas.setFont('Helvetica', 6.8); canvas.setFillColor(GRAY)
    canvas.drawCentredString(w/2, 6*mm, f"Page {doc.page}")
    canvas.restoreState()

def build_quote_pdf(quote, logo_bytes, which='all', company=None):
    company = company or {}
    opts = quote.get('quote_data') or []
    if which != 'all':
        try: opts = [opts[int(which)]]
        except: opts = opts[:1]
    if not opts: opts = [{'label':'Option 1','sections':[]}]
    multi = len(opts) > 1
    terms = quote.get('terms_data') or []
    cur = quote.get('currency') or 'AED'

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=15*mm, rightMargin=15*mm,
                          topMargin=12*mm, bottomMargin=18*mm,
                          title=f"Quotation - {quote.get('title','')}")
    doc.company = company
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='f')
    doc.addPageTemplates([PageTemplate(id='p', frames=[frame], onPage=_footer)])
    E = []
    CW = doc.width

    first = opts[0]
    # ── Header: logo (if uploaded) | company block ──
    co_block = [Paragraph(company.get('name') or 'YOUR COMPANY', ParagraphStyle('con', parent=S['co'], alignment=TA_RIGHT))]
    if company.get('sub'):     co_block.append(Paragraph(company['sub'], ParagraphStyle('cs1', parent=S['cosub'], alignment=TA_RIGHT)))
    if company.get('trn_tel'): co_block.append(Paragraph(company['trn_tel'], ParagraphStyle('cs2', parent=S['cosub'], alignment=TA_RIGHT)))
    if logo_bytes:
        logo = Image(io.BytesIO(logo_bytes)); ratio = logo.imageWidth/float(logo.imageHeight)
        logo.drawHeight = 17*mm; logo.drawWidth = 17*mm*ratio
        head = Table([[logo, co_block]], colWidths=[CW*0.38, CW*0.62])
    else:
        head = Table([[co_block]], colWidths=[CW])
    head.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]))
    E.append(head)
    E.append(Spacer(1, 3*mm))
    rule = Table([['']], colWidths=[CW], rowHeights=[2.2])
    rule.setStyle(TableStyle([('BACKGROUND',(0,0),(0,0),NAVY)]))
    E.append(rule); E.append(Spacer(1, 4*mm))

    # ── Customer / prepared-by ──
    by = first.get('by') or company.get('name') or ''
    prep_lines = [Paragraph('PREPARED BY', ParagraphStyle('lr', parent=S['lbl'], alignment=TA_RIGHT)),
                  Paragraph(by, ParagraphStyle('byr', parent=S['base'], alignment=TA_RIGHT))]
    if company.get('phone'):
        prep_lines.append(Paragraph(f"Mob: {company['phone']}", ParagraphStyle('mr', parent=S['cosub'], alignment=TA_RIGHT)))
    cust = Table([[[Paragraph('QUOTATION FOR', S['lbl']), Paragraph(quote.get('customer') or '—', S['cust'])],
                   prep_lines]],
                 colWidths=[CW*0.55, CW*0.45])
    cust.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]))
    E.append(cust); E.append(Spacer(1, 3*mm))

    # ── Meta pills: ref (navy tint) + date + scope (neutral) — matches the
    # small rounded badge row above the customer block in the on-screen view.
    def _pill(text, bg, tx):
        p = Paragraph(text, ParagraphStyle('pill', fontName='Helvetica-Bold', fontSize=8, textColor=tx))
        t = Table([[p]], colWidths=[None])
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),bg),
                                ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
                                ('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),9)]))
        return t
    pills = [_pill(first.get('ref') or '', PILLBG, PILLTX), _pill(first.get('date') or '', GRAYPILLBG, GRAYPILLTX)]
    scope_name = (first.get('sections') or [{}])[0].get('name') if first.get('sections') else ''
    if scope_name: pills.append(_pill(scope_name, GRAYPILLBG, GRAYPILLTX))
    bar = Table([pills], colWidths=[None]*len(pills), hAlign='LEFT')
    bar.setStyle(TableStyle([('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(1,0),(-1,-1),6),
                             ('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
    E.append(bar); E.append(Spacer(1, 5*mm))

    # ── Options ──
    for oi, o in enumerate(opts):
        ts, vat_on, rate, vat, grand = calc_opt(o)
        if multi:
            ob = Table([[Paragraph(f"<b>{o.get('label') or f'Option {oi+1}'}</b>",
                                   ParagraphStyle('ob', fontName='Helvetica-Bold', fontSize=9.5, textColor=white))]],
                       colWidths=[CW])
            ob.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),NAVY),('TOPPADDING',(0,0),(-1,-1),6),
                                    ('BOTTOMPADDING',(0,0),(-1,-1),6),('LEFTPADDING',(0,0),(-1,-1),10)]))
            E.append(ob)

        # Each section gets its own compact navy badge (standalone, above the
        # table) followed by its own item table — mirrors the on-screen/print
        # view, where sections are visually separate blocks rather than rows
        # merged into one continuous table.
        widths = [CW*0.04, CW*0.09, CW*0.12, CW*0.30, CW*0.11, CW*0.05, CW*0.06, CW*0.10, CW*0.13]
        header = [Paragraph(x, S['th']) for x in ['#','Brand','Model','Description',f'Unit ({cur})','Qty','UOM','VAT',f'Total ({cur})']]
        rn = 1
        for s in (o.get('sections') or []):
            if s.get('name'):
                badge = Table([[Paragraph(s['name'], ParagraphStyle('secb', fontName='Helvetica-Bold', fontSize=8.5, textColor=white))]], colWidths=[None])
                badge.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),NAVY),
                                            ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
                                            ('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10)]))
                E.append(badge); E.append(Spacer(1, 2*mm))
            rows = [header]
            for it in (s.get('items') or []):
                up, tp = calc_item(it)
                vat_amt = tp * (rate/100.0) if vat_on else None
                vat_style = ParagraphStyle('vatc', parent=S['baseR'], textColor=HexColor('#a8b0bd'))
                rows.append([Paragraph(str(rn), S['baseC']), Paragraph(str(it.get('brand') or ''), S['base']),
                             Paragraph(str(it.get('model') or ''), S['base']), Paragraph(str(it.get('desc') or ''), S['base']),
                             Paragraph(fmt(up), S['baseR']), Paragraph(fmt(_num(it.get('qty'))), S['baseC']),
                             Paragraph(str(it.get('uom') or 'Pcs'), S['baseC']),
                             Paragraph(fmt(vat_amt) if vat_amt is not None else '—', vat_style),
                             Paragraph(fmt(tp), S['baseR'])])
                rn += 1
            tbl = Table(rows, colWidths=widths, repeatRows=1)
            style = [('BACKGROUND',(0,0),(-1,0),NAVY),('VALIGN',(0,0),(-1,-1),'TOP'),
                     ('LINEBELOW',(0,1),(-1,-1),0.4,GRAYB),
                     ('LINEAFTER',(0,0),(-2,0),0.5,HexColor('#3a4d75')),
                     ('LINEAFTER',(0,1),(-2,-1),0.5,GRAYB),
                     ('TOPPADDING',(0,0),(-1,-1),4.5),('BOTTOMPADDING',(0,0),(-1,-1),4.5),
                     ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5)]
            for ri in range(1, len(rows)):
                if (ri - 1) % 2 == 1:
                    style += [('BACKGROUND',(0,ri),(-1,ri),ZEBRA)]
            tbl.setStyle(TableStyle(style))
            E.append(tbl); E.append(Spacer(1, 4*mm))

        # Vertical, right-aligned stack of totals cards — Subtotal, then VAT,
        # then a larger solid-navy Grand Total card below — matching the
        # on-screen/print totals layout instead of one wide table.
        TOTW = CW*0.42
        def _trow(label, value, grand=False):
            lbl_style = ParagraphStyle('tl', fontName='Helvetica-Bold', fontSize=7.5 if not grand else 8.5,
                                        textColor=(white if grand else GRAY))
            val_style = ParagraphStyle('tv', parent=S['baseR'], fontSize=11 if not grand else 13,
                                        fontName='Helvetica-Bold', textColor=(white if grand else NAVY))
            t = Table([[Paragraph(label, lbl_style), Paragraph(value, val_style)]], colWidths=[TOTW*0.5, TOTW*0.5])
            t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1), NAVY if grand else HexColor('#f7f9fc')),
                                   ('TOPPADDING',(0,0),(-1,-1), 8 if grand else 6),
                                   ('BOTTOMPADDING',(0,0),(-1,-1), 8 if grand else 6),
                                   ('LEFTPADDING',(0,0),(-1,-1),12),('RIGHTPADDING',(0,0),(-1,-1),12),
                                   ('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
            return t
        tot_rows = [[_trow('Subtotal', f"{cur} {fmt(ts)}")],
                    [Spacer(1, 1.5*mm)],
                    [_trow(f"VAT {rate:g}%" if vat_on else "VAT", f"{cur} {fmt(vat)}" if vat_on else "Not applied")],
                    [Spacer(1, 1.5*mm)],
                    [_trow('Grand total', f"{cur} {fmt(grand)}", grand=True)]]
        tot_stack = Table(tot_rows, colWidths=[TOTW])
        tot_stack.setStyle(TableStyle([('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),
                                       ('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),0)]))
        tot_stack.hAlign = 'RIGHT'
        E.append(KeepTogether(tot_stack)); E.append(Spacer(1, 6*mm))

    # ── Terms (compact badge sized to its text, not a full-width bar) ──
    th = Table([[Paragraph('Terms &amp; Conditions', ParagraphStyle('tt', fontName='Helvetica-Bold', fontSize=8.5, textColor=white))]], colWidths=[None])
    th.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),NAVY),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
                            ('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10)]))
    tflow = [th, Spacer(1, 3*mm)]
    for i, t in enumerate(terms):
        tflow.append(Paragraph(f"{i+1}.  {t}", S['term']))
    E.append(KeepTogether(tflow) if len(terms) <= 10 else tflow[0])
    if len(terms) > 10:
        E.append(Spacer(1, 2*mm))
        for i, t in enumerate(terms): E.append(Paragraph(f"{i+1}.  {t}", S['term']))
    E.append(Spacer(1, 8*mm))

    # ── Bank details (only if this company has filled them in) ──
    b = company.get('bank') or []
    if b:
        brows = [[Paragraph(f'<b>{b[i][0]}</b>', S['term']), Paragraph(b[i][1], S['term']),
                  Paragraph(f'<b>{b[i+1][0]}</b>', S['term']) if i+1 < len(b) else '',
                  Paragraph(b[i+1][1], S['term']) if i+1 < len(b) else ''] for i in range(0, len(b), 2)]
        bank_inner = Table(brows, colWidths=[CW*0.16, CW*0.36, CW*0.14, CW*0.34])
        bank_inner.setStyle(TableStyle([('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2)]))
        bank = Table([[Paragraph('<b>Bank Details (Wire Transfer)</b>', ParagraphStyle('bt', parent=S['lbl']))],[bank_inner]], colWidths=[CW])
        bank.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),BANKBG),('BOX',(0,0),(-1,-1),0.8,BANKBR),
                                  ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),('LEFTPADDING',(0,0),(-1,-1),10)]))
        E.append(KeepTogether(bank))

    doc.build(E)
    return buf.getvalue()


import json as _json

def _fetch_logo_bytes(logo_url):
    """Download the company's own uploaded logo for embedding in the PDF. Returns
    None (no logo drawn — a clean text-only header) if there isn't one or the
    fetch fails, rather than falling back to any hardcoded default company's
    logo, which would be wrong for every tenant but that one."""
    if not logo_url: return None
    try:
        req = urllib.request.Request(logo_url, headers={'User-Agent': 'Mozilla/5.0 (SysconicQuotes PDF)'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception:
        return None

@app.route('/api/pdf/quote/<qid>', methods=['GET'])
def quote_pdf(qid):
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401

    row = sb.table('quotes').select('*').eq('id', qid).eq('company_id', claims['company_id']).execute()
    if not row.data: return jsonify({'error': 'Not found'}), 404
    quote = row.data[0]
    if not _can_view_quote(qid, quote, claims):
        return jsonify({'error': 'Forbidden'}), 403
    for f in ('quote_data','terms_data','vendor_data'):
        if isinstance(quote.get(f), str):
            try: quote[f] = _json.loads(quote[f])
            except: quote[f] = []

    co_row = sb.table('companies').select('name,legal_name,address,trn,phone,website,logo_url,'
                                           'bank_name,bank_account_name,bank_account_no,bank_iban,bank_swift,bank_branch') \
        .eq('id', claims['company_id']).execute()
    co_raw = co_row.data[0] if co_row.data else {}
    company = build_company_dict(co_raw, co_raw.get('name'))
    logo_bytes = _fetch_logo_bytes(co_raw.get('logo_url'))

    which = request.args.get('which', 'all')
    try:
        pdf = build_quote_pdf(quote, logo_bytes, which, company)
    except Exception as e:
        return jsonify({'error': f'PDF generation failed: {str(e)[:200]}'}), 500

    ref = ''
    try: ref = (quote.get('quote_data') or [{}])[0].get('ref') or ''
    except: pass
    fname = f"Quotation-{ref or qid[:8]}.pdf".replace(' ', '-')
    return Response(pdf, mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})


# ══ Pixel-exact PDF via PDFShift ═════════════════════════════════════════════
# Renders the *actual* HTML the browser shows (built client-side from the same
# printHTML() used for the on-screen print view), through a real Chromium
# instance — guaranteeing the download matches what the user sees, instead of
# the best-effort reportlab reconstruction above (kept as a fallback route).
@app.route('/api/pdf/render', methods=['POST'])
def render_pdf():
    claims = verify_token(request)
    if not claims: return jsonify({'error': 'Unauthorized'}), 401
    if not PDFSHIFT_API_KEY:
        return jsonify({'error': 'PDF rendering is not configured (missing PDFSHIFT_API_KEY)'}), 500

    d = request.json or {}
    html = d.get('html', '')
    if not html:
        return jsonify({'error': 'No HTML provided'}), 400
    filename = (d.get('filename') or 'Quotation.pdf').strip()
    filename = re.sub(r'[\r\n"\\]', '', filename)[:150] or 'Quotation.pdf'
    if not filename.lower().endswith('.pdf'):
        filename += '.pdf'

    payload_dict = {
        'source': html,
        'use_print': True,   # apply the same @media print CSS rules the browser uses
        'format': 'A4',
        'sandbox': False,
    }

    # Optional repeating per-page footer (e.g. company website), independent
    # of the main document -- PDFShift renders it fresh on every page from
    # `start_at` onward. The footer HTML must be self-contained (no external
    # CSS/JS), which the frontend's pdfFooterHTML() already guarantees.
    footer_html = (d.get('footer_html') or '').strip()
    if footer_html:
        try:
            footer_start_at = int(d.get('footer_start_at') or 2)
        except (TypeError, ValueError):
            footer_start_at = 2
        payload_dict['footer'] = {
            'source': footer_html,
            'height': '30px',
            'start_at': max(1, footer_start_at),
        }

    payload = json.dumps(payload_dict).encode('utf-8')

    req = urllib.request.Request(
        'https://api.pdfshift.io/v3/convert/pdf',
        data=payload,
        headers={'X-API-Key': PDFSHIFT_API_KEY, 'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            pdf_bytes = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        return jsonify({'error': f'PDF service error ({e.code}): {body[:300]}'}), 502
    except Exception as e:
        return jsonify({'error': f'PDF generation failed: {str(e)[:200]}'}), 500

    return Response(pdf_bytes, mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})

if __name__ == '__main__':
    app.run(debug=True)