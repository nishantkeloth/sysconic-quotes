import type { RoomUnits } from './types';

// Room dimensions and object positions are both stored in whatever unit the
// room itself is set to (spec section 5's `units` field) — there's no
// separate "always meters internally" conversion happening server-side.
// The canvas needs a single consistent unit to convert to pixels though, so
// these helpers convert to/from meters purely for on-screen scaling; the
// values saved back via the API stay in the room's own `units`.
const TO_METERS: Record<RoomUnits, number> = {
  mm: 0.001,
  cm: 0.01,
  m: 1,
  ft: 0.3048,
  in: 0.0254,
};

export function toMeters(value: number, units: RoomUnits): number {
  return value * TO_METERS[units];
}

export function fromMeters(value: number, units: RoomUnits): number {
  return value / TO_METERS[units];
}

export const UNIT_LABELS: Record<RoomUnits, string> = {
  mm: 'mm',
  cm: 'cm',
  m: 'm',
  ft: 'ft',
  in: 'in',
};
