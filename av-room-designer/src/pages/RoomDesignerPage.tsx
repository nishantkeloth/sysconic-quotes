import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { AvRoom, AvRoomObject, DraftRoomObject } from '../types';
import { getObjectKey } from '../types';
import { libraryEntry } from '../deviceLibrary';
import { fromMeters } from '../units';
import DeviceLibraryPanel from '../components/DeviceLibraryPanel';
import RoomCanvas2D from '../components/RoomCanvas2D';
import DevicePropertiesPanel from '../components/DevicePropertiesPanel';

// Lazy-loaded: three.js + @react-three/fiber + @react-three/drei add ~1MB
// to the bundle. Most sessions will only ever use the 2D editor (per spec
// §7, 2D is the primary design interface), so that cost should only be
// paid the moment someone actually opens the 3D view, not on every login.
const RoomViewer3D = lazy(() => import('../components/RoomViewer3D'));

// The canvas works entirely with "draft" objects (no server id) locally,
// even for devices loaded from the server -- see save_objects() in
// api/av_rooms.py: PUT is a full-replace that reissues brand-new UUIDs for
// every object on every save, so there is no stable server id to hold onto
// client-side between saves anyway. `_localId` is this page's own stable
// identity for React keys and selection, generated once per object and
// never touched by a save round-trip.
let localIdCounter = 0;
function newLocalId() {
  localIdCounter += 1;
  return `local-${Date.now()}-${localIdCounter}`;
}

function toDraft(o: AvRoomObject): DraftRoomObject {
  const { id, company_id, room_id, created_at, updated_at, ...rest } = o;
  return { ...rest, _localId: newLocalId() };
}

const AUTOSAVE_DELAY_MS = 1200;

export default function RoomDesignerPage({
  projectId,
  roomId,
  onBack,
}: {
  projectId: string;
  roomId: string;
  onBack: () => void;
}) {
  const [room, setRoom] = useState<AvRoom | null>(null);
  const [objects, setObjects] = useState<DraftRoomObject[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [showOverlays, setShowOverlays] = useState(true);
  const [viewMode, setViewMode] = useState<'2D' | '3D'>('2D');

  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const skipNextAutosave = useRef(true); // don't autosave on initial load

  useEffect(() => {
    let cancelled = false;
    api
      .get<AvRoom>(`/api/av_rooms/rooms/${roomId}`)
      .then((r) => {
        if (cancelled) return;
        setRoom(r);
        skipNextAutosave.current = true;
        setObjects((r.objects || []).map(toDraft));
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : 'Failed to load room'));
    return () => {
      cancelled = true;
    };
  }, [roomId]);

  useEffect(() => {
    if (skipNextAutosave.current) {
      skipNextAutosave.current = false;
      return;
    }
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      void saveNow();
    }, AUTOSAVE_DELAY_MS);
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [objects]);

  async function saveNow() {
    setSaveStatus('saving');
    try {
      const payload = objects.map(({ _localId, ...rest }) => rest);
      await api.put<{ objects: AvRoomObject[] }>(`/api/av_rooms/rooms/${roomId}/objects`, {
        objects: payload,
      });
      setSaveStatus('saved');
    } catch (e) {
      setSaveStatus('error');
      setError(e instanceof ApiError ? e.message : 'Failed to save room objects');
    }
  }

  const selectedObject = useMemo(
    () => objects.find((o) => getObjectKey(o) === selectedKey) || null,
    [objects, selectedKey]
  );

  function updateObject(key: string, patch: Partial<DraftRoomObject>) {
    setObjects((prev) => prev.map((o) => (getObjectKey(o) === key ? { ...o, ...patch } : o)));
  }

  function handleDropCategory(category: string, x: number, y: number) {
    if (!room) return;
    const entry = libraryEntry(category);
    const draft: DraftRoomObject = {
      _localId: newLocalId(),
      object_type: entry?.objectType || 'device',
      category,
      object_name: entry?.label || category,
      product_id: null,
      position_x: x,
      position_y: y,
      position_z: 0,
      rotation_x: 0,
      rotation_y: 0,
      rotation_z: 0,
      width: entry ? fromMeters(entry.defaultWidth, room.units) : null,
      height: entry ? fromMeters(entry.defaultHeight, room.units) : null,
      depth: entry ? fromMeters(entry.defaultDepth, room.units) : null,
      mounting_height: null,
      mounting_type: null,
      quantity: 1,
      notes: null,
      metadata_json: {},
    };
    setObjects((prev) => [...prev, draft]);
    setSelectedKey(draft._localId);
  }

  function handleMoveObject(key: string, x: number, y: number) {
    updateObject(key, { position_x: x, position_y: y });
  }

  function handleDeleteSelected() {
    if (!selectedKey) return;
    setObjects((prev) => prev.filter((o) => getObjectKey(o) !== selectedKey));
    setSelectedKey(null);
  }

  if (error && !room) {
    return (
      <div className="avrd-page">
        <div className="avrd-error">{error}</div>
        <button className="avrd-btn" onClick={onBack}>
          Back to projects
        </button>
      </div>
    );
  }

  if (!room) {
    return (
      <div className="avrd-page">
        <p style={{ color: 'var(--gray-500)' }}>Loading room…</p>
      </div>
    );
  }

  return (
    <div className="avrd-designer">
      <DeviceLibraryPanel />

      <div className="avrd-canvas-wrap-outer" style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
        <div className="avrd-canvas-toolbar">
          <button onClick={onBack}>← Back</button>
          <span style={{ padding: '6px 10px', fontWeight: 600 }}>{room.room_name}</span>
          <button className={viewMode === '2D' ? 'active' : ''} onClick={() => setViewMode('2D')}>
            2D
          </button>
          <button className={viewMode === '3D' ? 'active' : ''} onClick={() => setViewMode('3D')}>
            3D
          </button>
          {viewMode === '2D' && (
            <button
              className={showOverlays ? 'active' : ''}
              onClick={() => setShowOverlays((v) => !v)}
              title="Toggle camera FOV / mic pickup / display viewing-distance overlays"
            >
              {showOverlays ? 'Overlays: On' : 'Overlays: Off'}
            </button>
          )}
        </div>
        <div
          className={`avrd-save-status ${saveStatus}`}
          style={{ position: 'absolute', top: 10, right: 300, zIndex: 5 }}
        >
          {saveStatus === 'idle' && 'No changes'}
          {saveStatus === 'saving' && 'Saving…'}
          {saveStatus === 'saved' && 'Saved'}
          {saveStatus === 'error' && (error || 'Save failed')}
        </div>

        {viewMode === '2D' ? (
          <RoomCanvas2D
            room={room}
            objects={objects}
            selectedKey={selectedKey}
            onSelect={setSelectedKey}
            onMoveObject={handleMoveObject}
            onDropCategory={handleDropCategory}
            showOverlays={showOverlays}
          />
        ) : (
          <Suspense
            fallback={
              <div className="avrd-canvas-wrap" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <p style={{ color: 'var(--gray-500)' }}>Loading 3D view…</p>
              </div>
            }
          >
            <RoomViewer3D
              room={room}
              objects={objects}
              selectedKey={selectedKey}
              onSelect={setSelectedKey}
              onMoveObject={handleMoveObject}
            />
          </Suspense>
        )}
      </div>

      <DevicePropertiesPanel
        object={selectedObject}
        units={room.units}
        onChange={(patch) => selectedKey && updateObject(selectedKey, patch as Partial<DraftRoomObject>)}
        onDelete={handleDeleteSelected}
      />
    </div>
  );
}
