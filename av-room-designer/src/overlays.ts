import type { AnyRoomObject, RoomUnits } from './types';
import { fromMeters } from './units';

// Engineering overlays: camera field-of-view cones, microphone pickup
// radii, and display viewing-distance zones, drawn directly on the 2D
// canvas -- the AVIO-style feature the room designer is chasing. All
// parameters live in the object's own metadata_json (the escape hatch
// migration-av-room-designer.sql was explicitly designed to hold this kind
// of device-type-specific data without further schema migrations), stored
// in the room's own `units` for consistency with position/width/depth.
//
// IMPORTANT: the default numbers below are illustrative starting points
// for visualization only -- NOT real optical/acoustic engineering specs
// pulled from any datasheet. products.specs is free-text/unstructured
// today (see types.ts's MappedProduct comment), so there's no structured
// per-model FOV/pickup-radius data to pull from yet. A later phase could
// populate these from real product specs once that data is structured;
// for now every value here is user-editable in the properties panel.

export type OverlayKind = 'camera' | 'mic' | 'display' | null;

const CAMERA_CATEGORIES = new Set(['camera', 'document_camera']);
const MIC_CATEGORIES = new Set(['ceiling_microphone', 'table_microphone', 'wireless_microphone']);
const DISPLAY_CATEGORIES = new Set(['display', 'led_wall', 'projection_screen']);

export function overlayKind(category: string): OverlayKind {
  if (CAMERA_CATEGORIES.has(category)) return 'camera';
  if (MIC_CATEGORIES.has(category)) return 'mic';
  if (DISPLAY_CATEGORIES.has(category)) return 'display';
  return null;
}

export interface CameraOverlay {
  fov_h: number; // degrees
  fov_range: number; // room units
}
export interface MicOverlay {
  pickup_radius: number; // room units
}
export interface DisplayOverlay {
  viewing_distance_min: number; // room units
  viewing_distance_max: number; // room units
  viewing_angle: number; // degrees
}

export function defaultCameraOverlay(units: RoomUnits): CameraOverlay {
  return { fov_h: 90, fov_range: fromMeters(6, units) };
}
export function defaultMicOverlay(units: RoomUnits): MicOverlay {
  return { pickup_radius: fromMeters(3, units) };
}
export function defaultDisplayOverlay(units: RoomUnits): DisplayOverlay {
  return { viewing_distance_min: fromMeters(1.5, units), viewing_distance_max: fromMeters(4.5, units), viewing_angle: 150 };
}

// Each getter falls back to the illustrative default whenever the specific
// field is missing -- true for every object created before this feature
// shipped, and for any object whose metadata_json was never touched since.
export function getCameraOverlay(o: AnyRoomObject, units: RoomUnits): CameraOverlay {
  const m = (o.metadata_json || {}) as Partial<CameraOverlay>;
  const d = defaultCameraOverlay(units);
  return { fov_h: m.fov_h ?? d.fov_h, fov_range: m.fov_range ?? d.fov_range };
}
export function getMicOverlay(o: AnyRoomObject, units: RoomUnits): MicOverlay {
  const m = (o.metadata_json || {}) as Partial<MicOverlay>;
  const d = defaultMicOverlay(units);
  return { pickup_radius: m.pickup_radius ?? d.pickup_radius };
}
export function getDisplayOverlay(o: AnyRoomObject, units: RoomUnits): DisplayOverlay {
  const m = (o.metadata_json || {}) as Partial<DisplayOverlay>;
  const d = defaultDisplayOverlay(units);
  return {
    viewing_distance_min: m.viewing_distance_min ?? d.viewing_distance_min,
    viewing_distance_max: m.viewing_distance_max ?? d.viewing_distance_max,
    viewing_angle: m.viewing_angle ?? d.viewing_angle,
  };
}

// Konva's Arc angle=0 points along +X (east) and sweeps clockwise. This
// app's own "facing" convention (rotation_z, an existing field on every
// object) uses 0deg = facing +Y ("south" / down the room -- e.g. a device
// mounted on the room's "north" wall facing into the room). Since Konva's
// Y-down canvas clockwise sweep already matches our screen-space
// convention, converting one to the other is just a +90 offset.
export function facingToKonvaDegrees(rotationZ: number): number {
  return rotationZ + 90;
}
