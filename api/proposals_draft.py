"""
api/proposals_draft.py — Proposals module: the 4-step flow.

  1. Generate FULL content (structured document model; every section editable/removable)
  2. Generate several images (one slot per image; regenerate / edit prompt / remove each)
  3. Review the assembled document, edit, confirm
  4. Confirm freezes an immutable document version (PDF is built from that)

Registered onto the Flask app in api/proposals.py (same /api/proposals/* routes,
same auth gate, same Vercel function). Customer-safe snapshot only: no cost,
margin or supplier discount ever reaches a model. Prices are never sent to a
model either - they are added to the document by code, not by AI.
"""
import os, re, json, base64, time
from datetime import datetime, timedelta
from flask import request, jsonify, g

STALE_MINUTES = 8
MAX_CONTENT_BYTES = 250_000
LIST_KEYS = ('kpis', 'systems', 'terms', 'confirm')


def register(app, H):
    get_sb = H['get_sb']
    GenError = H['GenError']

    def now():
        return datetime.utcnow().isoformat()

    # ── helpers ───────────────────────────────────────────────────────────────
    def clip(v, n):
        return str(v if v is not None else '').strip()[:n]

    def company_facts(sb, company_id):
        try:
            r = sb.table('companies').select('name,legal_name,about_us_text,why_us_bullets,scope_ours_bullets,scope_client_bullets,'
                                             'warranty_text,default_validity_days,default_delivery_weeks,default_payment_terms')\
                .eq('id', company_id).execute().data
            co = r[0] if r else {}
        except Exception:
            co = {}

        def lst(v):
            if isinstance(v, list): return [clip(x, 300) for x in v if x]
            return [clip(x, 300) for x in re.split(r'[\r\n]+', str(v or '')) if x.strip()]
        return {'name': co.get('legal_name') or co.get('name') or '', 'about': clip(co.get('about_us_text'), 1500),
                'why_us': lst(co.get('why_us_bullets')), 'scope_ours': lst(co.get('scope_ours_bullets')),
                'scope_client': lst(co.get('scope_client_bullets')), 'warranty': clip(co.get('warranty_text'), 800),
                'validity_days': co.get('default_validity_days') or '', 'delivery_weeks': co.get('default_delivery_weeks') or '',
                'payment_terms': clip(co.get('default_payment_terms'), 500)}

    def sweep(sb, pid):
        cutoff = (datetime.utcnow() - timedelta(minutes=STALE_MINUTES)).isoformat()
        sb.table('proposal_drafts').update({'status': 'failed', 'error': 'Generation timed out. Please try again.'})\
            .eq('proposal_id', pid).in_('status', ['queued', 'running']).lt('updated_at', cutoff).execute()
        icut = (datetime.utcnow() - timedelta(minutes=6)).isoformat()
        sb.table('proposal_images').update({'status': 'failed', 'error': 'Image generation timed out (the provider took too long or the request was interrupted). Try again, or use fewer reference photos.'})\
            .eq('proposal_id', pid).in_('status', ['queued', 'running']).lt('updated_at', icut).execute()

    def latest_snapshot(sb, pid):
        r = sb.table('proposal_source_snapshots').select('*').eq('proposal_id', pid).order('created_at', desc=True).limit(1).execute().data
        return r[0] if r else None

    def image_public(row):
        out = {k: row.get(k) for k in ('slot_key', 'title', 'caption', 'prompt', 'model_refs', 'status', 'include', 'sort',
                                         'image_model', 'error', 'updated_at', 'started_at')}
        out['kind'] = row.get('kind') or 'generated'
        out['source_url'] = None
        if row.get('source_path'):
            try:
                from api.auth import _signed_url
                out['source_url'] = _signed_url(H['PREVIEW_BUCKET'], row['source_path'], 3600)
            except Exception:
                pass
        out['url'] = None
        if row.get('image_path'):
            try:
                from api.auth import _signed_url
                out['url'] = _signed_url(H['PREVIEW_BUCKET'], row['image_path'], 3600)
            except Exception:
                pass
        return out

    # ── LLM call ──────────────────────────────────────────────────────────────
    def llm(profile, system, user, max_tokens, timeout):
        secret = os.environ.get(profile['credential_ref'], '')
        if not secret:
            raise GenError(f"server variable {profile['credential_ref']} is not set")
        post = H['_post_json']
        if profile['provider'] == 'anthropic':
            r = post('https://api.anthropic.com/v1/messages', {'x-api-key': secret, 'anthropic-version': '2023-06-01'},
                     {'model': profile['model'], 'max_tokens': max_tokens, 'system': system,
                      'messages': [{'role': 'user', 'content': user}]}, timeout)
            return ''.join(b.get('text', '') for b in (r.get('content') or []) if b.get('type') == 'text')
        if profile['provider'] == 'openai':
            r = post('https://api.openai.com/v1/chat/completions', {'Authorization': 'Bearer ' + secret},
                     {'model': profile['model'], 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}, timeout)
            return (((r.get('choices') or [{}])[0]).get('message') or {}).get('content') or ''
        raise GenError('text generation supports Anthropic or OpenAI profiles')

    COMMON_RULES = """STRICT RULES
- Use ONLY equipment, quantities and descriptions in the BOQ. Never invent products, quantities, sizes, performance figures, certifications, standards, prices, or company policies. You may explain in general terms what a product CLASS does, but add no numeric specification that is not in the BOQ description.
- Never mention cost, margin, discounts or prices (prices are added by the system).
- Refer to BOQ lines only by line_id; quantities are added by the system.
- Delivery time, warranty, payment and validity may be quoted ONLY from "quote_terms" or "company_facts" when present. If a fact is not given, do not state it.
- Anything uncertain or site-dependent is an assumption or a point to confirm, never stated as fact.
- Use the customer brief/guidance, if given, as the source for requirements and site context; without one, do not invent a site description.
- Write like a senior pre-sales engineer: specific to THIS customer and THIS equipment, plain professional English, no marketing hype, no filler. Vary wording; do not reuse stock sentences.
- DOCUMENT TYPE: follow "document_type_rule" in the input when it is present.
- LENGTH: if "min_pages" is given and above 0, the finished document (cover, contents, bill of quantities and acceptance take about 4 pages by themselves) must reach at least that many A4 pages at roughly 330 words per page. "depth_target_words" is YOUR share of the words: reach it with real substance (how each item is used, benefits for this customer, installation considerations, operation and maintenance, training, interoperability, risks and how they are mitigated, future expansion), never with repetition, filler or invented facts.
- Output ONLY one JSON object. No markdown fences, no commentary."""

    SYSTEM_A = """You write the NARRATIVE half of a detailed, customer-facing technical and commercial proposal for an audio-visual systems integrator, from a bill of quantities (BOQ). It will run to many pages, so be thorough: this is a full proposal, not a summary.

Plan the sections yourself for THIS project (do not follow a rigid template) but a complete proposal normally contains, in this order:
 1. an executive summary (2-3 substantial paragraphs; what is being delivered, for whom, why it fits; then a "kpis" block of 3-5 headline figures taken from BOQ quantities/sizes and, if given, quoted warranty/delivery),
 2. understanding of the requirement (what the customer needs, as objectives, and the key assumptions): use "text" then "points" for assumptions,
 3. the proposed solution: EXACTLY ONE "systems" section covering EVERY BOQ line, grouped sensibly.
For a multi-room or multi-system project add a section per room/system family where it helps. Scale depth to the BOQ: a handful of lines still deserves a full, well-argued proposal; a large system deserves proportionally more.

Section types: each is {"type","title",...}
- "text":    {"paragraphs": [str]}   each paragraph 50-120 words
- "kpis":    {"items": [{"value","label","sub"}]}
- "columns": {"intro", "items": [{"title","body"}]}  2-4 side-by-side ideas
- "points":  {"intro", "items": [{"title","body"}]}  numbered points (objectives, assumptions, points to confirm)
- "systems": {"intro", "groups": [{"id","title","intro","items": [{"line_ids": [str], "does": str}]}]}  "does" = 40-90 words: what this item does for this customer and how it is used
Use "columns"/"points" only where they fit.

""" + COMMON_RULES + """

OUTPUT:
{
 "cover": {"title","subtitle","scope_line"},
 "sections": [ ... ],
 "images": [{"key","title","caption","system_id" (a group id or "" for the main image),"line_ids": [str],
             "prompt": (<=130 words: photorealistic architectural visualisation showing ONLY the equipment in line_ids, plausible positions, exact counts; screens blank/abstract; no readable text, logos or faces)}]
}
The first image must have key "hero". Add further images only where they help, up to max_images."""

    SYSTEM_B = """You write the DESIGN and DELIVERY half of a detailed customer-facing technical proposal for an audio-visual systems integrator, from a bill of quantities (BOQ). Another writer covers the executive summary, requirements and equipment narrative; you cover how it is designed, installed, tested and supported. Be concrete and specific to THIS equipment.

Return two lists of sections.

"design_sections" (appear after the equipment narrative, before the bill of quantities). Normally:
 - mounting / installation design (text + a short "points" or "list" of what the customer must provide, general practice only),
 - a system architecture "diagram" (kind "architecture") plus a "table" explaining the layers,
 - a typical room layout "diagram" (kind "layout") plus a short "list" of design considerations (only if there are displays/speakers/mics/cameras/seating-related equipment in the BOQ),
 - a "scope" section (what is excluded and change control).
"delivery_sections" (appear after the bill of quantities). Normally:
 - implementation methodology: a "table" with columns ["Step","Activity","Output"] (5-7 steps),
 - project schedule: a "diagram" (kind "schedule"): ONLY if a delivery time is stated in quote_terms or company_facts; total weeks must fit within it (use the upper figure of a range),
 - testing, commissioning and handover: a "table" with columns ["Check","What is verified"] (6-9 checks specific to the equipment), plus a "list" of handover deliverables,
 - support: a "table" (columns ["Step","What happens"]) describing the support process; mention warranty only as given.
 - when "min_pages" calls for more depth, also add: operation and maintenance (text + list), training and handover plan (table), risks and mitigation (table with columns ["Risk","Mitigation"]), and future expansion (text) as they genuinely apply.
Skip any section that would be generic filler for this BOQ, but a full proposal normally has most of them.

Section types (each {"type","title",...}):
- "text":    {"paragraphs": [str]}
- "list":    {"intro": str, "items": [{"text": str}]}
- "points":  {"intro": str, "items": [{"title","body"}]}
- "table":   {"intro": str, "columns": [str] (2-4), "rows": [[str,...]] (cells per row = number of columns)}
- "scope":   {"intro": str, "excluded": [str], "note": str}   included items are generated by the system from the BOQ; "note" = change-control wording
- "diagram": {"kind","caption","spec"} where spec depends on kind:
    architecture: {"cols": [str] (2-4 column headings, e.g. "Users","Network","Room systems"), "nodes": [{"id","label","sub","col": int (0-based),"tone": "sysconic"|"client"|"neutral"}] (4-10 nodes; tone "sysconic" = supplied/installed by us, "client" = provided by the customer), "edges": [{"from","to","label"}]}
    layout: {"label": str, "front": [str] (items on the front wall), "ceiling": [str], "table": [str] (items on the table/furniture), "seats": int, "rear": [str]}   a schematic room; leave a list empty if it does not apply; use short labels (<=24 chars); seats only if stated or clearly implied, else 0
    schedule: {"weeks": int (1-26), "tasks": [{"name","start": int (1-based week),"end": int}]} (5-8 tasks, in order, end>=start, end<=weeks)

""" + COMMON_RULES + """

OUTPUT:
{"design_sections": [ ... ], "delivery_sections": [ ... ]}"""

    DOC_RULES = {
        'technical': 'This is a TECHNICAL proposal. Do not mention prices, payment terms, warranty period, delivery time, validity or any other commercial term anywhere, and do not use such facts in key figures.',
        'commercial': 'This is a COMMERCIAL offer. Keep the executive summary and the understanding of the requirement focused and concise (scope, value, what is included). Detailed technical design is not wanted.',
    }

    def build_user_prompt(p, snap, instr, co, max_images, terms=None, half='A', min_pages=0, doc_type='both'):
        lines = []
        for i, l in enumerate((snap['safe_source'].get('lines') or []), 1):
            lines.append({'line_id': l.get('source_line_id'), 'boq_no': i, 'section': l.get('section'),
                          'brand': l.get('manufacturer'), 'model': l.get('model'), 'description': clip(l.get('description'), 400),
                          'qty': l.get('quantity'), 'unit': l.get('unit')})
        brief = {'customer': p.get('customer_name'), 'project': p.get('title'), 'quotation_ref': p.get('quote_ref'),
                 'customer_brief_and_guidance': ' '.join(x for x in (p.get('instruction'), instr) if x) or '(none)',
                 'quote_terms': terms or [], 'company_facts': co, 'boq': lines[:120]}
        if half == 'A': brief['max_images'] = max_images
        if doc_type in DOC_RULES:
            brief['document_type'] = doc_type; brief['document_type_rule'] = DOC_RULES[doc_type]
            if doc_type == 'technical': brief['quote_terms'] = []
        if min_pages:
            need = max(0, (min_pages - 4) * 330)
            brief['min_pages'] = min_pages
            brief['depth_target_words'] = int(need * (0.55 if half == 'A' else 0.45))
        return 'Write the proposal content for this project. Input (JSON):\n' + json.dumps(brief, ensure_ascii=False)

    def _int(x, lo, hi, d):
        try: v = int(float(x))
        except Exception: return d
        return max(lo, min(hi, v))

    def parse_terms(terms):
        """Facts pulled from the quotation's own terms lines (code, not AI)."""
        f = {'payment': '', 'delivery': '', 'warranty': '', 'validity': ''}
        for t in terms or []:
            m = re.match(r'\s*(payment|delivery|warranty|validity)\b[^:]*:\s*(.+)$', t, re.I)
            if m and not f[m.group(1).lower()]: f[m.group(1).lower()] = clip(m.group(2), 200)
        miles = []
        for m in re.finditer(r'(\d{1,3}(?:\.\d+)?)\s*%\s*([^,;.\n]{2,60})', f['payment']):
            miles.append({'pct': float(m.group(1)), 'label': clip(m.group(2).strip(' .'), 60)})
        f['milestones'] = miles if miles and abs(sum(x['pct'] for x in miles) - 100) < 0.51 else []
        return f

    def normalise(raw, snap, co, p, max_images, terms=None, raw_b=None):
        """Model output -> editable document model (v3: a free-form list of sections).
        Every BOQ reference is validated; quantities/models come from the BOQ, never the model."""
        lines = snap['safe_source'].get('lines') or []
        by_id = {l.get('source_line_id'): (i + 1, l) for i, l in enumerate(lines)}
        used, seen_ids = set(), set()
        priced = snap.get('visibility') == 'commercial' and bool((snap['safe_source'] or {}).get('commercials'))
        facts = parse_terms(terms)

        def slug(x, fallback):
            v = re.sub(r'[^a-z0-9]+', '-', clip(x, 40).lower()).strip('-') or fallback
            base, n = v, 2
            while v in seen_ids:
                v = f'{base}-{n}'; n += 1
            seen_ids.add(v)
            return v

        def items_of(lst, n, fields):
            out = []
            for x in (lst or [])[:n]:
                if isinstance(x, dict):
                    d = {k: clip(x.get(k), lim) for k, lim in fields.items()}
                    if any(d.values()):
                        d['include'] = True; out.append(d)
            return out

        def diagram(sec, base):
            kind, sp = sec.get('kind'), (sec.get('spec') or {})
            if not isinstance(sp, dict): return None
            if kind == 'architecture':
                cols = [clip(c, 30) for c in (sp.get('cols') or [])[:4] if clip(c, 1)]
                ncol = max(len(cols), 1)
                nodes, ids = [], set()
                for n in (sp.get('nodes') or [])[:10]:
                    if not isinstance(n, dict) or not clip(n.get('label'), 1): continue
                    nid = clip(n.get('id'), 20) or f'n{len(nodes)+1}'
                    if nid in ids: continue
                    ids.add(nid)
                    tone = n.get('tone') if n.get('tone') in ('sysconic', 'client', 'neutral') else 'neutral'
                    nodes.append({'id': nid, 'label': clip(n.get('label'), 40), 'sub': clip(n.get('sub'), 50),
                                  'col': _int(n.get('col'), 0, ncol - 1, 0), 'tone': tone})
                edges = []
                for e in (sp.get('edges') or [])[:14]:
                    if isinstance(e, dict) and clip(e.get('from'), 20) in ids and clip(e.get('to'), 20) in ids:
                        edges.append({'from': clip(e.get('from'), 20), 'to': clip(e.get('to'), 20), 'label': clip(e.get('label'), 30)})
                if len(nodes) < 2: return None
                return {**base, 'type': 'diagram', 'kind': kind, 'caption': clip(sec.get('caption'), 200),
                        'spec': {'cols': cols, 'nodes': nodes, 'edges': edges}}
            if kind == 'layout':
                def lab(k, n): return [clip(x, 28) for x in (sp.get(k) or [])[:n] if clip(x, 1)]
                spec = {'label': clip(sp.get('label'), 50), 'front': lab('front', 6), 'ceiling': lab('ceiling', 6),
                        'table': lab('table', 6), 'rear': lab('rear', 4), 'seats': _int(sp.get('seats'), 0, 40, 0)}
                if not (spec['front'] or spec['ceiling'] or spec['table'] or spec['rear']): return None
                return {**base, 'type': 'diagram', 'kind': kind, 'caption': clip(sec.get('caption'), 200), 'spec': spec}
            if kind == 'schedule':
                weeks = _int(sp.get('weeks'), 1, 26, 0)
                tasks = []
                for t in (sp.get('tasks') or [])[:10]:
                    if not isinstance(t, dict) or not clip(t.get('name'), 1): continue
                    s0 = _int(t.get('start'), 1, 26, 1); e0 = _int(t.get('end'), 1, 26, s0)
                    tasks.append({'name': clip(t.get('name'), 60), 'start': s0, 'end': max(s0, e0)})
                if tasks and not weeks: weeks = max(t['end'] for t in tasks)
                weeks = max(weeks, max((t['end'] for t in tasks), default=0))
                if not tasks or not weeks: return None
                return {**base, 'type': 'diagram', 'kind': kind, 'caption': clip(sec.get('caption'), 200), 'spec': {'weeks': weeks, 'tasks': tasks}}
            return None

        def parse_section(sec, si, allow_systems=False):
            """Returns a normalised section dict or None."""
            nonlocal systems_done
            if not isinstance(sec, dict): return None
            t = sec.get('type')
            title = clip(sec.get('title'), 100)
            base = {'id': slug(title or t, f'sec-{si+1}'), 'type': t, 'title': title, 'include': True}
            if t == 'text':
                paras = [clip(x, 2000) for x in (sec.get('paragraphs') or [])[:10] if clip(x, 1)]
                return {**base, 'paragraphs': paras} if paras else None
            if t == 'kpis':
                its = [i for i in items_of(sec.get('items'), 8, {'value': 24, 'label': 60, 'sub': 80}) if i['value']]
                return {**base, 'items': its} if its else None
            if t == 'columns':
                its = items_of(sec.get('items'), 6, {'title': 60, 'body': 400})
                return {**base, 'intro': clip(sec.get('intro'), 900), 'items': its} if its else None
            if t == 'points':
                its = items_of(sec.get('items'), 12, {'title': 100, 'body': 700})
                return {**base, 'intro': clip(sec.get('intro'), 700), 'items': its} if its else None
            if t == 'list':
                its = []
                for x in (sec.get('items') or [])[:14]:
                    tx = clip(x.get('text') if isinstance(x, dict) else x, 400)
                    if tx: its.append({'text': tx, 'include': True})
                return {**base, 'intro': clip(sec.get('intro'), 700), 'items': its} if its else None
            if t == 'table':
                cols = [clip(c, 40) for c in (sec.get('columns') or [])[:4] if clip(c, 1)]
                if len(cols) < 2: return None
                rows = []
                for r in (sec.get('rows') or [])[:14]:
                    if isinstance(r, list):
                        cells = [clip(c, 400) for c in r[:len(cols)]]
                        cells += [''] * (len(cols) - len(cells))
                        if any(cells): rows.append({'cells': cells, 'include': True})
                return {**base, 'intro': clip(sec.get('intro'), 700), 'columns': cols, 'rows': rows} if rows else None
            if t == 'scope':
                ex = [{'text': clip(x if not isinstance(x, dict) else x.get('text'), 300), 'include': True} for x in (sec.get('excluded') or [])[:10]]
                ex = [x for x in ex if x['text']]
                return {**base, 'type': 'scope', 'intro': clip(sec.get('intro'), 600), 'excluded': ex, 'note': clip(sec.get('note'), 500)}
            if t == 'diagram':
                return diagram(sec, base)
            if t == 'systems' and allow_systems and not systems_done:
                groups = []
                for gi, gr in enumerate((sec.get('groups') or [])[:10]):
                    if not isinstance(gr, dict): continue
                    items = []
                    for it in (gr.get('items') or []):
                        if not isinstance(it, dict): continue
                        ids = [x for x in (it.get('line_ids') or []) if x in by_id and x not in used]
                        if not ids: continue
                        for x in ids: used.add(x)
                        items.append({'line_ids': ids, 'does': clip(it.get('does'), 700), 'include': True})
                    if items:
                        groups.append({'id': slug(gr.get('id') or gr.get('title'), f'grp-{gi+1}'), 'title': clip(gr.get('title'), 100),
                                       'intro': clip(gr.get('intro'), 900), 'items': items, 'image': '', 'include': True})
                systems_done = True
                return {**base, 'type': 'systems', 'intro': clip(sec.get('intro'), 900), 'groups': groups}
            return None

        systems_done = False
        sections = []
        for si, sec in enumerate((raw.get('sections') or [])[:16]):
            s = parse_section(sec, si, allow_systems=True)
            if s: sections.append(s)
        rest = [l.get('source_line_id') for l in lines if l.get('source_line_id') not in used]
        sysec = next((s for s in sections if s['type'] == 'systems'), None)
        if sysec is None:
            sysec = {'id': slug('systems', 'systems'), 'type': 'systems', 'title': 'Proposed solution', 'include': True, 'intro': '', 'groups': []}
            sections.append(sysec)
        if rest:
            sysec['groups'].append({'id': slug('other', 'other'), 'title': 'Other equipment and services', 'intro': '', 'image': '', 'include': True,
                                    'items': [{'line_ids': [x], 'does': '', 'include': True} for x in rest]})
        grp_ids = {g['id'] for g in sysec['groups']}
        images, seen = [], set()
        for im in (raw.get('images') or [])[:max_images]:
            if not isinstance(im, dict): continue
            key = re.sub(r'[^a-z0-9]+', '-', clip(im.get('key'), 40).lower()).strip('-') or f'img-{len(images)+1}'
            if key in seen: continue
            seen.add(key)
            ids = [x for x in (im.get('line_ids') or []) if x in by_id]
            models = []
            for x in ids:
                m = (by_id[x][1].get('model') or '').strip()
                if m and m not in models: models.append(m)
            sid = im.get('system_id') if im.get('system_id') in grp_ids else ''
            images.append({'key': key, 'title': clip(im.get('title'), 120), 'caption': clip(im.get('caption'), 300),
                           'prompt': clip(im.get('prompt'), 1200), 'system_id': sid, 'models': models[:6]})
        if images and not any(i['key'] == 'hero' for i in images):
            images[0]['key'] = 'hero'; images[0]['system_id'] = ''
        for im in images:
            if im['system_id']:
                for g_ in sysec['groups']:
                    if g_['id'] == im['system_id'] and not g_['image']: g_['image'] = im['key']

        # ── assemble in reading order ──
        # 1) narrative (+ code-generated investment summary and About from the company profile)
        pre = [s for s in sections if s is not sysec]
        head_i = next((i for i, s in enumerate(pre) if s['type'] in ('text', 'kpis')), -1)
        # executive summary = the leading text (+ kpis if it follows)
        cut = 0
        for s in pre:
            if s['type'] in ('text', 'kpis') and cut < 2: cut += 1
            else: break
        lead, after_lead = pre[:cut], pre[cut:]
        if priced:
            lead.append({'id': 'investment', 'type': 'investment', 'title': 'Investment summary', 'include': True})
        if co.get('about'):
            ab = {'id': 'about', 'type': 'text', 'title': 'About ' + (co.get('name') or 'us'), 'include': True, 'paragraphs': [co['about']]}
            lead.append(ab)
            if co.get('why_us'):
                lead.append({'id': 'why', 'type': 'list', 'title': 'Why ' + (co.get('name') or 'us'), 'include': True, 'intro': '',
                             'items': [{'text': x, 'include': True} for x in co['why_us'][:8]]})
        ordered = lead + after_lead + [sysec]
        # 2) design, scope
        braw = raw_b or {}
        for si, sec in enumerate((braw.get('design_sections') or [])[:10]):
            s = parse_section(sec, 100 + si)
            if s: ordered.append(s)
        if not any(s['type'] == 'scope' for s in ordered):
            ordered.append({'id': 'scope', 'type': 'scope', 'title': 'Scope of work', 'include': True, 'intro': '',
                            'excluded': [{'text': x, 'include': True} for x in (co.get('scope_client') or [])[:8]], 'note': ''})
        else:
            sc = next(s for s in ordered if s['type'] == 'scope')
            if not sc['excluded'] and co.get('scope_client'):
                sc['excluded'] = [{'text': x, 'include': True} for x in co['scope_client'][:8]]
        # 3) bill of quantities (positioned here; rendered by the client from the frozen BOQ)
        ordered.append({'id': 'boq', 'type': 'boq', 'title': 'Bill of quantities', 'include': True})
        # 4) delivery
        for si, sec in enumerate((braw.get('delivery_sections') or [])[:10]):
            s = parse_section(sec, 200 + si)
            if s: ordered.append(s)
        # 5) commercial terms: the quotation's own terms win; else the company's saved facts
        if facts['milestones']:
            ordered.append({'id': 'payment', 'type': 'payment', 'title': 'Payment schedule', 'include': True,
                            'milestones': [{'label': m['label'], 'pct': m['pct'], 'include': True} for m in facts['milestones']]})
        tl = []
        if terms:
            tl = list(terms)
        else:
            if p.get('quote_ref'): tl.append(f"Items and quantities are as per quotation {p['quote_ref']}.")
            for b in co.get('scope_client', []): tl.append(f'Client scope: {b}')
            if co.get('payment_terms'): tl.append(f"Payment terms: {co['payment_terms']}")
            if co.get('delivery_weeks'): tl.append(f"Delivery: {co['delivery_weeks']} weeks from order.")
            if co.get('warranty'): tl.append(f"Warranty: {co['warranty']}")
            if co.get('validity_days'): tl.append(f"Validity: {co['validity_days']} days from the date of the offer.")
        if tl:
            ordered.append({'id': 'terms', 'type': 'list', 'title': 'Terms and conditions', 'include': True,
                            'items': [{'text': t, 'include': True} for t in tl]})
        cv = raw.get('cover') or {}
        content = {'v': 3, 'cover': {'title': clip(cv.get('title') or p.get('title'), 140), 'subtitle': clip(cv.get('subtitle'), 200),
                                     'scope_line': clip(cv.get('scope_line'), 500)}, 'sections': ordered,
                   'facts': {k: facts[k] for k in ('delivery', 'warranty', 'validity', 'payment')}}
        if raw_b is None: content['warn'] = 'The design and delivery sections could not be generated this time. Click “Regenerate all content” to try again.'
        return content, images

    app.extensions['proposals_normalise'] = normalise  # exposed for tests

    # ── routes ────────────────────────────────────────────────────────────────
    @app.route('/api/proposals/<pid>/draft', methods=['GET'])
    def get_draft(pid):
        sb, claims = get_sb(), g.claims
        if not H['_load'](sb, claims, pid): return jsonify({'error': 'Not found'}), 404
        sweep(sb, pid)
        d = sb.table('proposal_drafts').select('*').eq('proposal_id', pid).execute().data
        imgs = sb.table('proposal_images').select('*').eq('proposal_id', pid).order('sort').execute().data or []
        draft = None
        if d:
            draft = {k: d[0].get(k) for k in ('status', 'instruction', 'content', 'rev', 'text_model', 'error', 'updated_at')}
        return jsonify({'draft': draft, 'images': [image_public(i) for i in imgs]})

    @app.route('/api/proposals/<pid>/draft/generate', methods=['POST'])
    def generate_draft(pid):
        sb, claims = get_sb(), g.claims
        p = H['_load'](sb, claims, pid)
        if not p: return jsonify({'error': 'Not found'}), 404
        if p['lifecycle'] != 'active': return jsonify({'error': 'Restore this proposal first.'}), 409
        s = H['_active_settings'](sb, claims['company_id'])
        st = H['_status'](s)
        if st['text'] != 'ready':
            return jsonify({'error': 'Proposal AI text model is not ready. An administrator must finish Settings → Proposal AI.', 'code': 'PROPOSAL_AI_NOT_CONFIGURED'}), 503
        sweep(sb, pid)
        cur = sb.table('proposal_drafts').select('id,status').eq('proposal_id', pid).execute().data
        if cur and cur[0]['status'] in ('queued', 'running'):
            return jsonify({'error': 'Content is already being generated.'}), 409
        instr = clip((request.json or {}).get('instruction'), 6000) or None
        try: min_pages = max(0, min(40, int(float((request.json or {}).get('min_pages') or 0))))
        except Exception: min_pages = 0
        doc_type = (request.json or {}).get('doc_type')
        if doc_type not in ('both', 'technical', 'commercial'): doc_type = 'both'
        row = {'company_id': claims['company_id'], 'proposal_id': pid, 'status': 'queued', 'instruction': instr, 'content': {'req': {'min_pages': min_pages, 'doc_type': doc_type}},
               'error': None, 'created_by': claims.get('user_id'), 'updated_at': now(), 'rev': 1}
        if cur:
            sb.table('proposal_drafts').update(row).eq('id', cur[0]['id']).execute()
        else:
            sb.table('proposal_drafts').insert(row).execute()
        try:
            sb.table('proposal_images').delete().eq('proposal_id', pid).neq('kind', 'site').execute()   # keep uploaded client site photos
        except Exception:   # site-photo migration not applied yet
            sb.table('proposal_images').delete().eq('proposal_id', pid).execute()
        return jsonify({'ok': True}), 201

    @app.route('/api/proposals/<pid>/draft/run', methods=['POST'])
    def run_draft(pid):
        sb, claims = get_sb(), g.claims
        p = H['_load'](sb, claims, pid)
        if not p: return jsonify({'error': 'Not found'}), 404
        claimed = sb.table('proposal_drafts').update({'status': 'running', 'started_at': now(), 'updated_at': now()})\
            .eq('proposal_id', pid).eq('status', 'queued').execute().data
        if not claimed:
            return jsonify({'ok': True, 'note': 'not queued'}), 202
        draft = claimed[0]

        def fail(msg):
            sb.table('proposal_drafts').update({'status': 'failed', 'error': msg[:500], 'updated_at': now()}).eq('id', draft['id']).execute()
            return jsonify({'ok': False, 'error': msg[:500]}), 200
        try:
            s = H['_active_settings'](sb, claims['company_id'])
            snap = latest_snapshot(sb, pid)
            if not s or not snap: return fail('Proposal AI settings or the source snapshot is missing.')
            lim = s.get('limits') or {}
            max_images = max(2, min(8, int(lim.get('max_images_per_package') or 4)))
            co = company_facts(sb, claims['company_id'])
            terms = list((snap['safe_source'] or {}).get('terms') or [])
            if not terms and p.get('quote_id'):  # older snapshots: read the quotation's own terms lines now
                try:
                    qr = sb.table('quotes').select('terms_data').eq('id', p['quote_id']).eq('company_id', claims['company_id']).execute().data
                    if qr: terms = H['_quote_terms'](qr[0])
                except Exception:
                    terms = []
            instr = draft.get('instruction')
            try: min_pages = max(0, min(40, int(((draft.get('content') or {}).get('req') or {}).get('min_pages') or 0)))
            except Exception: min_pages = 0
            doc_type = ((draft.get('content') or {}).get('req') or {}).get('doc_type')
            if doc_type not in ('both', 'technical', 'commercial'): doc_type = 'both'
            user_a = build_user_prompt(p, snap, instr, co, max_images, terms, 'A', min_pages, doc_type)
            user_b = build_user_prompt(p, snap, instr, co, max_images, terms, 'B', min_pages, doc_type)
            budget = max(8000, min(12000, int((min_pages - 4) * 330 * 0.6 * 1.6))) if min_pages > 12 else 8000
            t0 = time.time()

            def gen(system, user, max_tokens):
                last = None
                for prof in (s.get('text_profile'), s.get('text_fallback')):
                    if not prof: continue
                    left = 250 - (time.time() - t0)
                    if left < 40: break
                    try:
                        return H['_extract_json'](llm(prof, system, user, max_tokens, int(left))), prof, None
                    except (GenError, ValueError) as e:
                        last = str(e)
                return None, None, last or 'no time left'
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as ex:
                fa = ex.submit(gen, SYSTEM_A, user_a, budget)
                fb = ex.submit(gen, SYSTEM_B, user_b, budget) if doc_type != 'commercial' else None
                raw, used, err = fa.result()
                raw_b, _u, _e = fb.result() if fb else ({'design_sections': [], 'delivery_sections': []}, None, None)
            if raw is None: return fail(f'Content generation failed: {err}')
            content, images = normalise(raw, snap, co, p, max_images, terms, raw_b)
            if min_pages: content['min_pages'] = min_pages
            content['doc_type'] = doc_type
            if not any(sc['type'] == 'systems' and sc.get('groups') for sc in content['sections']): return fail('The model returned no usable content. Please try again.')
            for i, im in enumerate(images):
                sb.table('proposal_images').insert({
                    'company_id': claims['company_id'], 'proposal_id': pid, 'slot_key': im['key'], 'title': im['title'],
                    'caption': im['caption'], 'prompt': im['prompt'], 'model_refs': im['models'], 'status': 'pending', 'sort': i,
                }).execute()
            sb.table('proposal_drafts').update({'status': 'ready', 'content': content, 'rev': 1, 'text_provider': used['provider'],
                                                'text_model': used['model'], 'error': None, 'updated_at': now()}).eq('id', draft['id']).execute()
            sb.table('proposals').update({'stage': 'content_ready', 'updated_at': now()}).eq('id', pid).execute()
            return jsonify({'ok': True})
        except Exception as e:
            return fail(f'Unexpected error: {str(e)[:200]}')

    def clean_content(c):
        if not isinstance(c, dict) or c.get('v') not in (2, 3): raise ValueError('Invalid content.')
        if len(json.dumps(c, ensure_ascii=False)) > MAX_CONTENT_BYTES: raise ValueError('Content is too large.')

        def walk(x, depth=0):
            if depth > 6: raise ValueError('Invalid content.')
            if isinstance(x, str): return x[:4000]
            if isinstance(x, list): return [walk(i, depth + 1) for i in x[:80]]
            if isinstance(x, dict): return {str(k)[:40]: walk(v, depth + 1) for k, v in list(x.items())[:40]}
            return x if isinstance(x, (int, float, bool)) or x is None else str(x)[:200]
        return walk(c)

    @app.route('/api/proposals/<pid>/draft', methods=['PATCH'])
    def save_draft(pid):
        sb, claims = get_sb(), g.claims
        p = H['_load'](sb, claims, pid)
        if not p: return jsonify({'error': 'Not found'}), 404
        if p['lifecycle'] != 'active': return jsonify({'error': 'Restore this proposal first.'}), 409
        d = request.json or {}
        cur = sb.table('proposal_drafts').select('*').eq('proposal_id', pid).execute().data
        if not cur or cur[0]['status'] != 'ready': return jsonify({'error': 'No content to save yet.'}), 409
        cur = cur[0]
        if int(d.get('expected_rev', cur['rev'])) != cur['rev']:
            return jsonify({'error': 'The draft changed elsewhere. Reload and try again.', 'code': 'REVISION_CONFLICT'}), 409
        try:
            content = clean_content(d.get('content'))
        except ValueError as e:
            return jsonify({'error': str(e)}), 422
        r = sb.table('proposal_drafts').update({'content': content, 'rev': cur['rev'] + 1, 'updated_at': now()})\
            .eq('id', cur['id']).eq('rev', cur['rev']).execute().data
        if not r: return jsonify({'error': 'The draft changed elsewhere. Reload and try again.', 'code': 'REVISION_CONFLICT'}), 409
        return jsonify({'rev': r[0]['rev']})

    def load_image(sb, claims, pid, key):
        r = sb.table('proposal_images').select('*').eq('proposal_id', pid).eq('slot_key', key).eq('company_id', claims['company_id']).execute().data
        return r[0] if r else None

    # ── Reviewer feedback: revise one section (client applies the result, then autosaves) ──
    REVISE_SYSTEM = """You revise ONE section of a customer-facing proposal using a reviewer's feedback.
Return ONLY the revised section as one JSON object with EXACTLY the same structure as the input "section": same keys, same "id", same "type", same group "id"s, same number of groups and, for equipment sections, the same items in the same order with the same "line_ids". Change only the wording and the content of text fields (and add/remove paragraphs, bullet items, table rows or exclusions where the feedback asks for it).
- Apply "feedback_for_this_section" fully. Apply "overall_feedback" only where it is relevant to this section; if it does not concern this section, leave the section unchanged.
- Keep everything that the feedback does not ask you to change.
- Never invent products, quantities, specifications, certifications, prices, dates, delivery, warranty or company policies. Only use facts present in the "boq", "quote_terms", "company_facts" or the reviewer's feedback itself (treat facts the reviewer states as true).
- Never mention cost, margin, discounts or prices. Plain professional English, no marketing hype.
- Output ONLY the JSON object, no markdown fences, no commentary."""

    NOREV = ('boq', 'investment', 'payment', 'diagram')

    def clean_revised(orig, new):
        import copy
        if not isinstance(new, dict): raise ValueError('The model did not return a section.')
        out = copy.deepcopy(orig); ty = orig.get('type')
        if ty == 'cover':
            for k, n in (('title', 140), ('subtitle', 200), ('scope_line', 500)):
                if clip(new.get(k), 1): out[k] = clip(new.get(k), n)
            return out
        if clip(new.get('title'), 1): out['title'] = clip(new.get('title'), 100)

        def items(lst, n, fields):
            res = []
            for x in (lst or [])[:n]:
                if isinstance(x, dict):
                    d = {k: clip(x.get(k), lim) for k, lim in fields.items()}
                    if any(d.values()): d['include'] = True; res.append(d)
            return res
        if ty == 'text':
            ps = [clip(x, 2000) for x in (new.get('paragraphs') or [])[:10] if clip(x, 1)]
            if ps: out['paragraphs'] = ps
        elif ty == 'kpis':
            its = [i for i in items(new.get('items'), 8, {'value': 24, 'label': 60, 'sub': 80}) if i['value']]
            if its: out['items'] = its
        elif ty in ('columns', 'points'):
            its = items(new.get('items'), 12, {'title': 100, 'body': 700})
            if its: out['items'] = its
            if 'intro' in new: out['intro'] = clip(new.get('intro'), 900)
        elif ty == 'list':
            its = []
            for x in (new.get('items') or [])[:14]:
                tx = clip(x.get('text') if isinstance(x, dict) else x, 400)
                if tx: its.append({'text': tx, 'include': True})
            if its: out['items'] = its
            if 'intro' in new: out['intro'] = clip(new.get('intro'), 700)
        elif ty == 'table':
            ncol = len(orig.get('columns') or [])
            rows = []
            for r in (new.get('rows') or [])[:14]:
                cells = r.get('cells') if isinstance(r, dict) else r
                if isinstance(cells, list):
                    cells = [clip(c, 400) for c in cells[:ncol]]; cells += [''] * (ncol - len(cells))
                    if any(cells): rows.append({'cells': cells, 'include': True})
            if rows: out['rows'] = rows
            if 'intro' in new: out['intro'] = clip(new.get('intro'), 700)
        elif ty == 'scope':
            ex = []
            for x in (new.get('excluded') or [])[:10]:
                tx = clip(x.get('text') if isinstance(x, dict) else x, 300)
                if tx: ex.append({'text': tx, 'include': True})
            out['excluded'] = ex
            if 'intro' in new: out['intro'] = clip(new.get('intro'), 600)
            if 'note' in new: out['note'] = clip(new.get('note'), 500)
        elif ty == 'systems':
            if 'intro' in new: out['intro'] = clip(new.get('intro'), 900)
            ng = {clip(g_.get('id'), 40): g_ for g_ in (new.get('groups') or []) if isinstance(g_, dict)}
            for og in out.get('groups') or []:
                g_ = ng.get(og.get('id'))
                if not g_: continue
                if clip(g_.get('title'), 1): og['title'] = clip(g_.get('title'), 100)
                if 'intro' in g_: og['intro'] = clip(g_.get('intro'), 900)
                nit = g_.get('items') or []
                for k, oi in enumerate(og.get('items') or []):
                    if k < len(nit) and isinstance(nit[k], dict) and clip(nit[k].get('does'), 1):
                        oi['does'] = clip(nit[k].get('does'), 700)
        return out

    app.extensions['proposals_clean_revised'] = clean_revised  # exposed for tests

    @app.route('/api/proposals/<pid>/draft/revise-section', methods=['POST'])
    def revise_section(pid):
        sb, claims = get_sb(), g.claims
        p = H['_load'](sb, claims, pid)
        if not p: return jsonify({'error': 'Not found'}), 404
        if p['lifecycle'] != 'active': return jsonify({'error': 'Restore this proposal first.'}), 409
        s = H['_active_settings'](sb, claims['company_id'])
        if H['_status'](s)['text'] != 'ready':
            return jsonify({'error': 'Proposal AI text model is not ready. An administrator must finish Settings → Proposal AI.', 'code': 'PROPOSAL_AI_NOT_CONFIGURED'}), 503
        d = request.json or {}
        sec = d.get('section')
        fb, gfb = clip(d.get('feedback'), 3000), clip(d.get('global_feedback'), 3000)
        if not isinstance(sec, dict) or not (fb or gfb): return jsonify({'error': 'Write some feedback first.'}), 422
        if sec.get('type') in NOREV: return jsonify({'error': 'This section is generated automatically and cannot be revised.'}), 422
        if len(json.dumps(sec, ensure_ascii=False)) > 60000: return jsonify({'error': 'Section is too large.'}), 422
        snap = latest_snapshot(sb, pid)
        lines = []
        for i, l in enumerate(((snap or {}).get('safe_source') or {}).get('lines') or [], 1):
            lines.append({'line_id': l.get('source_line_id'), 'boq_no': i, 'brand': l.get('manufacturer'), 'model': l.get('model'),
                          'description': clip(l.get('description'), 250), 'qty': l.get('quantity'), 'unit': l.get('unit')})
        co = company_facts(sb, claims['company_id'])
        terms = list(((snap or {}).get('safe_source') or {}).get('terms') or [])
        user = 'Revise this proposal section. Input (JSON):\n' + json.dumps({
            'customer': p.get('customer_name'), 'project': p.get('title'), 'feedback_for_this_section': fb or '(none)',
            'overall_feedback': gfb or '(none)', 'quote_terms': terms, 'company_facts': co, 'boq': lines[:120], 'section': sec}, ensure_ascii=False)
        last = None
        for prof in (s.get('text_profile'), s.get('text_fallback')):
            if not prof: continue
            try:
                raw = H['_extract_json'](llm(prof, REVISE_SYSTEM, user, 6000, 150))
                return jsonify({'section': clean_revised(sec, raw)})
            except (GenError, ValueError) as e:
                last = str(e)
        return jsonify({'error': 'Revision failed: ' + (last or 'unknown error')[:200]}), 502

    @app.route('/api/proposals/<pid>/draft/images/<key>', methods=['PATCH'])
    def patch_image(pid, key):
        sb, claims = get_sb(), g.claims
        if not H['_load'](sb, claims, pid): return jsonify({'error': 'Not found'}), 404
        im = load_image(sb, claims, pid, key)
        if not im: return jsonify({'error': 'Not found'}), 404
        d = request.json or {}
        upd = {'updated_at': now()}
        if 'title' in d: upd['title'] = clip(d['title'], 120)
        if 'caption' in d: upd['caption'] = clip(d['caption'], 300)
        if 'prompt' in d: upd['prompt'] = clip(d['prompt'], 1500)
        if 'include' in d: upd['include'] = bool(d['include'])
        r = sb.table('proposal_images').update(upd).eq('id', im['id']).execute().data
        return jsonify({'image': image_public(r[0])})

    @app.route('/api/proposals/<pid>/draft/site-photo', methods=['POST'])
    def upload_site_photo(pid):
        """A photograph of the client's actual space. It becomes an image slot that re-renders the photo with the proposed equipment in it."""
        sb, claims = get_sb(), g.claims
        p = H['_load'](sb, claims, pid)
        if not p: return jsonify({'error': 'Not found'}), 404
        if p['lifecycle'] != 'active': return jsonify({'error': 'Restore this proposal first.'}), 409
        d = request.json or {}
        ct = str(d.get('content_type') or '').lower()
        if ct not in ('image/jpeg', 'image/png', 'image/webp'): return jsonify({'error': 'Please upload a JPG, PNG or WebP photo.'}), 422
        try: raw = base64.b64decode(d.get('image_b64') or '', validate=False)
        except Exception: return jsonify({'error': 'The photo could not be read.'}), 422
        if not raw or len(raw) > 3_500_000: return jsonify({'error': 'The photo is too large (limit about 3.5 MB). It is normally reduced automatically; try a smaller photo.'}), 422
        if not (raw[:3] == b'\xff\xd8\xff' or raw[:8] == b'\x89PNG\r\n\x1a\n' or raw[:4] == b'RIFF'): return jsonify({'error': 'That file is not a valid image.'}), 422
        snap = latest_snapshot(sb, pid)
        lines = ((snap or {}).get('safe_source') or {}).get('lines') or []
        co = company_facts(sb, claims['company_id'])
        own = (co.get('name') or '').strip().lower()
        vis = [l for l in lines if (l.get('model') or '').strip().lower() != 'service' and (l.get('manufacturer') or '').strip().lower() != own]
        models, desc = [], []
        for l in vis[:12]:
            m = (l.get('model') or '').strip()
            if m and m not in models: models.append(m)
            desc.append(f"{l.get('quantity') or 1} x {clip(((l.get('manufacturer') or '') + ' ' + (l.get('model') or '')).strip(), 60)}"
                        + (f" ({clip(l.get('description'), 80)})" if l.get('description') else ''))
        existing = sb.table('proposal_images').select('slot_key,sort').eq('proposal_id', pid).execute().data or []
        n = 1 + sum(1 for e in existing if str(e.get('slot_key', '')).startswith('site-'))
        while any(e.get('slot_key') == f'site-{n}' for e in existing): n += 1
        key = f'site-{n}'
        ext = {'image/png': 'png', 'image/webp': 'webp'}.get(ct, 'jpg')
        path = f"{claims['company_id']}/proposals/{pid}/site-{n}-{int(datetime.utcnow().timestamp())}.{ext}"
        try:
            sb.storage.from_(H['PREVIEW_BUCKET']).upload(path, raw, {'content-type': ct})
        except Exception:
            return jsonify({'error': 'The photo could not be saved.'}), 500
        prompt = ('Show the proposed equipment installed in this space, in sensible positions and to scale: ' + '; '.join(desc[:10]) + '.') if desc else 'Show the proposed equipment installed in this space.'
        row = sb.table('proposal_images').insert({
            'company_id': claims['company_id'], 'proposal_id': pid, 'slot_key': key, 'title': clip(d.get('title'), 120) or f'Proposed installation at site ({n})',
            'caption': 'Proposed equipment shown in the client\'s space (illustrative).', 'prompt': prompt, 'model_refs': models[:6], 'status': 'pending',
            'sort': 100 + n, 'kind': 'site', 'source_path': path, 'include': True,
        }).execute().data[0]
        return jsonify({'image': image_public(row)}), 201

    @app.route('/api/proposals/<pid>/draft/images/<key>', methods=['DELETE'])
    def delete_image(pid, key):
        sb, claims = get_sb(), g.claims
        if not H['_load'](sb, claims, pid): return jsonify({'error': 'Not found'}), 404
        im = load_image(sb, claims, pid, key)
        if not im: return jsonify({'error': 'Not found'}), 404
        if im.get('kind') != 'site': return jsonify({'error': 'Only uploaded site photos can be deleted. Untick Include to drop other images.'}), 409
        for pth in (im.get('source_path'), im.get('image_path')):
            if pth:
                try: sb.storage.from_(H['PREVIEW_BUCKET']).remove([pth])
                except Exception: pass
        sb.table('proposal_images').delete().eq('id', im['id']).execute()
        return jsonify({'ok': True})

    @app.route('/api/proposals/<pid>/draft/images/<key>/queue', methods=['POST'])
    def queue_image(pid, key):
        sb, claims = get_sb(), g.claims
        p = H['_load'](sb, claims, pid)
        if not p: return jsonify({'error': 'Not found'}), 404
        if p['lifecycle'] != 'active': return jsonify({'error': 'Restore this proposal first.'}), 409
        s = H['_active_settings'](sb, claims['company_id'])
        if H['_status'](s)['image'] != 'ready':
            return jsonify({'error': 'Proposal AI image model is not ready. An administrator must finish Settings → Proposal AI.', 'code': 'PROPOSAL_AI_NOT_CONFIGURED'}), 503
        im = load_image(sb, claims, pid, key)
        if not im: return jsonify({'error': 'Not found'}), 404
        if im['status'] in ('queued', 'running'): return jsonify({'error': 'This image is already generating.'}), 409
        sb.table('proposal_images').update({'status': 'queued', 'error': None, 'updated_at': now()}).eq('id', im['id']).execute()
        return jsonify({'ok': True})

    @app.route('/api/proposals/<pid>/draft/images/<key>/reset', methods=['POST'])
    def reset_image(pid, key):
        sb, claims = get_sb(), g.claims
        if not H['_load'](sb, claims, pid): return jsonify({'error': 'Not found'}), 404
        im = load_image(sb, claims, pid, key)
        if not im: return jsonify({'error': 'Not found'}), 404
        if im['status'] in ('queued', 'running'):
            sb.table('proposal_images').update({'status': 'failed', 'error': 'Cancelled.', 'updated_at': now()}).eq('id', im['id']).execute()
        return jsonify({'ok': True})

    @app.route('/api/proposals/<pid>/draft/images/<key>/run', methods=['POST'])
    def run_image(pid, key):
        sb, claims = get_sb(), g.claims
        if not H['_load'](sb, claims, pid): return jsonify({'error': 'Not found'}), 404
        claimed = sb.table('proposal_images').update({'status': 'running', 'started_at': now(), 'updated_at': now()})\
            .eq('proposal_id', pid).eq('slot_key', key).eq('company_id', claims['company_id']).eq('status', 'queued').execute().data
        if not claimed: return jsonify({'ok': True, 'note': 'not queued'}), 202
        im = claimed[0]

        def fail(msg):
            sb.table('proposal_images').update({'status': 'failed', 'error': msg[:400], 'updated_at': now()}).eq('id', im['id']).execute()
            return jsonify({'ok': False, 'error': msg[:400]}), 200
        try:
            s = H['_active_settings'](sb, claims['company_id'])
            if not s: return fail('Proposal AI is not configured.')
            style = (' Photorealistic professional architectural photography, natural balanced lighting, clean modern interior. '
                     'Any screens show blank or abstract gradients. No readable text, no logos, no brand names, no people.')
            prompt = (im.get('prompt') or im.get('title') or 'Meeting room') + style
            t0 = time.time()
            left = lambda: 275 - (time.time() - t0)   # Vercel limit is 300 s; keep a margin for upload + DB writes
            refs = H['_refs_for_models'](sb, claims['company_id'], im.get('model_refs') or [], 3 if im.get('kind') == 'site' else 4)
            site = im.get('kind') == 'site' and im.get('source_path')
            if site:
                try:
                    sb_bytes = sb.storage.from_(H['PREVIEW_BUCKET']).download(im['source_path'])
                except Exception:
                    return fail('The uploaded site photo could not be read. Please upload it again.')
                ext = im['source_path'].rsplit('.', 1)[-1].lower()
                refs = [({'png': 'image/png', 'webp': 'image/webp'}.get(ext, 'image/jpeg'), sb_bytes)] + refs
                prompt = ('The FIRST attached image is a photograph of the client\'s real space. Edit that photograph: keep the room exactly as photographed '
                          '(walls, floor, ceiling, windows, existing furniture, lighting and camera angle) and add ONLY the proposed equipment described below, '
                          'installed naturally and to scale in sensible positions. Any further attached images are product reference photos for the matching '
                          'equipment (shape, colour, proportions). Photorealistic. Screens show blank or abstract content. No readable text, logos or brand names. '
                          + (im.get('prompt') or 'Show the proposed equipment installed in this space.'))
            img, used, err = None, None, None
            for prof in (s.get('image_profile'), s.get('image_fallback')):
                if not prof or not prof.get('model'): continue
                if left() < 40: err = err or 'ran out of time'; break
                if site and prof['provider'] != 'openai':
                    err = 'Showing your site photo with the proposed equipment needs the OpenAI image model (gpt-image).'; continue
                try:
                    if refs and prof['provider'] == 'openai':
                        try:
                            img = H['_call_image_edit_openai'](prof, prompt if site else ('Use the attached product photos as the exact visual reference for the matching '
                                                               'equipment (shape, colour, proportions). ' + prompt), refs, min(100 if not site else 150, left() - 20))
                        except GenError as e:
                            if site: raise
                            img = None
                    if img is None and not site:
                        img = H['_call_image'](prof, prompt, max(30, min(150, left() - 15)))
                    used = prof; break
                except GenError as e:
                    err = str(e)
            if not img: return fail(f'Image failed: {err}')
            path = f"{claims['company_id']}/proposals/{pid}/img-{key}-{int(datetime.utcnow().timestamp())}.png"
            try:
                sb.storage.from_(H['PREVIEW_BUCKET']).upload(path, img, {'content-type': 'image/png'})
            except Exception:
                return fail('The image was generated but could not be saved to storage.')
            r = sb.table('proposal_images').update({'status': 'ready', 'image_path': path, 'image_provider': used['provider'],
                                                    'image_model': used['model'], 'error': None, 'updated_at': now()}).eq('id', im['id']).execute().data
            return jsonify({'ok': True, 'image': image_public(r[0])})
        except Exception as e:
            return fail(f'Unexpected error: {str(e)[:200]}')


    # ── Upload a BOQ (PDF / Word / Excel text) instead of picking a quotation ──
    EXTRACT_SYSTEM = """You extract a bill of quantities (BOQ) from a document. Return ONLY one JSON object, no markdown:
{"customer": str, "project": str, "currency": str, "truncated": bool,
 "lines": [{"section": str, "manufacturer": str, "model": str, "description": str, "quantity": number, "unit": str, "unit_price": number or null, "total": number or null}]}
Rules: copy only what the document says; never invent or guess items, quantities, models or prices. One entry per BOQ line item. Skip sub-totals, totals, VAT and notes rows. "section" is the heading/group the line sits under (empty if none). Keep the original model/part number text. If a quantity is missing use 1. If a unit is missing use "Pcs". Put numbers as plain numbers (no currency symbols or thousands separators). customer/project/currency only if clearly stated, else "". If there are more than 150 line items, return the first 150 and set truncated to true."""

    def docx_text(b):
        import io, docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        d = docx.Document(io.BytesIO(b))
        out = []
        for el in d.element.body.iterchildren():
            if el.tag.endswith('}p'):
                t = Paragraph(el, d).text.strip()
                if t: out.append(t)
            elif el.tag.endswith('}tbl'):
                for row in Table(el, d).rows:
                    cells, prev = [], None
                    for c in row.cells:
                        if c._tc is prev: continue
                        prev = c._tc
                        cells.append(c.text.strip().replace('\n', ' / '))
                    if any(cells): out.append(' | '.join(cells))
        return '\n'.join(out)

    def pdf_text(b):
        import io
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(b))
        return '\n'.join((pg.extract_text() or '') for pg in r.pages[:60])

    def num(x):
        try:
            if x is None or x == '': return None
            return float(str(x).replace(',', '').strip())
        except Exception:
            return None

    @app.route('/api/proposals/extract-boq', methods=['POST'])
    def extract_boq():
        sb, claims = get_sb(), g.claims
        s = H['_active_settings'](sb, claims['company_id'])
        if H['_status'](s)['text'] != 'ready':
            return jsonify({'error': 'Proposal AI text model is not ready. An administrator must finish Settings → Proposal AI.', 'code': 'PROPOSAL_AI_NOT_CONFIGURED'}), 503
        d = request.json or {}
        fname = clip(d.get('filename'), 150).lower()
        ext = fname.rsplit('.', 1)[-1] if '.' in fname else ''
        prof = s.get('text_profile')
        text, pdf_b64 = None, None
        try:
            if d.get('text'):                                   # Excel/CSV already converted to text in the browser
                text = clip(d['text'], 250_000)
            else:
                raw = base64.b64decode(d.get('data') or '', validate=False)
                if not raw: return jsonify({'error': 'The file is empty.'}), 400
                if len(raw) > 3_000_000: return jsonify({'error': 'The file is too large. Please use a file under 3 MB.'}), 413
                if ext == 'docx':
                    text = docx_text(raw)
                elif ext == 'pdf':
                    if prof['provider'] == 'anthropic':
                        pdf_b64 = base64.b64encode(raw).decode()
                    else:
                        text = pdf_text(raw)
                elif ext in ('txt', 'csv'):
                    text = raw.decode('utf-8', 'replace')[:250_000]
                else:
                    return jsonify({'error': 'Unsupported file type. Use PDF, Word (.docx), Excel or CSV.'}), 400
        except Exception as e:
            return jsonify({'error': 'Could not read the file: ' + str(e)[:150]}), 400
        if text is not None and len(text.strip()) < 20:
            return jsonify({'error': 'No readable text was found in the file. If it is a scanned PDF, use the Anthropic text model in Proposal AI settings.'}), 422
        try:
            secret = os.environ.get(prof['credential_ref'], '')
            if not secret: raise GenError(f"server variable {prof['credential_ref']} is not set")
            if pdf_b64:
                r = H['_post_json']('https://api.anthropic.com/v1/messages', {'x-api-key': secret, 'anthropic-version': '2023-06-01'},
                    {'model': prof['model'], 'max_tokens': 9000, 'system': EXTRACT_SYSTEM,
                     'messages': [{'role': 'user', 'content': [
                         {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': pdf_b64}},
                         {'type': 'text', 'text': 'Extract the BOQ from this document.'}]}]}, 200)
                out = ''.join(b.get('text', '') for b in (r.get('content') or []) if b.get('type') == 'text')
            else:
                out = llm(prof, EXTRACT_SYSTEM, 'Extract the BOQ from this document text:\n\n' + text, 9000, 200)
            raw_j = H['_extract_json'](out)
        except (GenError, ValueError) as e:
            return jsonify({'error': 'Could not read the BOQ: ' + str(e)[:200]}), 502
        lines = []
        for l in (raw_j.get('lines') or [])[:150]:
            if not isinstance(l, dict): continue
            q = num(l.get('quantity')) or 1
            row = {'section': clip(l.get('section'), 100), 'manufacturer': clip(l.get('manufacturer'), 80), 'model': clip(l.get('model'), 120),
                   'description': clip(l.get('description'), 800), 'quantity': q, 'unit': clip(l.get('unit') or 'Pcs', 20),
                   'unit_price': num(l.get('unit_price')), 'total': num(l.get('total'))}
            if row['manufacturer'] or row['model'] or row['description']: lines.append(row)
        if not lines: return jsonify({'error': 'No BOQ line items were found in this file.'}), 422
        return jsonify({'lines': lines, 'customer': clip(raw_j.get('customer'), 120), 'project': clip(raw_j.get('project'), 160),
                        'currency': clip(raw_j.get('currency'), 8), 'truncated': bool(raw_j.get('truncated'))})

    def filter_doc(c, mode):
        """Sections that belong to the chosen document type. Technical: no price/terms sections.
        Commercial: opening sections, scope, bill of quantities and the commercial sections only."""
        if mode == 'both': return c
        secs = c.get('sections') or []
        si = next((i for i, s in enumerate(secs) if s.get('type') == 'systems'), -1)
        comm = lambda s: s.get('type') in ('investment', 'payment') or s.get('id') == 'terms'
        if mode == 'technical': keep = [s for s in secs if not comm(s)]
        else: keep = [s for i, s in enumerate(secs) if comm(s) or s.get('type') in ('boq', 'scope') or si < 0 or i < si]
        out = dict(c, sections=keep)
        if mode == 'technical': out['facts'] = {}
        return out

    @app.route('/api/proposals/<pid>/draft/confirm', methods=['POST'])
    def confirm_draft(pid):
        """Step 3 -> 4: freeze the edited content + chosen images into an immutable document version."""
        sb, claims = get_sb(), g.claims
        p = H['_load'](sb, claims, pid)
        if not p: return jsonify({'error': 'Not found'}), 404
        if p['lifecycle'] != 'active': return jsonify({'error': 'Restore this proposal first.'}), 409
        dr = sb.table('proposal_drafts').select('*').eq('proposal_id', pid).execute().data
        if not dr or dr[0]['status'] != 'ready' or not dr[0].get('content'): return jsonify({'error': 'Generate the content first.'}), 422
        imgs = sb.table('proposal_images').select('*').eq('proposal_id', pid).order('sort').execute().data or []
        pending = [i['slot_key'] for i in imgs if i['include'] and i['status'] != 'ready']
        if pending:
            return jsonify({'error': 'These images are not generated yet: ' + ', '.join(pending) + '. Generate them or remove them from the proposal.'}), 422
        snap = latest_snapshot(sb, pid)
        if not snap: return jsonify({'error': 'Source snapshot missing.'}), 422
        d = request.json or {}
        opts_in = d.get('options') or {}
        can_price = H['_can_commercial'](claims) and snap['visibility'] == 'commercial'
        doc_type = opts_in.get('doc_type')
        if doc_type not in ('both', 'technical', 'commercial'): doc_type = 'both' if opts_in.get('show_prices') else 'technical'
        if doc_type == 'commercial' and not can_price: return jsonify({'error': 'Prices are not available for this proposal, so a commercial document cannot be created. Choose technical only.'}), 422
        if doc_type == 'both' and not can_price: doc_type = 'technical'
        show_prices = doc_type != 'technical' and can_price
        options = {'show_prices': show_prices, 'doc_type': doc_type, 'cover_note': clip(opts_in.get('cover_note'), 1500)}
        ss = snap['safe_source']
        lines = ss.get('lines') or []
        if not show_prices:
            lines = [{k: v for k, v in l.items() if k not in ('unit_selling_price', 'line_total')} for l in lines]
        content = {
            'v': (dr[0]['content'].get('v') or 2), 'reference': p['reference'], 'title': p.get('title') or '', 'customer': ss.get('customer') or p.get('customer_name') or '',
            'quote_ref': p.get('quote_ref') or '', 'project': ss.get('project') or '', 'date': datetime.utcnow().strftime('%Y-%m-%d'),
            'prepared_by': clip(d.get('prepared_by'), 120), 'doc': filter_doc(dr[0]['content'], doc_type), 'lines': lines,
            'images': {i['slot_key']: {'path': i['image_path'], 'title': i['title'] or '', 'caption': i['caption'] or '',
                                       'kind': i.get('kind') or 'generated', 'source_path': i.get('source_path') or None}
                       for i in imgs if i['include'] and i['status'] == 'ready'},
        }
        if show_prices and ss.get('commercials'): content['commercials'] = ss['commercials']
        last = sb.table('proposal_documents').select('version').eq('proposal_id', pid).order('version', desc=True).limit(1).execute().data
        ver = (last[0]['version'] if last else 0) + 1
        row = sb.table('proposal_documents').insert({
            'company_id': claims['company_id'], 'proposal_id': pid, 'version': ver, 'preview_id': dr[0]['id'], 'snapshot_id': snap['id'],
            'source_hash': snap.get('source_hash'), 'options': options, 'content': content, 'status': 'draft', 'created_by': claims.get('user_id'),
        }).execute().data[0]
        sb.table('proposals').update({'current_document_id': row['id'], 'stage': 'document_draft', 'updated_at': now()}).eq('id', pid).execute()
        return jsonify({'document': H['_doc_public'](row)}), 201
