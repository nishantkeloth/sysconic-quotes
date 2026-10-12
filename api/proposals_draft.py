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
import os, re, json, base64
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
        sb.table('proposal_images').update({'status': 'failed', 'error': 'Image generation timed out. Please try again.'})\
            .eq('proposal_id', pid).in_('status', ['queued', 'running']).lt('updated_at', cutoff).execute()

    def latest_snapshot(sb, pid):
        r = sb.table('proposal_source_snapshots').select('*').eq('proposal_id', pid).order('created_at', desc=True).limit(1).execute().data
        return r[0] if r else None

    def image_public(row):
        out = {k: row.get(k) for k in ('slot_key', 'title', 'caption', 'prompt', 'model_refs', 'status', 'include', 'sort',
                                         'image_model', 'error', 'updated_at')}
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

    CONTENT_SYSTEM = """You are a senior pre-sales engineer at an audio-visual systems integrator writing a customer-facing technical proposal narrative from a bill of quantities (BOQ).

THE STRUCTURE IS YOURS TO DESIGN. Do not follow a template. Plan the sections that suit THIS project, and scale the depth to the BOQ: a handful of lines needs a short proposal (about 3-5 sections); a large multi-room system can have up to 12. Name sections to fit the project (not stock headings). Leave out anything that would be filler. Vary wording; never reuse boilerplate.

Section types you may use (each section: {"type", "title", ...}):
- "text":    {"paragraphs": [str]}                                   narrative (e.g. summary, understanding, how it works)
- "kpis":    {"items": [{"value": str, "label": str, "sub": str}]}   headline figures, ONLY if they genuinely help; derived from BOQ quantities/descriptions
- "columns": {"intro": str, "items": [{"title": str, "body": str}]} 2-4 side-by-side ideas (use only if it fits)
- "points":  {"intro": str, "items": [{"title": str, "body": str}]} numbered points (e.g. points to confirm / assumptions); ONLY real gaps or assumptions, zero is fine
- "systems": {"intro": str, "groups": [{"id": str, "title": str, "intro": str, "items": [{"line_ids": [str], "does": str}]}]}  the equipment narrative: EXACTLY ONE section of this type, covering EVERY BOQ line in exactly one group item
Put the sections in the order that reads best for this customer.

STRICT RULES
- Use ONLY equipment, quantities and descriptions in the BOQ. Never invent products, quantities, sizes, performance figures, certifications, standards, dates, prices or company policies. You may explain in general terms what a product CLASS does but add no numeric specification that is not in the BOQ description.
- Never mention cost, margin, discounts or prices.
- Refer to BOQ lines only by line_id; quantities are added by the system.
- Anything uncertain or site-dependent belongs in a "points" section, not stated as fact.
- Use the customer brief/guidance if given as the source for requirements and context; without one, write from the BOQ only and do not invent a site description.
- Use company facts only as given.
- Plain professional English, no marketing hype. Concise but specific.

OUTPUT: ONLY one JSON object, no markdown fences:
{
 "cover": {"title": str, "subtitle": str, "scope_line": str (1-2 sentences)},
 "sections": [ ... as above ... ],
 "images": [{"key": str, "title": str, "caption": str, "system_id": str (a group id, or "" for the main image), "line_ids": [str],
             "prompt": str (<=130 words: photorealistic architectural visualisation showing ONLY the equipment in line_ids, plausible positions, exact counts; screens blank/abstract; no readable text, logos or faces)}]
}
The first image must have key "hero" (main space with the principal equipment). Add further images only where they help, up to the maximum given."""

    def build_user_prompt(p, snap, instr, co, max_images):
        lines = []
        for i, l in enumerate((snap['safe_source'].get('lines') or []), 1):
            lines.append({'line_id': l.get('source_line_id'), 'boq_no': i, 'section': l.get('section'),
                          'brand': l.get('manufacturer'), 'model': l.get('model'), 'description': clip(l.get('description'), 400),
                          'qty': l.get('quantity'), 'unit': l.get('unit')})
        brief = {'customer': p.get('customer_name'), 'project': p.get('title'), 'quotation_ref': p.get('quote_ref'),
                 'customer_brief_and_guidance': ' '.join(x for x in (p.get('instruction'), instr) if x) or '(none)',
                 'max_images': max_images, 'company_facts': co, 'boq': lines[:120]}
        return 'Write the full proposal content for this project. Input (JSON):\n' + json.dumps(brief, ensure_ascii=False)

    def normalise(raw, snap, co, p, max_images):
        """Model output -> editable document model (v3: a free-form list of sections).
        Every BOQ reference is validated; quantities/models come from the BOQ, never the model."""
        lines = snap['safe_source'].get('lines') or []
        by_id = {l.get('source_line_id'): (i + 1, l) for i, l in enumerate(lines)}
        used, seen_ids = set(), set()

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

        groups_all, sections, systems_done = [], [], False
        for si, sec in enumerate((raw.get('sections') or [])[:14]):
            if not isinstance(sec, dict): continue
            t = sec.get('type')
            title = clip(sec.get('title'), 100)
            base = {'id': slug(title or t, f'sec-{si+1}'), 'type': t, 'title': title, 'include': True}
            if t == 'text':
                paras = [clip(x, 1800) for x in (sec.get('paragraphs') or [])[:6] if clip(x, 1)]
                if paras: sections.append({**base, 'paragraphs': paras})
            elif t == 'kpis':
                its = items_of(sec.get('items'), 8, {'value': 24, 'label': 60, 'sub': 80})
                its = [i for i in its if i['value']]
                if its: sections.append({**base, 'items': its})
            elif t == 'columns':
                its = items_of(sec.get('items'), 6, {'title': 60, 'body': 400})
                if its: sections.append({**base, 'intro': clip(sec.get('intro'), 900), 'items': its})
            elif t == 'points':
                its = items_of(sec.get('items'), 12, {'title': 100, 'body': 700})
                if its: sections.append({**base, 'intro': clip(sec.get('intro'), 700), 'items': its})
            elif t == 'systems' and not systems_done:
                groups = []
                for gi, gr in enumerate((sec.get('groups') or [])[:10]):
                    if not isinstance(gr, dict): continue
                    items = []
                    for it in (gr.get('items') or []):
                        if not isinstance(it, dict): continue
                        ids = [x for x in (it.get('line_ids') or []) if x in by_id and x not in used]
                        if not ids: continue
                        for x in ids: used.add(x)
                        items.append({'line_ids': ids, 'does': clip(it.get('does'), 500), 'include': True})
                    if items:
                        groups.append({'id': slug(gr.get('id') or gr.get('title'), f'grp-{gi+1}'), 'title': clip(gr.get('title'), 100),
                                       'intro': clip(gr.get('intro'), 900), 'items': items, 'image': '', 'include': True})
                systems_done = True
                sections.append({**base, 'type': 'systems', 'intro': clip(sec.get('intro'), 900), 'groups': groups})
        rest = [l.get('source_line_id') for l in lines if l.get('source_line_id') not in used]
        sysec = next((s for s in sections if s['type'] == 'systems'), None)
        if sysec is None:
            sysec = {'id': slug('systems', 'systems'), 'type': 'systems', 'title': 'The systems', 'include': True, 'intro': '', 'groups': []}
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
        # terms: ONLY from the company's own saved facts (no stock sentences)
        terms = []
        if p.get('quote_ref'): terms.append(f"Items and quantities are as per quotation {p['quote_ref']}.")
        for b in co.get('scope_client', []): terms.append(f'Client scope: {b}')
        if co.get('payment_terms'): terms.append(f"Payment terms: {co['payment_terms']}")
        if co.get('delivery_weeks'): terms.append(f"Delivery: {co['delivery_weeks']} weeks from order.")
        if co.get('warranty'): terms.append(f"Warranty: {co['warranty']}")
        if co.get('validity_days'): terms.append(f"Validity: {co['validity_days']} days from the date of the offer.")
        if terms:
            sections.append({'id': 'terms', 'type': 'list', 'title': 'Terms and conditions', 'include': True,
                             'items': [{'text': t, 'include': True} for t in terms]})
        cv = raw.get('cover') or {}
        content = {'v': 3, 'cover': {'title': clip(cv.get('title') or p.get('title'), 140), 'subtitle': clip(cv.get('subtitle'), 200),
                                     'scope_line': clip(cv.get('scope_line'), 500)}, 'sections': sections}
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
        row = {'company_id': claims['company_id'], 'proposal_id': pid, 'status': 'queued', 'instruction': instr, 'content': None,
               'error': None, 'created_by': claims.get('user_id'), 'updated_at': now(), 'rev': 1}
        if cur:
            sb.table('proposal_drafts').update(row).eq('id', cur[0]['id']).execute()
        else:
            sb.table('proposal_drafts').insert(row).execute()
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
            user = build_user_prompt(p, snap, draft.get('instruction'), co, max_images)
            raw, used, err = None, None, None
            for prof in (s.get('text_profile'), s.get('text_fallback')):
                if not prof: continue
                try:
                    txt = llm(prof, CONTENT_SYSTEM, user, 9000, 200)
                    raw = H['_extract_json'](txt); used = prof; break
                except (GenError, ValueError) as e:
                    err = str(e)
            if raw is None: return fail(f'Content generation failed: {err}')
            content, images = normalise(raw, snap, co, p, max_images)
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
            refs = H['_refs_for_models'](sb, claims['company_id'], im.get('model_refs') or [])
            img, used, err = None, None, None
            for prof in (s.get('image_profile'), s.get('image_fallback')):
                if not prof or not prof.get('model'): continue
                try:
                    if refs and prof['provider'] == 'openai':
                        try:
                            img = H['_call_image_edit_openai'](prof, 'Use the attached product photos as the exact visual reference for the matching '
                                                               'equipment (shape, colour, proportions). ' + prompt, refs)
                        except GenError:
                            img = None
                    if img is None:
                        img = H['_call_image'](prof, prompt, 110 if refs else 230)
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
        show_prices = bool(opts_in.get('show_prices')) and H['_can_commercial'](claims) and snap['visibility'] == 'commercial'
        options = {'show_prices': show_prices, 'cover_note': clip(opts_in.get('cover_note'), 1500)}
        ss = snap['safe_source']
        lines = ss.get('lines') or []
        if not show_prices:
            lines = [{k: v for k, v in l.items() if k not in ('unit_selling_price', 'line_total')} for l in lines]
        content = {
            'v': (dr[0]['content'].get('v') or 2), 'reference': p['reference'], 'title': p.get('title') or '', 'customer': ss.get('customer') or p.get('customer_name') or '',
            'quote_ref': p.get('quote_ref') or '', 'project': ss.get('project') or '', 'date': datetime.utcnow().strftime('%Y-%m-%d'),
            'prepared_by': clip(d.get('prepared_by'), 120), 'doc': dr[0]['content'], 'lines': lines,
            'images': {i['slot_key']: {'path': i['image_path'], 'title': i['title'] or '', 'caption': i['caption'] or ''} for i in imgs if i['include'] and i['status'] == 'ready'},
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
