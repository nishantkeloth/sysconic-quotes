"""
api/fsm_kb.py — FSM Module 15: Knowledge Base

Articles / videos / manuals / troubleshooting guides / FAQ / known errors —
a single simple table (fsm_kb_articles), CRUD + basic keyword search.

Deliberately NOT doing AI-powered natural-language search here — that's
Module 18 (AI Capabilities), which already has api/ai.py's Gemini
integration to build a real conversational search on top of this data.
Duplicating that here would be over-engineering a module that's supposed
to just be the content store.

Same RBAC gate pattern as every other fsm_*.py file.
"""

from flask import Flask, request, jsonify, g
import uuid
from datetime import datetime

from api.auth import verify_token, get_sb

app = Flask(__name__)

VALID_CATEGORIES = {"article", "video", "manual", "troubleshooting_guide", "faq", "known_error"}


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


@app.route("/api/fsm_kb", methods=["GET", "POST"])
def fsm_kb_router():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        action = body.get("action") or request.args.get("action")
        if action == "create":
            return create_article(body)
        if action == "update":
            return update_article(body)
        if action == "delete":
            return delete_article(body)
        return jsonify({"error": "unknown action"}), 400

    action = request.args.get("action", "list")
    if action == "list":
        return list_articles()
    if action == "get":
        return get_article()
    return jsonify({"error": "unknown action"}), 400


def list_articles():
    company_id = g.claims['company_id']
    sb = get_sb()

    q = sb.table("fsm_kb_articles").select("*").eq("company_id", company_id)

    category = request.args.get("category")
    if category:
        q = q.eq("category", category)

    search = (request.args.get("search") or "").strip()

    rows = q.order("updated_at", desc=True).execute().data or []

    if search:
        s = search.lower()
        rows = [
            r for r in rows
            if s in (r.get("title") or "").lower()
            or s in (r.get("content") or "").lower()
            or any(s in (tag or "").lower() for tag in (r.get("tags") or []))
        ]

    return jsonify({"articles": rows})


def get_article():
    company_id = g.claims['company_id']
    article_id = request.args.get("id")
    if not article_id:
        return jsonify({"error": "id required"}), 400
    sb = get_sb()
    row = sb.table("fsm_kb_articles").select("*").eq("company_id", company_id).eq("id", article_id).single().execute().data
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({"article": row})


def create_article(body):
    company_id = g.claims['company_id']
    user_id = g.claims.get('sub') or g.claims.get('user_id')
    sb = get_sb()

    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400

    category = body.get("category") or "article"
    if category not in VALID_CATEGORIES:
        return jsonify({"error": f"invalid category, must be one of {sorted(VALID_CATEGORIES)}"}), 400

    tags = body.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    now = datetime.utcnow().isoformat()
    entry = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "title": title,
        "category": category,
        "content": body.get("content") or "",
        "tags": tags,
        "asset_category": body.get("asset_category") or None,
        "attachment_url": body.get("attachment_url") or None,
        "is_published": body.get("is_published", True),
        "created_by": user_id,
        "created_at": now,
        "updated_at": now,
    }
    row = sb.table("fsm_kb_articles").insert(entry).execute().data[0]
    return jsonify({"article": row})


def update_article(body):
    company_id = g.claims['company_id']
    article_id = body.get("id")
    if not article_id:
        return jsonify({"error": "id required"}), 400
    sb = get_sb()

    updates = {"updated_at": datetime.utcnow().isoformat()}
    for field in ("title", "content", "asset_category", "attachment_url", "is_published"):
        if field in body:
            updates[field] = body[field]

    if "category" in body:
        if body["category"] not in VALID_CATEGORIES:
            return jsonify({"error": f"invalid category, must be one of {sorted(VALID_CATEGORIES)}"}), 400
        updates["category"] = body["category"]

    if "tags" in body:
        tags = body["tags"] or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        updates["tags"] = tags

    row = (
        sb.table("fsm_kb_articles")
        .update(updates)
        .eq("company_id", company_id)
        .eq("id", article_id)
        .execute()
        .data
    )
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({"article": row[0]})


def delete_article(body):
    company_id = g.claims['company_id']
    article_id = body.get("id")
    if not article_id:
        return jsonify({"error": "id required"}), 400
    sb = get_sb()
    sb.table("fsm_kb_articles").delete().eq("company_id", company_id).eq("id", article_id).execute()
    return jsonify({"ok": True})
