"""
api/fsm_ai.py — FSM Module 18: AI Capabilities

Five lean, AV-specific capabilities layered on data the other FSM modules
already capture — no new schema. Uses the same Gemini setup as api/ai.py
(urllib, GEMINI_API_KEY/GEMINI_URL, JSON-only responses), duplicated per
the house convention since Vercel doesn't bundle sibling modules.

Kept deliberately narrow per "don't overcomplicate, AV-specific" steer:
  - triage:          suggest ticket_type / priority / failure_category from a
                      free-text description (AV fault vocabulary).
  - troubleshoot:     likely root causes + a checklist for an LED wall / DSP /
                      control processor style fault, grounded with matching
                      Knowledge Base articles when any exist.
  - similar_tickets:  keyword-matched past resolved tickets with the same
                      asset_category/failure_category, so an engineer can see
                      what fixed it last time — no AI needed, pure retrieval.
  - parts_suggest:    Gemini picks likely-needed parts, constrained to the
                      company's actual spare-parts inventory (candidate list
                      passed in) — it can only pick real part IDs, never
                      invent one.
  - assign_suggest:   deterministic scoring (skills, availability, current
                      open workload) — no LLM call. Matching people
                      to tickets is a scoring problem, not a language
                      problem, and a scoring function can't hallucinate an
                      engineer who doesn't exist. This is also where the
                      "automatic assignment" deferred from Module 6 lands.
"""

from flask import Flask, request, jsonify, g
import os
import json
import urllib.request
import urllib.error

from api.auth import verify_token, get_sb

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI_MODEL = 'gemini-3.5-flash'
GEMINI_URL = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'

VALID_TICKET_TYPES = {
    "incident", "service_request", "preventive_maintenance", "corrective_maintenance",
    "inspection", "warranty", "installation_support", "change_request", "consultation",
}
VALID_PRIORITIES = {"low", "medium", "high", "critical"}
VALID_FAILURE_CATEGORIES = {
    "hardware_failure", "firmware_software", "cabling_connectivity", "power_electrical",
    "configuration_error", "environmental", "user_error", "wear_and_tear", "other",
}


def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))


@app.before_request
def rbac_gate():
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'unauthorized'}), 401

    if not (claims.get('features') or {}).get('fsm_module'):
        return jsonify({'error': 'FSM module not enabled for this company'}), 403

    if not has_page_access(claims, 'fsm'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403

    g.claims = claims
    return None


def _gemini(system_prompt, user_msg, max_tokens=2000):
    if not GEMINI_API_KEY:
        return None, ('AI features are not configured yet (GEMINI_API_KEY is missing)', 500)

    body = json.dumps({
        'systemInstruction': {'parts': [{'text': system_prompt}]},
        'contents': [{'role': 'user', 'parts': [{'text': user_msg}]}],
        'generationConfig': {
            'temperature': 0.2,
            'maxOutputTokens': max_tokens,
            'responseMimeType': 'application/json',
            'thinkingConfig': {'thinkingBudget': 0},
        },
    }).encode('utf-8')

    req = urllib.request.Request(
        GEMINI_URL, data=body,
        headers={'Content-Type': 'application/json', 'x-goog-api-key': GEMINI_API_KEY},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=50) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, ('AI is rate-limited right now. Please wait a minute and try again.', 502)
        detail = e.read().decode('utf-8', 'ignore')[:200]
        return None, (f'AI service error ({e.code}). {detail}', 502)
    except Exception:
        return None, ('AI service is unreachable right now. Please try again.', 502)

    try:
        parts = data['candidates'][0]['content']['parts']
        text = ''.join(p.get('text', '') for p in parts).strip()
    except (KeyError, IndexError):
        return None, ('AI service returned an unexpected response. Please try again.', 502)

    if text.startswith('```'):
        text = text.strip('`').strip()
        if text.lower().startswith('json'):
            text = text[4:].strip()

    try:
        return json.loads(text), None
    except Exception:
        return None, ('AI service returned an unreadable response. Please try again.', 502)


@app.route("/api/fsm_ai", methods=["POST"])
def fsm_ai_router():
    body = request.get_json(silent=True) or {}
    action = body.get("action") or request.args.get("action")
    if action == "triage":
        return triage(body)
    if action == "troubleshoot":
        return troubleshoot(body)
    if action == "similar_tickets":
        return similar_tickets(body)
    if action == "parts_suggest":
        return parts_suggest(body)
    if action == "assign_suggest":
        return assign_suggest(body)
    return jsonify({"error": "unknown action"}), 400


# ── Triage ────────────────────────────────────────────────────────────────

TRIAGE_PROMPT = """You are a triage assistant for an AV/ELV field service desk (LED video walls, DSP/audio processors, control processors like Crestron/AMX/Extron, video wall processors, projectors, matrix switchers). A customer or engineer has described a problem in free text. Classify it.

ticket_type must be exactly one of: incident, service_request, preventive_maintenance, corrective_maintenance, inspection, warranty, installation_support, change_request, consultation
priority must be exactly one of: low, medium, high, critical (critical = total system down / safety issue / live event at risk; high = major function broken; medium = degraded but usable; low = cosmetic or minor)
failure_category must be exactly one of: hardware_failure, firmware_software, cabling_connectivity, power_electrical, configuration_error, environmental, user_error, wear_and_tear, other (omit this field entirely if the description doesn't describe a fault, e.g. it's a new install request)

Respond ONLY with valid JSON, no markdown fences: {"ticket_type":"...","priority":"...","failure_category":"... or null","reasoning":"one sentence"}"""


def triage(body):
    subject = (body.get("subject") or "").strip()
    description = (body.get("description") or "").strip()
    asset_category = (body.get("asset_category") or "").strip()
    if not subject and not description:
        return jsonify({"error": "subject or description required"}), 400

    user_msg = json.dumps({"subject": subject, "description": description, "asset_category": asset_category})
    result, err = _gemini(TRIAGE_PROMPT, user_msg, max_tokens=500)
    if err:
        return jsonify({"error": err[0]}), err[1]

    ticket_type = result.get("ticket_type")
    priority = result.get("priority")
    failure_category = result.get("failure_category")
    if ticket_type not in VALID_TICKET_TYPES:
        ticket_type = None
    if priority not in VALID_PRIORITIES:
        priority = None
    if failure_category not in VALID_FAILURE_CATEGORIES:
        failure_category = None

    return jsonify({
        "ticket_type": ticket_type,
        "priority": priority,
        "failure_category": failure_category,
        "reasoning": str(result.get("reasoning", ""))[:300],
    })


# ── Troubleshoot ─────────────────────────────────────────────────────────

TROUBLESHOOT_PROMPT = """You are a senior AV/ELV field service engineer specializing in LED video walls, DSP/audio processors, control processors (Crestron/AMX/Extron), video wall processors, projectors, and matrix switchers. Given a fault description (and optionally relevant excerpts from the company's own knowledge base), give a practical diagnosis.

Ground your answer in the knowledge base excerpts when they're relevant and directly say so; otherwise rely on general AV field service knowledge. Never invent specific part numbers or model-specific menu paths you're not given.

Respond ONLY with valid JSON, no markdown fences, in exactly this shape:
{"likely_causes":["short phrase", ...max 5],"checklist":["step 1", "step 2", ...max 8 ordered diagnostic steps],"grounded_in_kb":true or false}"""


def troubleshoot(body):
    company_id = g.claims['company_id']
    subject = (body.get("subject") or "").strip()
    description = (body.get("description") or "").strip()
    asset_category = (body.get("asset_category") or "").strip()
    if not subject and not description:
        return jsonify({"error": "subject or description required"}), 400

    sb = get_sb()
    kb_matches = []
    search_terms = [t for t in (subject + " " + description).lower().split() if len(t) > 3][:8]
    if search_terms or asset_category:
        rows = (
            sb.table("fsm_kb_articles")
            .select("title,category,content,asset_category")
            .eq("company_id", company_id)
            .in_("category", ["troubleshooting_guide", "known_error", "faq"])
            .limit(50)
            .execute()
            .data or []
        )
        for r in rows:
            hay = ((r.get("title") or "") + " " + (r.get("content") or "")).lower()
            score = sum(1 for t in search_terms if t in hay)
            if asset_category and r.get("asset_category") == asset_category:
                score += 2
            if score > 0:
                kb_matches.append((score, r))
        kb_matches.sort(key=lambda x: -x[0])
        kb_matches = [r for _, r in kb_matches[:3]]

    user_msg = json.dumps({
        "subject": subject,
        "description": description,
        "asset_category": asset_category,
        "knowledge_base_excerpts": [
            {"title": r["title"], "content": (r.get("content") or "")[:800]} for r in kb_matches
        ],
    })
    result, err = _gemini(TROUBLESHOOT_PROMPT, user_msg, max_tokens=1200)
    if err:
        return jsonify({"error": err[0]}), err[1]

    return jsonify({
        "likely_causes": [str(x)[:150] for x in (result.get("likely_causes") or [])[:5]],
        "checklist": [str(x)[:200] for x in (result.get("checklist") or [])[:8]],
        "grounded_in_kb": bool(result.get("grounded_in_kb")) and bool(kb_matches),
        "kb_sources": [{"title": r["title"], "category": r["category"]} for r in kb_matches],
    })


# ── Similar tickets (pure retrieval, no AI) ─────────────────────────────

def similar_tickets(body):
    company_id = g.claims['company_id']
    ticket_id = body.get("ticket_id")
    asset_category = body.get("asset_category")
    failure_category = body.get("failure_category")

    sb = get_sb()

    if ticket_id and not (asset_category and failure_category):
        t = sb.table("fsm_tickets").select("asset_id,failure_category").eq("company_id", company_id).eq("id", ticket_id).single().execute().data
        if t:
            failure_category = failure_category or t.get("failure_category")
            if t.get("asset_id") and not asset_category:
                asset = sb.table("fsm_assets").select("category").eq("id", t["asset_id"]).single().execute().data
                asset_category = asset.get("category") if asset else None

    if not asset_category and not failure_category:
        return jsonify({"matches": []})

    q = (
        sb.table("fsm_tickets")
        .select("id,ticket_number,subject,failure_category,root_cause,corrective_action,permanent_fix,resolved_at,asset_id")
        .eq("company_id", company_id)
        .in_("status", ["resolved", "closed"])
        .not_.is_("corrective_action", "null")
    )
    if failure_category:
        q = q.eq("failure_category", failure_category)
    if ticket_id:
        q = q.neq("id", ticket_id)
    rows = q.order("resolved_at", desc=True).limit(20).execute().data or []

    if asset_category and rows:
        asset_ids = list({r["asset_id"] for r in rows if r.get("asset_id")})
        if asset_ids:
            assets = sb.table("fsm_assets").select("id,category").in_("id", asset_ids).execute().data or []
            cat_by_asset = {a["id"]: a["category"] for a in assets}
            rows = [r for r in rows if cat_by_asset.get(r.get("asset_id")) == asset_category] or rows

    matches = [{
        "id": r["id"], "ticket_number": r["ticket_number"], "subject": r["subject"],
        "failure_category": r.get("failure_category"), "root_cause": r.get("root_cause"),
        "corrective_action": r.get("corrective_action"), "permanent_fix": r.get("permanent_fix"),
        "resolved_at": r.get("resolved_at"),
    } for r in rows[:5]]

    return jsonify({"matches": matches})


# ── Parts suggestion ─────────────────────────────────────────────────────

PARTS_PROMPT = """You are an AV/ELV field service parts advisor. Given a fault description and a list of the company's actual spare parts inventory (id, name, category, compatible_models), pick the parts most likely to be needed for this repair. You may ONLY choose from the given list — never invent a part or return an id that isn't in the list. If nothing in the list is plausibly relevant, return an empty list.

Respond ONLY with valid JSON, no markdown fences: {"suggested_part_ids":["id", ...max 5],"reasoning":"one sentence"}"""


def parts_suggest(body):
    company_id = g.claims['company_id']
    description = (body.get("description") or "").strip()
    asset_category = (body.get("asset_category") or "").strip()
    failure_category = (body.get("failure_category") or "").strip()
    if not description and not asset_category:
        return jsonify({"error": "description or asset_category required"}), 400

    sb = get_sb()
    parts = (
        sb.table("fsm_spare_parts")
        .select("id,name,category,compatible_models,quantity_on_hand")
        .eq("company_id", company_id)
        .execute()
        .data or []
    )
    if not parts:
        return jsonify({"suggested_parts": [], "reasoning": "No spare parts in inventory yet."})

    candidates = parts[:100]
    user_msg = json.dumps({
        "description": description,
        "asset_category": asset_category,
        "failure_category": failure_category,
        "inventory": [{"id": p["id"], "name": p["name"], "category": p.get("category"), "compatible_models": p.get("compatible_models")} for p in candidates],
    })
    result, err = _gemini(PARTS_PROMPT, user_msg, max_tokens=800)
    if err:
        return jsonify({"error": err[0]}), err[1]

    valid_ids = {p["id"] for p in candidates}
    by_id = {p["id"]: p for p in candidates}
    suggested_ids = [pid for pid in (result.get("suggested_part_ids") or [])[:5] if pid in valid_ids]

    return jsonify({
        "suggested_parts": [
            {"id": pid, "name": by_id[pid]["name"], "category": by_id[pid].get("category"), "quantity_on_hand": by_id[pid].get("quantity_on_hand")}
            for pid in suggested_ids
        ],
        "reasoning": str(result.get("reasoning", ""))[:300],
    })


# ── Assign suggestion (deterministic scoring, no LLM) ────────────────────

def assign_suggest(body):
    company_id = g.claims['company_id']
    ticket_id = body.get("ticket_id")
    if not ticket_id:
        return jsonify({"error": "ticket_id required"}), 400

    sb = get_sb()
    ticket = sb.table("fsm_tickets").select("*").eq("company_id", company_id).eq("id", ticket_id).single().execute().data
    if not ticket:
        return jsonify({"error": "ticket not found"}), 404

    # Note: fsm_sites has no territory/region column (only fsm_engineers does),
    # so there's no clean way to match a ticket's site to an engineer's
    # territory yet. Scoring below is skill + availability + workload only.
    asset_category = None
    if ticket.get("asset_id"):
        asset = sb.table("fsm_assets").select("category").eq("id", ticket["asset_id"]).single().execute().data
        asset_category = asset.get("category") if asset else None

    engineers = sb.table("fsm_engineers").select("*").eq("company_id", company_id).eq("is_active", True).execute().data or []
    if not engineers:
        return jsonify({"suggestions": []})

    open_statuses = ["new", "acknowledged", "assigned", "accepted", "travelling", "on_site", "diagnosis",
                      "waiting_customer", "waiting_parts", "waiting_approval"]
    open_tix = (
        sb.table("fsm_tickets")
        .select("assigned_engineer_id")
        .eq("company_id", company_id)
        .in_("status", open_statuses)
        .not_.is_("assigned_engineer_id", "null")
        .execute()
        .data or []
    )
    workload = {}
    for t in open_tix:
        eid = t["assigned_engineer_id"]
        workload[eid] = workload.get(eid, 0) + 1

    fault_text = ((ticket.get("subject") or "") + " " + (ticket.get("description") or "")).lower()

    scored = []
    for e in engineers:
        score = 0
        reasons = []
        if e.get("availability") == "available":
            score += 3
            reasons.append("available")
        elif e.get("availability") == "busy":
            score += 1
        else:
            reasons.append(e.get("availability") or "unavailable")

        skills = [s.lower() for s in (e.get("skills") or [])]
        if asset_category and any(asset_category.lower() in s or s in asset_category.lower() for s in skills):
            score += 4
            reasons.append(f"skilled in {asset_category}")
        matched_skill_words = [s for s in skills if s and s in fault_text]
        if matched_skill_words:
            score += 2
            reasons.append(f"matches: {', '.join(matched_skill_words[:2])}")

        current_load = workload.get(e["id"], 0)
        score -= current_load
        if current_load:
            reasons.append(f"{current_load} open ticket(s)")

        scored.append({
            "id": e["id"], "name": e["name"], "score": score,
            "availability": e.get("availability"), "open_tickets": current_load,
            "reasons": reasons,
        })

    scored.sort(key=lambda x: -x["score"])
    return jsonify({"suggestions": scored[:5]})
