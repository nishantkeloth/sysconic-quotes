import { lazy, Suspense, useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import type Konva from 'konva';
import { api, ApiError } from '../api/client';
import type { AvRoom, AvRoomObject, DraftRoomObject } from '../types';
import { getObjectKey } from '../types';
import { libraryEntry } from '../deviceLibrary';
import { fromMeters } from '../units';
import DeviceLibraryPanel from '../components/DeviceLibraryPanel';
import RoomCanvas2D from '../components/RoomCanvas2D';
import DevicePropertiesPanel from '../components/DevicePropertiesPanel';
import ValidationPanel from '../components/ValidationPanel';
import { exportObjectsAsCsv, exportStageAsPng, composeHeroImage, triggerDownload } from '../exportUtils';
import { exportClientFloorPlan } from '../clientExport';

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
const MAX_HISTORY = 50;
const NUDGE_STEP_M = 0.05;
const NUDGE_STEP_LARGE_M = 0.5;
const DUPLICATE_OFFSET_M = 0.2;

// Undo/redo history lives alongside the objects array itself (one reducer,
// one atomic state) rather than as separate useState calls -- keeping them
// separate invites exactly the stale-closure/double-update bugs this kind
// of feature is famous for. Two dispatch shapes on purpose:
//   'commit' -- a real edit (add/delete/duplicate/drag/nudge): snapshots the
//               CURRENT objects into `past` before applying the new array,
//               and clears `future` (a fresh edit invalidates old redos).
//   'update' -- a live, in-progress change (every keystroke in a text
//               field, every pointermove of a drag) that should NOT itself
//               create an undo step -- callers pair this with an explicit
//               'checkpoint' dispatched once up front (on field focus / drag
//               start) so a whole edit session collapses into one undo step
//               instead of one per keystroke or per drag frame.
type HistoryState = { objects: DraftRoomObject[]; past: DraftRoomObject[][]; future: DraftRoomObject[][] };
type HistoryAction =
  | { type: 'set'; objects: DraftRoomObject[] }
  | { type: 'commit'; objects: DraftRoomObject[] }
  | { type: 'update'; objects: DraftRoomObject[] }
  | { type: 'checkpoint' }
  | { type: 'undo' }
  | { type: 'redo' };

function historyReducer(state: HistoryState, action: HistoryAction): HistoryState {
  switch (action.type) {
    case 'set':
      return { objects: action.objects, past: [], future: [] };
    case 'update':
      return { ...state, objects: action.objects };
    case 'checkpoint':
      return { ...state, past: [...state.past, state.objects].slice(-MAX_HISTORY), future: [] };
    case 'commit':
      return {
        objects: action.objects,
        past: [...state.past, state.objects].slice(-MAX_HISTORY),
        future: [],
      };
    case 'undo': {
      if (state.past.length === 0) return state;
      const prev = state.past[state.past.length - 1];
      return {
        objects: prev,
        past: state.past.slice(0, -1),
        future: [state.objects, ...state.future].slice(0, MAX_HISTORY),
      };
    }
    case 'redo': {
      if (state.future.length === 0) return state;
      const next = state.future[0];
      return {
        objects: next,
        past: [...state.past, state.objects].slice(-MAX_HISTORY),
        future: state.future.slice(1),
      };
    }
    default:
      return state;
  }
}

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
  const [history, dispatch] = useReducer(historyReducer, { objects: [], past: [], future: [] });
  const objects = history.objects;
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [showOverlays, setShowOverlays] = useState(true);
  const [showDimensions, setShowDimensions] = useState(true);
  const [snapToGrid, setSnapToGrid] = useState(true);
  const [showValidation, setShowValidation] = useState(false);
  // Default to Split so 2D and 3D stay visible and in sync side by side
  // (matches the reference product's simultaneous-panel layout) -- users
  // can still go full-width on either view via the toolbar.
  const [viewMode, setViewMode] = useState<'2D' | '3D' | 'Split'>('Split');

  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const skipNextAutosave = useRef(true); // don't autosave on initial load
  const stageRef = useRef<Konva.Stage | null>(null);
  const hero3dCaptureRef = useRef<(() => string) | null>(null);

  // Kept in sync via effects purely so the *one* global keydown listener
  // (registered once, below) can always read current values without being
  // torn down and re-registered on every keystroke/drag frame.
  const objectsRef = useRef(objects);
  const selectedKeyRef = useRef(selectedKey);
  const roomRef = useRef(room);
  useEffect(() => {
    objectsRef.current = objects;
  }, [objects]);
  useEffect(() => {
    selectedKeyRef.current = selectedKey;
  }, [selectedKey]);
  useEffect(() => {
    roomRef.current = room;
  }, [room]);

  useEffect(() => {
    let cancelled = false;
    api
      .get<AvRoom>(`/api/av_rooms/rooms/${roomId}`)
      .then((r) => {
        if (cancelled) return;
        setRoom(r);
        skipNextAutosave.current = true;
        dispatch({ type: 'set', objects: (r.objects || []).map(toDraft) });
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

  // Live, in-progress edits (typing, dragging) -- deliberately NOT an undo
  // step on their own. Callers that want the edit undoable call beginEdit()
  // once first (on focus / drag start); see the HistoryAction comment above.
  function updateObject(key: string, patch: Partial<DraftRoomObject>) {
    dispatch({ type: 'update', objects: objects.map((o) => (getObjectKey(o) === key ? { ...o, ...patch } : o)) });
  }

  // Snapshots the pre-edit state as a single undo step. Call once at the
  // start of an edit session (field focus, drag start) -- not on every
  // change within it.
  const beginEdit = useCallback(() => dispatch({ type: 'checkpoint' }), []);

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
    dispatch({ type: 'commit', objects: [...objects, draft] });
    setSelectedKey(draft._localId);
  }

  function handleMoveObject(key: string, x: number, y: number) {
    updateObject(key, { position_x: x, position_y: y });
  }

  function handleDeleteSelected() {
    if (!selectedKey) return;
    dispatch({ type: 'commit', objects: objects.filter((o) => getObjectKey(o) !== selectedKey) });
    setSelectedKey(null);
  }

  function handleDuplicateSelected() {
    if (!selectedObject || !room) return;
    const nudge = fromMeters(DUPLICATE_OFFSET_M, room.units);
    const copy: DraftRoomObject = {
      ...selectedObject,
      _localId: newLocalId(),
      position_x: selectedObject.position_x + nudge,
      position_y: selectedObject.position_y + nudge,
    };
    dispatch({ type: 'commit', objects: [...objects, copy] });
    setSelectedKey(copy._localId);
  }

  function handleExportCsv() {
    if (room) exportObjectsAsCsv(room, objects);
  }

  function handleExportPng() {
    if (stageRef.current) exportStageAsPng(stageRef.current, room?.room_name || 'room');
  }

  function handleExportClientPlan() {
    if (room) exportClientFloorPlan(room, objects);
  }

  async function handleExportHero3D() {
    if (!hero3dCaptureRef.current || !room) return;
    try {
      // toDataURL() on the WebGL canvas can throw a SecurityError if any
      // texture in the scene came from a cross-origin source without CORS
      // headers (the environment lighting's HDRI is fetched from a public
      // CDN) -- guarded end-to-end so a bad response there degrades to
      // "export didn't produce a file" instead of an unhandled error.
      const dataUrl = hero3dCaptureRef.current();
      const blob = await composeHeroImage(dataUrl, room.room_name);
      const safeName = (room.room_name || 'room').replace(/[^a-z0-9-_]+/gi, '_');
      triggerDownload(blob, `${safeName}-3d-view.png`);
    } catch (e) {
      setError('Could not export the 3D view as an image. Try the 2D client export instead.');
    }
  }

  // One global keydown listener, registered once (empty deps) -- it reads
  // objects/selectedKey/room off the refs kept in sync above rather than
  // closing over the render's own copies, so it never goes stale without
  // needing to be torn down and re-attached on every keystroke or drag
  // frame (which, with objects changing that often, would otherwise be
  // effectively every frame during a drag).
  useEffect(() => {
    function isEditableTarget(el: EventTarget | null): boolean {
      const t = el as HTMLElement | null;
      if (!t) return false;
      return t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable;
    }

    function handleKeyDown(e: KeyboardEvent) {
      const mod = e.ctrlKey || e.metaKey;

      // Undo/redo work even while a text field is focused (matches normal
      // browser/editor expectations) -- everything else below is disabled
      // while typing so Delete/Backspace/arrows behave normally in inputs.
      if (mod && e.key.toLowerCase() === 'z') {
        e.preventDefault();
        dispatch({ type: e.shiftKey ? 'redo' : 'undo' });
        return;
      }
      if (mod && e.key.toLowerCase() === 'y') {
        e.preventDefault();
        dispatch({ type: 'redo' });
        return;
      }
      if (isEditableTarget(e.target)) return;

      const currentRoom = roomRef.current;
      const currentObjects = objectsRef.current;
      const currentSelected = selectedKeyRef.current;
      if (!currentRoom) return;

      if (mod && e.key.toLowerCase() === 'd') {
        e.preventDefault();
        if (!currentSelected) return;
        const src = currentObjects.find((o) => getObjectKey(o) === currentSelected);
        if (!src) return;
        const nudge = fromMeters(DUPLICATE_OFFSET_M, currentRoom.units);
        const copy: DraftRoomObject = {
          ...src,
          _localId: newLocalId(),
          position_x: src.position_x + nudge,
          position_y: src.position_y + nudge,
        };
        dispatch({ type: 'commit', objects: [...currentObjects, copy] });
        setSelectedKey(copy._localId);
        return;
      }
      if ((e.key === 'Delete' || e.key === 'Backspace') && currentSelected) {
        e.preventDefault();
        dispatch({ type: 'commit', objects: currentObjects.filter((o) => getObjectKey(o) !== currentSelected) });
        setSelectedKey(null);
        return;
      }
      if (e.key === 'Escape') {
        setSelectedKey(null);
        return;
      }
      if (e.key.startsWith('Arrow') && currentSelected) {
        e.preventDefault();
        const step = fromMeters(e.shiftKey ? NUDGE_STEP_LARGE_M : NUDGE_STEP_M, currentRoom.units);
        let dx = 0;
        let dy = 0;
        if (e.key === 'ArrowLeft') dx = -step;
        if (e.key === 'ArrowRight') dx = step;
        if (e.key === 'ArrowUp') dy = -step;
        if (e.key === 'ArrowDown') dy = step;
        const obj = currentObjects.find((o) => getObjectKey(o) === currentSelected);
        if (!obj) return;
        dispatch({ type: 'checkpoint' });
        dispatch({
          type: 'update',
          objects: currentObjects.map((o) =>
            getObjectKey(o) === currentSelected
              ? { ...o, position_x: o.position_x + dx, position_y: o.position_y + dy }
              : o
          ),
        });
      }
    }

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

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
          <button className={viewMode === 'Split' ? 'active' : ''} onClick={() => setViewMode('Split')}>
            Split
          </button>
          <span className="avrd-toolbar-sep" />
          <button onClick={() => dispatch({ type: 'undo' })} disabled={history.past.length === 0} title="Undo (Ctrl+Z)">
            ↶ Undo
          </button>
          <button onClick={() => dispatch({ type: 'redo' })} disabled={history.future.length === 0} title="Redo (Ctrl+Shift+Z)">
            ↷ Redo
          </button>
          <button onClick={handleDuplicateSelected} disabled={!selectedKey} title="Duplicate (Ctrl+D)">
            ⧉ Duplicate
          </button>
          <span className="avrd-toolbar-sep" />
          <button
            className={showOverlays ? 'active' : ''}
            onClick={() => setShowOverlays((v) => !v)}
            title="Toggle camera FOV / mic pickup / display viewing-distance overlays"
          >
            {showOverlays ? 'Overlays: On' : 'Overlays: Off'}
          </button>
          {viewMode !== '3D' && (
            <>
              <button
                className={showDimensions ? 'active' : ''}
                onClick={() => setShowDimensions((v) => !v)}
                title="Show distance-to-wall dimension lines for the selected device"
              >
                {showDimensions ? 'Dimensions: On' : 'Dimensions: Off'}
              </button>
              <button
                className={snapToGrid ? 'active' : ''}
                onClick={() => setSnapToGrid((v) => !v)}
                title="Snap dragged devices to a 10cm grid"
              >
                {snapToGrid ? 'Snap: On' : 'Snap: Off'}
              </button>
            </>
          )}
          <span className="avrd-toolbar-sep" />
          <button onClick={() => setShowValidation(true)} title="Run engineering/coverage checks against this room">
            ✓ Check Design
          </button>
          <button onClick={handleExportCsv} title="Export the device list as a CSV bill of materials">
            ⤓ Export BOM
          </button>
          {viewMode !== '3D' && (
            <button onClick={handleExportPng} title="Export the current 2D canvas view as-is (editor screenshot)">
              ⤓ Export Image
            </button>
          )}
          {viewMode !== '3D' && (
            <button
              onClick={handleExportClientPlan}
              title="Export a client-ready floor plan: title block, equipment schedule, dimensions"
            >
              ⤓ Export for Client (2D)
            </button>
          )}
          {viewMode !== '2D' && (
            <button onClick={handleExportHero3D} title="Export a high-res, titled 3D presentation image">
              ⤓ Export for Client (3D)
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

        <div className={viewMode === 'Split' ? 'avrd-split-view' : 'avrd-split-view single'}>
          {viewMode !== '3D' && (
            <RoomCanvas2D
              room={room}
              objects={objects}
              selectedKey={selectedKey}
              onSelect={setSelectedKey}
              onMoveObject={handleMoveObject}
              onDropCategory={handleDropCategory}
              onBeginEdit={beginEdit}
              showOverlays={showOverlays}
              showDimensions={showDimensions}
              snapToGrid={snapToGrid}
              stageRef={stageRef}
            />
          )}
          {viewMode !== '2D' && (
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
                onBeginEdit={beginEdit}
                showOverlays={showOverlays}
                captureRef={hero3dCaptureRef}
              />
            </Suspense>
          )}
        </div>
      </div>

      <DevicePropertiesPanel
        object={selectedObject}
        units={room.units}
        onChange={(patch) => selectedKey && updateObject(selectedKey, patch as Partial<DraftRoomObject>)}
        onDelete={handleDeleteSelected}
        onDuplicate={handleDuplicateSelected}
        onBeginEdit={beginEdit}
      />

      {showValidation && (
        <ValidationPanel room={room} objects={objects} onClose={() => setShowValidation(false)} />
      )}
    </div>
  );
}
