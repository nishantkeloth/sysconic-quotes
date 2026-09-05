import { useMemo } from 'react';
import type { AnyRoomObject, AvRoom } from '../types';
import { getObjectKey } from '../types';
import { toMeters } from '../units';
import { getCameraOverlay, getMicOverlay, getDisplayOverlay, facingToKonvaDegrees } from '../overlays';

// A lightweight, rule-based "design health check" -- not a full engineering
// validation engine (spec's much larger §-whatever validation section),
// but a first, genuinely useful pass: the kind of things a reviewer would
// actually flag when looking at a finished room (missing gear, coverage
// gaps, overlapping furniture). Runs entirely over data already in memory
// (objects + their overlays.ts metadata) -- no backend call.

type Severity = 'error' | 'warning' | 'pass';
interface Issue {
  severity: Severity;
  title: string;
  detail?: string;
}

const SCORE_PENALTY: Record<Severity, number> = { error: 20, warning: 8, pass: 0 };

function normalizeAngleDiff(a: number): number {
  let d = ((a + 180) % 360 + 360) % 360 - 180;
  return d;
}

function seatPositions(objects: AnyRoomObject[], room: AvRoom): { xM: number; yM: number }[] {
  return objects
    .filter((o) => o.category === 'chair')
    .map((o) => ({ xM: toMeters(o.position_x, room.units), yM: toMeters(o.position_y, room.units) }));
}

function runValidation(room: AvRoom, objects: AnyRoomObject[]): Issue[] {
  const issues: Issue[] = [];

  if (!room.width || !room.length) {
    issues.push({ severity: 'warning', title: 'Room dimensions are not fully set', detail: 'Set width and length for accurate coverage and BOM checks.' });
  }

  const byCategory = (cats: string[]) => objects.filter((o) => cats.includes(o.category as string));
  const cameras = byCategory(['camera', 'document_camera']);
  const displays = byCategory(['display', 'led_wall', 'projection_screen']);
  const mics = byCategory(['ceiling_microphone', 'table_microphone', 'wireless_microphone']);
  const speakers = byCategory(['speaker', 'soundbar']);
  const chairs = seatPositions(objects, room);

  if (cameras.length === 0) {
    issues.push({ severity: 'error', title: 'No camera in the room', detail: 'Video conferencing needs at least one camera.' });
  }
  if (displays.length === 0) {
    issues.push({ severity: 'error', title: 'No display in the room', detail: 'Add a display, LED wall, or projection screen.' });
  }
  if (mics.length === 0) {
    issues.push({ severity: 'error', title: 'No microphone in the room', detail: 'Remote participants won’t be heard without one.' });
  }
  if (speakers.length === 0) {
    issues.push({ severity: 'warning', title: 'No speaker or soundbar in the room', detail: 'Remote audio needs somewhere to play back.' });
  }
  if (chairs.length === 0) {
    issues.push({ severity: 'warning', title: 'No seating placed yet', detail: 'Coverage checks below can’t confirm cameras/mics/displays reach the seats until chairs are placed.' });
  }

  // Camera FOV coverage: for each camera, how many chairs fall inside both
  // its range and its angular field of view.
  cameras.forEach((cam) => {
    if (chairs.length === 0) return;
    const ov = getCameraOverlay(cam, room.units);
    const rangeM = toMeters(ov.fov_range, room.units);
    const camX = toMeters(cam.position_x, room.units);
    const camY = toMeters(cam.position_y, room.units);
    const facing = facingToKonvaDegrees(cam.rotation_z || 0);
    let covered = 0;
    chairs.forEach((c) => {
      const dx = c.xM - camX;
      const dy = c.yM - camY;
      const dist = Math.hypot(dx, dy);
      const angle = (Math.atan2(dy, dx) * 180) / Math.PI;
      const withinAngle = Math.abs(normalizeAngleDiff(angle - facing)) <= ov.fov_h / 2;
      if (dist <= rangeM && withinAngle) covered += 1;
    });
    if (covered < chairs.length) {
      issues.push({
        severity: covered === 0 ? 'error' : 'warning',
        title: `${cam.object_name || 'Camera'}: ${chairs.length - covered} of ${chairs.length} seat(s) outside its field of view`,
        detail: `Field of view ${ov.fov_h.toFixed(0)}°, range ${ov.fov_range}. Widen the angle, increase range, or reposition the camera.`,
      });
    } else {
      issues.push({ severity: 'pass', title: `${cam.object_name || 'Camera'} covers all placed seating` });
    }
  });

  // Mic pickup: omnidirectional, distance-only.
  mics.forEach((mic) => {
    if (chairs.length === 0) return;
    const ov = getMicOverlay(mic, room.units);
    const radiusM = toMeters(ov.pickup_radius, room.units);
    const micX = toMeters(mic.position_x, room.units);
    const micY = toMeters(mic.position_y, room.units);
    const outside = chairs.filter((c) => Math.hypot(c.xM - micX, c.yM - micY) > radiusM).length;
    if (outside > 0) {
      issues.push({
        severity: outside === chairs.length ? 'error' : 'warning',
        title: `${mic.object_name || 'Microphone'}: ${outside} of ${chairs.length} seat(s) outside pickup radius`,
        detail: `Pickup radius ${ov.pickup_radius}. Add another mic or move this one closer to that seating.`,
      });
    } else {
      issues.push({ severity: 'pass', title: `${mic.object_name || 'Microphone'} covers all placed seating` });
    }
  });

  // Display viewing distance + angle.
  displays.forEach((disp) => {
    if (chairs.length === 0) return;
    const ov = getDisplayOverlay(disp, room.units);
    const minM = toMeters(ov.viewing_distance_min, room.units);
    const maxM = toMeters(ov.viewing_distance_max, room.units);
    const dispX = toMeters(disp.position_x, room.units);
    const dispY = toMeters(disp.position_y, room.units);
    const facing = facingToKonvaDegrees(disp.rotation_z || 0);
    let outside = 0;
    chairs.forEach((c) => {
      const dx = c.xM - dispX;
      const dy = c.yM - dispY;
      const dist = Math.hypot(dx, dy);
      const angle = (Math.atan2(dy, dx) * 180) / Math.PI;
      const withinAngle = Math.abs(normalizeAngleDiff(angle - facing)) <= ov.viewing_angle / 2;
      if (dist < minM || dist > maxM || !withinAngle) outside += 1;
    });
    if (outside > 0) {
      issues.push({
        severity: outside === chairs.length ? 'error' : 'warning',
        title: `${disp.object_name || 'Display'}: ${outside} of ${chairs.length} seat(s) outside recommended viewing zone`,
        detail: `Recommended distance ${ov.viewing_distance_min}–${ov.viewing_distance_max}, viewing angle ${ov.viewing_angle.toFixed(0)}°.`,
      });
    } else {
      issues.push({ severity: 'pass', title: `${disp.object_name || 'Display'} viewing zone covers all placed seating` });
    }
  });

  // Overlapping furniture/devices (simple AABB overlap in meters) --
  // catches accidental stacking, not real clearance rules.
  const boxes = objects.map((o) => {
    const xM = toMeters(o.position_x, room.units);
    const yM = toMeters(o.position_y, room.units);
    const wM = toMeters(o.width || 0.3, room.units);
    const dM = toMeters(o.depth || 0.3, room.units);
    return { key: getObjectKey(o), name: o.object_name || (o.category as string), xM, yM, wM, dM };
  });
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i];
      const b = boxes[j];
      const overlapX = Math.abs(a.xM - b.xM) < (a.wM + b.wM) / 2 - 0.03;
      const overlapY = Math.abs(a.yM - b.yM) < (a.dM + b.dM) / 2 - 0.03;
      if (overlapX && overlapY) {
        issues.push({ severity: 'warning', title: `"${a.name}" overlaps "${b.name}"`, detail: 'Move one of them so their footprints don’t collide.' });
      }
    }
  }

  if (issues.length === 0) {
    issues.push({ severity: 'pass', title: 'No issues found' });
  }

  return issues;
}

export default function ValidationPanel({
  room,
  objects,
  onClose,
}: {
  room: AvRoom;
  objects: AnyRoomObject[];
  onClose: () => void;
}) {
  const issues = useMemo(() => runValidation(room, objects), [room, objects]);
  const score = useMemo(() => {
    const penalty = issues.reduce((sum, i) => sum + SCORE_PENALTY[i.severity], 0);
    return Math.max(0, 100 - penalty);
  }, [issues]);
  const errorCount = issues.filter((i) => i.severity === 'error').length;
  const warningCount = issues.filter((i) => i.severity === 'warning').length;

  return (
    <div className="avrd-modal-overlay" onClick={onClose}>
      <div className="avrd-modal" style={{ width: 520 }} onClick={(e) => e.stopPropagation()}>
        <h3>Design Check — {room.room_name}</h3>

        <div className="avrd-validation-score">
          <span className="avrd-validation-score-num">{score}</span>
          <span className="avrd-validation-score-label">
            / 100 design health
            <br />
            {errorCount} error{errorCount === 1 ? '' : 's'}, {warningCount} warning{warningCount === 1 ? '' : 's'}
          </span>
        </div>

        <div>
          {issues.map((issue, i) => (
            <div key={i} className={`avrd-validation-item ${issue.severity}`}>
              <span className="avrd-validation-icon">
                {issue.severity === 'error' ? '✕' : issue.severity === 'warning' ? '⚠' : '✓'}
              </span>
              <div>
                <div className="avrd-validation-title">{issue.title}</div>
                {issue.detail && <div className="avrd-validation-detail">{issue.detail}</div>}
              </div>
            </div>
          ))}
        </div>

        <div className="avrd-modal-actions">
          <button className="avrd-btn primary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
