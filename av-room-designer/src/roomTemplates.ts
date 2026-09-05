// Starter layouts for common AV room types (spec's "13 predefined room
// templates" -- this ships a representative first set, not all 13; more
// can be added to this array later without touching anything else, since
// NewRoomModal just maps over ROOM_TEMPLATES). Picking one at room-creation
// time pre-fills the room's dimensions/type AND seeds a starter device
// layout via the same PUT /api/av_rooms/rooms/:id/objects the designer's
// own autosave uses -- no backend change needed.
//
// All templates are authored directly in meters with room units fixed to
// 'm' (co-ordinates below assume that), front wall at y=0 (where the
// display/screen goes) and the room extending in +y toward the back wall.
// Every position here is a reasonable starting point, not a validated
// engineering layout -- the whole point of a template is that it's faster
// to nudge into place than to build from an empty room.

export interface RoomTemplateDevice {
  category: string;
  x: number; // meters
  y: number; // meters
  z?: number; // meters, height off floor -- omit for floor-standing items
  rotationZ?: number; // degrees, 0 = facing +y (matches DevicePropertiesPanel's "Facing" field)
}

export interface RoomTemplate {
  id: string;
  label: string;
  description: string;
  roomType: string;
  length: number; // meters (the room's y/depth extent)
  width: number; // meters (the room's x extent)
  height: number; // meters (ceiling height)
  capacity: number;
  devices: RoomTemplateDevice[];
}

export const ROOM_TEMPLATES: RoomTemplate[] = [
  {
    id: 'huddle',
    label: 'Huddle Room',
    description: '2–4 people · one display, one camera',
    roomType: 'Huddle Room',
    length: 3,
    width: 2.5,
    height: 2.7,
    capacity: 4,
    devices: [
      { category: 'display', x: 1.25, y: 0.08, z: 1.0 },
      { category: 'camera', x: 1.25, y: 0.15, z: 1.75 },
      { category: 'ceiling_microphone', x: 1.25, y: 1.5, z: 2.6 },
      { category: 'speaker', x: 0.3, y: 0.3, z: 2.4 },
      { category: 'table', x: 1.25, y: 1.7, rotationZ: 0 },
      { category: 'chair', x: 0.75, y: 2.3, rotationZ: 180 },
      { category: 'chair', x: 1.75, y: 2.3, rotationZ: 180 },
    ],
  },
  {
    id: 'small-conf',
    label: 'Small Conference Room',
    description: '6–8 people · display, camera, dual mics',
    roomType: 'Small Conference Room',
    length: 4.5,
    width: 3.5,
    height: 2.8,
    capacity: 8,
    devices: [
      { category: 'display', x: 1.75, y: 0.08, z: 1.0 },
      { category: 'camera', x: 1.75, y: 0.15, z: 1.8 },
      { category: 'codec', x: 1.75, y: 0.15, z: 1.4 },
      { category: 'ceiling_microphone', x: 1.75, y: 1.6, z: 2.7 },
      { category: 'ceiling_microphone', x: 1.75, y: 3.0, z: 2.7 },
      { category: 'soundbar', x: 1.75, y: 0.1, z: 0.5 },
      { category: 'table', x: 1.75, y: 2.25, rotationZ: 0 },
      { category: 'chair', x: 0.9, y: 1.5, rotationZ: 90 },
      { category: 'chair', x: 0.9, y: 2.25, rotationZ: 90 },
      { category: 'chair', x: 0.9, y: 3.0, rotationZ: 90 },
      { category: 'chair', x: 2.6, y: 1.5, rotationZ: 270 },
      { category: 'chair', x: 2.6, y: 2.25, rotationZ: 270 },
      { category: 'chair', x: 2.6, y: 3.0, rotationZ: 270 },
      { category: 'touch_panel', x: 3.35, y: 0.4, z: 1.1 },
    ],
  },
  {
    id: 'boardroom',
    label: 'Boardroom',
    description: '12–16 people · dual displays, multiple cameras/mics',
    roomType: 'Boardroom',
    length: 8,
    width: 5,
    height: 3,
    capacity: 16,
    devices: [
      { category: 'display', x: 1.6, y: 0.08, z: 1.0 },
      { category: 'display', x: 3.4, y: 0.08, z: 1.0 },
      { category: 'camera', x: 2.5, y: 0.15, z: 1.9 },
      { category: 'codec', x: 2.5, y: 0.15, z: 1.4 },
      { category: 'rack', x: 4.6, y: 0.4, z: 0 },
      { category: 'credenza', x: 2.5, y: 0.3, z: 0 },
      { category: 'ceiling_microphone', x: 2.5, y: 1.8, z: 2.9 },
      { category: 'ceiling_microphone', x: 2.5, y: 4, z: 2.9 },
      { category: 'ceiling_microphone', x: 2.5, y: 6.2, z: 2.9 },
      { category: 'soundbar', x: 2.5, y: 0.1, z: 0.5 },
      { category: 'table', x: 2.5, y: 4, rotationZ: 0 },
      { category: 'chair', x: 1.3, y: 1.5, rotationZ: 90 },
      { category: 'chair', x: 1.3, y: 2.5, rotationZ: 90 },
      { category: 'chair', x: 1.3, y: 3.5, rotationZ: 90 },
      { category: 'chair', x: 1.3, y: 4.5, rotationZ: 90 },
      { category: 'chair', x: 1.3, y: 5.5, rotationZ: 90 },
      { category: 'chair', x: 1.3, y: 6.5, rotationZ: 90 },
      { category: 'chair', x: 3.7, y: 1.5, rotationZ: 270 },
      { category: 'chair', x: 3.7, y: 2.5, rotationZ: 270 },
      { category: 'chair', x: 3.7, y: 3.5, rotationZ: 270 },
      { category: 'chair', x: 3.7, y: 4.5, rotationZ: 270 },
      { category: 'chair', x: 3.7, y: 5.5, rotationZ: 270 },
      { category: 'chair', x: 3.7, y: 6.5, rotationZ: 270 },
      { category: 'touch_panel', x: 4.6, y: 0.4, z: 1.1 },
    ],
  },
  {
    id: 'training',
    label: 'Training Room',
    description: '20+ people · projection screen, podium, rows of seating',
    roomType: 'Training Room',
    length: 9,
    width: 7,
    height: 3.2,
    capacity: 24,
    devices: [
      { category: 'projection_screen', x: 3.5, y: 0.08, z: 1.2 },
      { category: 'projector', x: 3.5, y: 6.5, z: 2.9, rotationZ: 180 },
      { category: 'podium', x: 1, y: 0.8, rotationZ: 90 },
      { category: 'camera', x: 3.5, y: 0.2, z: 1.9 },
      { category: 'ceiling_microphone', x: 3.5, y: 2.5, z: 3.1 },
      { category: 'ceiling_microphone', x: 3.5, y: 5, z: 3.1 },
      { category: 'speaker', x: 0.4, y: 0.4, z: 2.8 },
      { category: 'speaker', x: 6.6, y: 0.4, z: 2.8 },
      { category: 'table', x: 1.75, y: 2.2, rotationZ: 0 },
      { category: 'table', x: 3.5, y: 2.2, rotationZ: 0 },
      { category: 'table', x: 5.25, y: 2.2, rotationZ: 0 },
      { category: 'chair', x: 1.75, y: 3, rotationZ: 180 },
      { category: 'chair', x: 3.5, y: 3, rotationZ: 180 },
      { category: 'chair', x: 5.25, y: 3, rotationZ: 180 },
      { category: 'table', x: 1.75, y: 4.4, rotationZ: 0 },
      { category: 'table', x: 3.5, y: 4.4, rotationZ: 0 },
      { category: 'table', x: 5.25, y: 4.4, rotationZ: 0 },
      { category: 'chair', x: 1.75, y: 5.2, rotationZ: 180 },
      { category: 'chair', x: 3.5, y: 5.2, rotationZ: 180 },
      { category: 'chair', x: 5.25, y: 5.2, rotationZ: 180 },
    ],
  },
];
