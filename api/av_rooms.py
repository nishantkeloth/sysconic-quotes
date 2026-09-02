"""
api/av_rooms.py — AV Room Designer: Phase 1 backend (schema + CRUD only, no
canvas/UI yet -- see migration-av-room-designer.sql for the data model and
its own header comment for the full rationale).

The room design UI itself will be a separate React micro-app (Konva for 2D,
Three.js for 3D later) mounted into QTcal -- this file only needs to expose
plain JSON CRUD so that app (and, later, the AI Design Assistant) has
somewhere to read/write design state. It intentionally does NOT implement
the engineering rules engine, cable estimation, BOM generation, or versioning
yet -- those are later slices once the canvas exists to exercise this API
against.

Wired to the same RBAC pattern as api/delivery_challans.py / api/fsm_*.py:
single 'avRoomDesigner' page key, gated behind the av_room_designer feature
flag. Uses the api.auth cross-import (confirmed working in production via
api/delivery_challans.py) rather than the older fully-duplicated-per-file
pattern used by api/quotes.py / api/integrations.py.

Add this file to BOTH `builds` and `routes` in vercel.json.
"""

from flask import Flask, request, jsonify, g
import uuid
import traceback
from datetime import datetime

from api.auth import verify_token, get_sb

app = Flask(__name__)

PROJECT_STATUSES = {'draft', 'concept', 'under_review', 'approved', 'quotation_generated', 'locked'}
ROOM_STATUSES = PROJECT_STATUSES  # same lifecycle vocabulary, per the spec
UNITS = {'mm', 'cm', 'm', 'ft', 'in'}


def has_page_access(claims, page_key):
    if claims.get('role') == 'admin':
        return True
    rp = claims.get('role_permissions')
    if rp is None:
        return True
    return bool(rp.get(page_key))


def has_feature(claims, feature):
    return bool((claims.get('features') or {}).get(feature))


@app.before_request
def rbac_gate():
    claims = verify_token(request)
    if not claims:
        return jsonify({'error': 'unauthorized'}), 401
    if not has_feature(claims, 'av_room_designer'):
        return jsonify({'error': 'AV Room Designer is not enabled for this company'}), 403
    if not has_page_access(claims, 'avRoomDesigner'):
        return jsonify({'error': 'You do not have access to this feature.'}), 403
    g.claims = claims
    return None


# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------
def _numf(v, d=0.0):
    try:
        return float(v) if v not in (None, '') else d
    except (TypeError, ValueError):
        return d


def _clean_project(d, partial=False):
    out = {}
    if 'project_name' in d:
        name = (d.get('project_name') or '').strip()[:300]
        if not name and not partial:
            return None, 'project_name is required'
        out['project_name'] = name or None
    elif not partial:
        return None, 'project_name is required'
    for k in ('customer_id', 'quote_id', 'deal_id'):
        if k in d:
            out[k] = d.get(k) or None
    if 'status' in d:
        s = (d.get('status') or '').strip()
        if s not in PROJECT_STATUSES:
            return None, f'invalid status: {s}'
        out['status'] = s
    elif not partial:
        out['status'] = 'draft'
    return out, None


def _clean_room(d, partial=False):
    out = {}
    if 'room_name' in d:
        name = (d.get('room_name') or '').strip()[:200]
        if not name and not partial:
            return None, 'room_name is required'
        out['room_name'] = name or None
    elif not partial:
        return None, 'room_name is required'
    if 'room_type' in d:
        out['room_type'] = (d.get('room_type') or '').strip()[:100] or None
    for k in ('length', 'width', 'height', 'ceiling_height'):
        if k in d:
            out[k] = _numf(d.get(k), None) if d.get(k) not in (None, '') else None
    if 'units' in d:
        u = (d.get('units') or 'm').strip()
        if u not in UNITS:
            return None, f'invalid units: {u}'
        out['units'] = u
    elif not partial:
        out['units'] = 'm'
    for k in ('capacity', 'seating_capacity'):
        if k in d:
            try:
                out[k] = int(d[k]) if d.get(k) not in (None, '') else None
            except (TypeError, ValueError):
                return None, f'invalid {k}'
    if 'quantity' in d:
        try:
            out['quantity'] = max(1, int(d.get('quantity') or 1))
        except (TypeError, ValueError):
            return None, 'invalid quantity'
    if 'status' in d:
        s = (d.get('status') or '').strip()
        if s not in ROOM_STATUSES:
            return None, f'invalid status: {s}'
        out['status'] = s
    elif not partial:
        out['status'] = 'draft'
    return out, None


def _clean_object(d):
    """One placed device/furniture item. Only object_type/category/object_name
    are required -- everything else defaults sanely so a freshly-dropped
    generic device (no product mapped, no size known yet) still saves."""
    obj_type = (d.get('object_type') or 'device').strip()[:30]
    category = (d.get('category') or '').strip()[:100]
    if not category:
        return None, 'category is required'
    name = (d.get('object_name') or category).strip()[:200]
    out = {
        'object_type': obj_type,
        'category': category,
        'object_name': name,
        'product_id': d.get('product_id') or None,
        'position_x': _numf(d.get('position_x')),
        'position_y': _numf(d.get('position_y')),
        'position_z': _numf(d.get('position_z')),
        'rotation_x': _numf(d.get('rotation_x')),
        'rotation_y': _numf(d.get('rotation_y')),
        'rotation_z': _numf(d.get('rotation_z')),
        'width': _numf(d.get('width'), None) if d.get('width') not in (None, '') else None,
        'height': _numf(d.get('height'), None) if d.get('height') not in (None, '') else None,
        'depth': _numf(d.get('depth'), None) if d.get('depth') not in (None, '') else None,
        'mounting_height': _numf(d.get('mounting_height'), None) if d.get('mounting_height') not in (None, '') else None,
        'mounting_type': (d.get('mounting_type') or '').strip()[:100] or None,
        'quantity': max(1, int(d.get('quantity') or 1)) if str(d.get('quantity') or '').strip() else 1,
        'notes': (d.get('notes') or '').strip()[:1000] or None,
        'metadata_json': d.get('metadata_json') or {},
    }
    return out, None


def _get_project_or_404(sb, company_id, project_id):
    row = sb.table('av_design_projects').select('*').eq('id', project_id).eq('company_id', company_id).execute()
    return row.data[0] if row.data else None


def _get_room_or_404(sb, company_id, room_id):
    row = sb.table('av_rooms').select('*').eq('id', room_id).eq('company_id', company_id).execute()
    return row.data[0] if row.data else None


# ------------------------------------------------------------------
# Design projects
# ------------------------------------------------------------------
@app.route('/api/av_rooms/projects', methods=['GET'])
def list_projects():
    company_id = g.claims['company_id']
    sb = get_sb()
    q = sb.table('av_design_projects').select('*').eq('company_id', company_id)
    quote_id = request.args.get('quote_id')
    if quote_id:
        q = q.eq('quote_id', quote_id)
    status = request.args.get('status')
    if status:
        q = q.eq('status', status)
    rows = q.order('created_at', desc=True).limit(500).execute()
    return jsonify({'projects': rows.data or []})


@app.route('/api/av_rooms/projects', methods=['POST'])
def create_project():
    company_id = g.claims['company_id']
    payload = request.get_json(force=True) or {}
    cleaned, err = _clean_project(payload)
    if err:
        return jsonify({'error': err}), 400

    sb = get_sb()
    now = datetime.utcnow().isoformat()
    record = {
        'id': str(uuid.uuid4()),
        'company_id': company_id,
        'created_by': g.claims['user_id'],
        'created_at': now,
        'updated_at': now,
        **cleaned,
    }
    try:
        result = sb.table('av_design_projects').insert(record).execute()
        return jsonify(result.data[0]), 201
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'Could not create project: {e}'}), 500


@app.route('/api/av_rooms/projects/<project_id>', methods=['GET'])
def get_project(project_id):
    company_id = g.claims['company_id']
    sb = get_sb()
    project = _get_project_or_404(sb, company_id, project_id)
    if not project:
        return jsonify({'error': 'not found'}), 404
    rooms = sb.table('av_rooms').select('*').eq('project_id', project_id).eq('company_id', company_id) \
        .order('created_at').execute()
    project['rooms'] = rooms.data or []
    return jsonify(project)


@app.route('/api/av_rooms/projects/<project_id>', methods=['PATCH'])
def update_project(project_id):
    company_id = g.claims['company_id']
    sb = get_sb()
    if not _get_project_or_404(sb, company_id, project_id):
        return jsonify({'error': 'not found'}), 404
    payload = request.get_json(force=True) or {}
    cleaned, err = _clean_project(payload, partial=True)
    if err:
        return jsonify({'error': err}), 400
    cleaned['updated_at'] = datetime.utcnow().isoformat()
    try:
        result = sb.table('av_design_projects').update(cleaned).eq('id', project_id).eq('company_id', company_id).execute()
        return jsonify(result.data[0])
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'Could not update project: {e}'}), 500


@app.route('/api/av_rooms/projects/<project_id>', methods=['DELETE'])
def delete_project(project_id):
    company_id = g.claims['company_id']
    sb = get_sb()
    if not _get_project_or_404(sb, company_id, project_id):
        return jsonify({'error': 'not found'}), 404
    # Cascades to av_rooms -> av_room_objects via ON DELETE CASCADE.
    sb.table('av_design_projects').delete().eq('id', project_id).eq('company_id', company_id).execute()
    return jsonify({'ok': True})


# ------------------------------------------------------------------
# Rooms
# ------------------------------------------------------------------
@app.route('/api/av_rooms/rooms', methods=['POST'])
def create_room():
    company_id = g.claims['company_id']
    payload = request.get_json(force=True) or {}
    project_id = payload.get('project_id')
    if not project_id:
        return jsonify({'error': 'project_id is required'}), 400

    sb = get_sb()
    if not _get_project_or_404(sb, company_id, project_id):
        return jsonify({'error': 'project not found'}), 404

    cleaned, err = _clean_room(payload)
    if err:
        return jsonify({'error': err}), 400

    now = datetime.utcnow().isoformat()
    record = {
        'id': str(uuid.uuid4()),
        'company_id': company_id,
        'project_id': project_id,
        'created_by': g.claims['user_id'],
        'created_at': now,
        'updated_at': now,
        **cleaned,
    }
    try:
        result = sb.table('av_rooms').insert(record).execute()
        return jsonify(result.data[0]), 201
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'Could not create room: {e}'}), 500


@app.route('/api/av_rooms/rooms/<room_id>', methods=['GET'])
def get_room(room_id):
    company_id = g.claims['company_id']
    sb = get_sb()
    room = _get_room_or_404(sb, company_id, room_id)
    if not room:
        return jsonify({'error': 'not found'}), 404
    objects = sb.table('av_room_objects').select('*').eq('room_id', room_id).eq('company_id', company_id) \
        .order('created_at').execute()
    room['objects'] = objects.data or []
    return jsonify(room)


@app.route('/api/av_rooms/rooms/<room_id>', methods=['PATCH'])
def update_room(room_id):
    company_id = g.claims['company_id']
    sb = get_sb()
    if not _get_room_or_404(sb, company_id, room_id):
        return jsonify({'error': 'not found'}), 404
    payload = request.get_json(force=True) or {}
    cleaned, err = _clean_room(payload, partial=True)
    if err:
        return jsonify({'error': err}), 400
    cleaned['updated_at'] = datetime.utcnow().isoformat()
    try:
        result = sb.table('av_rooms').update(cleaned).eq('id', room_id).eq('company_id', company_id).execute()
        return jsonify(result.data[0])
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'Could not update room: {e}'}), 500


@app.route('/api/av_rooms/rooms/<room_id>', methods=['DELETE'])
def delete_room(room_id):
    company_id = g.claims['company_id']
    sb = get_sb()
    if not _get_room_or_404(sb, company_id, room_id):
        return jsonify({'error': 'not found'}), 404
    sb.table('av_rooms').delete().eq('id', room_id).eq('company_id', company_id).execute()
    return jsonify({'ok': True})


# ------------------------------------------------------------------
# Room objects (placed devices/furniture) — autosave target
# ------------------------------------------------------------------
@app.route('/api/av_rooms/rooms/<room_id>/objects', methods=['GET'])
def list_objects(room_id):
    company_id = g.claims['company_id']
    sb = get_sb()
    if not _get_room_or_404(sb, company_id, room_id):
        return jsonify({'error': 'not found'}), 404
    rows = sb.table('av_room_objects').select('*').eq('room_id', room_id).eq('company_id', company_id) \
        .order('created_at').execute()
    return jsonify({'objects': rows.data or []})


@app.route('/api/av_rooms/rooms/<room_id>/objects', methods=['PUT'])
def save_objects(room_id):
    """Full-replace autosave: the canvas sends its complete current object
    list on every save (debounced client-side per the spec's autosave
    requirement), and this swaps it in wholesale. Simpler and more robust
    than diffing adds/moves/deletes against what's already stored, at the
    cost of not being a true partial-update API -- fine for Phase 1 since
    the client always holds the full authoritative in-memory state anyway
    (per the spec's "one common object model" principle in section 18)."""
    company_id = g.claims['company_id']
    sb = get_sb()
    if not _get_room_or_404(sb, company_id, room_id):
        return jsonify({'error': 'not found'}), 404

    payload = request.get_json(force=True) or {}
    objects_in = payload.get('objects')
    if not isinstance(objects_in, list):
        return jsonify({'error': 'objects must be a list'}), 400

    cleaned = []
    for i, d in enumerate(objects_in):
        c, err = _clean_object(d)
        if err:
            return jsonify({'error': f'object[{i}]: {err}'}), 400
        cleaned.append(c)

    now = datetime.utcnow().isoformat()
    records = [{
        'id': str(uuid.uuid4()),
        'company_id': company_id,
        'room_id': room_id,
        'created_by': g.claims['user_id'],
        'created_at': now,
        'updated_at': now,
        **c,
    } for c in cleaned]

    try:
        # Not a single atomic transaction (supabase-py has no cross-statement
        # transaction helper here) -- a failure between delete and insert
        # would leave the room's objects empty rather than corrupted, and
        # the client still holds its full in-memory state to retry the save.
        sb.table('av_room_objects').delete().eq('room_id', room_id).eq('company_id', company_id).execute()
        if records:
            sb.table('av_room_objects').insert(records).execute()
        sb.table('av_rooms').update({'updated_at': now}).eq('id', room_id).eq('company_id', company_id).execute()
        return jsonify({'objects': records})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'Could not save room objects: {e}'}), 500
