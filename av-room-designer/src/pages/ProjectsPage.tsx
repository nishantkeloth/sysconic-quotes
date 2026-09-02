import { useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { AvDesignProject, AvRoom, RoomUnits } from '../types';

// Two-level list screen: projects, then a selected project's rooms.
// Deliberately kept as one component with local `selectedProject` state
// rather than adding a router, matching App.tsx's minimal view-state
// approach -- this whole screen is "step 1" before entering a room's
// 2D designer via onOpenRoom.
export default function ProjectsPage({
  onOpenRoom,
}: {
  onOpenRoom: (projectId: string, roomId: string) => void;
}) {
  const [projects, setProjects] = useState<AvDesignProject[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<AvDesignProject | null>(null);
  const [showNewProject, setShowNewProject] = useState(false);
  const [showNewRoom, setShowNewRoom] = useState(false);

  function loadProjects() {
    setError(null);
    // list_projects wraps its array as { projects: [...] } -- see api/av_rooms.py.
    api
      .get<{ projects: AvDesignProject[] }>('/api/av_rooms/projects')
      .then((res) => setProjects(res.projects))
      .catch((e) => setError(e instanceof ApiError ? e.message : 'Failed to load projects'));
  }

  function loadProject(id: string) {
    setError(null);
    api
      .get<AvDesignProject>(`/api/av_rooms/projects/${id}`)
      .then(setSelected)
      .catch((e) => setError(e instanceof ApiError ? e.message : 'Failed to load project'));
  }

  useEffect(loadProjects, []);

  if (error) {
    return (
      <div className="avrd-page">
        <div className="avrd-error">{error}</div>
        <button className="avrd-btn" onClick={selected ? () => loadProject(selected.id) : loadProjects}>
          Retry
        </button>
      </div>
    );
  }

  if (selected) {
    return (
      <div className="avrd-page">
        <div className="avrd-crumb">
          <span onClick={() => setSelected(null)}>Projects</span> / {selected.project_name}
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2>{selected.project_name}</h2>
          <button className="avrd-btn primary" onClick={() => setShowNewRoom(true)}>
            + New Room
          </button>
        </div>

        {(!selected.rooms || selected.rooms.length === 0) && (
          <p style={{ color: 'var(--gray-500)' }}>No rooms yet. Create one to start designing.</p>
        )}

        <div className="avrd-grid">
          {(selected.rooms || []).map((room) => (
            <div key={room.id} className="avrd-card" onClick={() => onOpenRoom(selected.id, room.id)}>
              <div className="avrd-card-title">{room.room_name}</div>
              <div className="avrd-card-sub">
                {room.room_type || 'Room'}
                {room.length && room.width ? ` · ${room.length}×${room.width} ${room.units}` : ''}
                {room.quantity > 1 ? ` · ×${room.quantity}` : ''}
              </div>
              <span className="avrd-status-badge">{room.status.replace('_', ' ')}</span>
            </div>
          ))}
        </div>

        {showNewRoom && (
          <NewRoomModal
            onClose={() => setShowNewRoom(false)}
            onCreated={(room) => {
              setShowNewRoom(false);
              loadProject(selected.id);
              onOpenRoom(selected.id, room.id);
            }}
            projectId={selected.id}
          />
        )}
      </div>
    );
  }

  return (
    <div className="avrd-page">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2>AV Design Projects</h2>
        <button className="avrd-btn primary" onClick={() => setShowNewProject(true)}>
          + New Project
        </button>
      </div>

      {projects === null && <p style={{ color: 'var(--gray-500)' }}>Loading…</p>}
      {projects && projects.length === 0 && (
        <p style={{ color: 'var(--gray-500)' }}>No projects yet. Create one to get started.</p>
      )}

      <div className="avrd-grid">
        {(projects || []).map((p) => (
          <div key={p.id} className="avrd-card" onClick={() => loadProject(p.id)}>
            <div className="avrd-card-title">{p.project_name}</div>
            <div className="avrd-card-sub">Updated {new Date(p.updated_at).toLocaleDateString()}</div>
            <span className="avrd-status-badge">{p.status.replace('_', ' ')}</span>
          </div>
        ))}
      </div>

      {showNewProject && (
        <NewProjectModal
          onClose={() => setShowNewProject(false)}
          onCreated={(project) => {
            setShowNewProject(false);
            loadProjects();
            loadProject(project.id);
          }}
        />
      )}
    </div>
  );
}

function NewProjectModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (p: AvDesignProject) => void;
}) {
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const project = await api.post<AvDesignProject>('/api/av_rooms/projects', {
        project_name: name.trim(),
      });
      onCreated(project);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create project');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="avrd-modal-overlay" onClick={onClose}>
      <div className="avrd-modal" onClick={(e) => e.stopPropagation()}>
        <h3>New AV Design Project</h3>
        {error && <div className="avrd-error">{error}</div>}
        <form onSubmit={submit}>
          <div className="avrd-field">
            <label>Project name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} autoFocus required />
          </div>
          <div className="avrd-modal-actions">
            <button type="button" className="avrd-btn" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="avrd-btn primary" disabled={busy}>
              {busy ? 'Creating…' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

const UNIT_OPTIONS: RoomUnits[] = ['mm', 'cm', 'm', 'ft', 'in'];

function NewRoomModal({
  projectId,
  onClose,
  onCreated,
}: {
  projectId: string;
  onClose: () => void;
  onCreated: (r: AvRoom) => void;
}) {
  const [name, setName] = useState('');
  const [roomType, setRoomType] = useState('');
  const [length, setLength] = useState('');
  const [width, setWidth] = useState('');
  const [height, setHeight] = useState('');
  const [units, setUnits] = useState<RoomUnits>('m');
  const [quantity, setQuantity] = useState('1');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const room = await api.post<AvRoom>('/api/av_rooms/rooms', {
        project_id: projectId,
        room_name: name.trim(),
        room_type: roomType.trim() || null,
        length: length ? Number(length) : null,
        width: width ? Number(width) : null,
        height: height ? Number(height) : null,
        units,
        quantity: quantity ? Number(quantity) : 1,
      });
      onCreated(room);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create room');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="avrd-modal-overlay" onClick={onClose}>
      <div className="avrd-modal" onClick={(e) => e.stopPropagation()}>
        <h3>New Room</h3>
        {error && <div className="avrd-error">{error}</div>}
        <form onSubmit={submit}>
          <div className="avrd-field">
            <label>Room name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} autoFocus required />
          </div>
          <div className="avrd-field">
            <label>Room type (optional)</label>
            <input
              value={roomType}
              onChange={(e) => setRoomType(e.target.value)}
              placeholder="e.g. Boardroom, Huddle Room"
            />
          </div>
          <div className="avrd-modal-row">
            <div className="avrd-field" style={{ flex: 1 }}>
              <label>Length</label>
              <input type="number" step="any" value={length} onChange={(e) => setLength(e.target.value)} />
            </div>
            <div className="avrd-field" style={{ flex: 1 }}>
              <label>Width</label>
              <input type="number" step="any" value={width} onChange={(e) => setWidth(e.target.value)} />
            </div>
            <div className="avrd-field" style={{ flex: 1 }}>
              <label>Height</label>
              <input type="number" step="any" value={height} onChange={(e) => setHeight(e.target.value)} />
            </div>
          </div>
          <div className="avrd-modal-row">
            <div className="avrd-field" style={{ flex: 1 }}>
              <label>Units</label>
              <select value={units} onChange={(e) => setUnits(e.target.value as RoomUnits)}>
                {UNIT_OPTIONS.map((u) => (
                  <option key={u} value={u}>
                    {u}
                  </option>
                ))}
              </select>
            </div>
            <div className="avrd-field" style={{ flex: 1 }}>
              <label>Quantity (Room Multiplier)</label>
              <input
                type="number"
                min={1}
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
              />
            </div>
          </div>
          <div className="avrd-modal-actions">
            <button type="button" className="avrd-btn" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="avrd-btn primary" disabled={busy}>
              {busy ? 'Creating…' : 'Create Room'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
