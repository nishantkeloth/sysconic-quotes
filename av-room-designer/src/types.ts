// Shared types mirroring api/av_rooms.py's request/response shapes exactly
// (field names match the DB columns in migration-av-room-designer.sql one
// for one) — this file is the single source of truth the canvas, the
// properties panel, and (later) the engineering/BOM services should all
// consume, per the spec's "one common object model" principle.

export type DesignStatus =
  | 'draft'
  | 'concept'
  | 'under_review'
  | 'approved'
  | 'quotation_generated'
  | 'locked';

export type RoomUnits = 'mm' | 'cm' | 'm' | 'ft' | 'in';

export interface AvDesignProject {
  id: string;
  company_id: string;
  project_name: string;
  customer_id: string | null;
  quote_id: string | null;
  deal_id: string | null;
  status: DesignStatus;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  rooms?: AvRoom[];
}

export interface AvRoom {
  id: string;
  company_id: string;
  project_id: string;
  room_name: string;
  room_type: string | null;
  length: number | null;
  width: number | null;
  height: number | null;
  ceiling_height: number | null;
  units: RoomUnits;
  capacity: number | null;
  seating_capacity: number | null;
  quantity: number;
  version: number;
  status: DesignStatus;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  objects?: AvRoomObject[];
}

export type ObjectType = 'device' | 'furniture';

// Matches the device categories enumerated in the spec (section 8), grouped
// the same way for the DeviceLibrary panel.
export type DeviceCategory =
  // Video
  | 'display' | 'led_wall' | 'projector' | 'projection_screen' | 'camera' | 'document_camera'
  // Audio
  | 'ceiling_microphone' | 'table_microphone' | 'wireless_microphone' | 'speaker' | 'soundbar' | 'amplifier'
  // Conferencing
  | 'teams_room_device' | 'zoom_room_device' | 'codec' | 'collaboration_bar'
  // Control
  | 'touch_panel' | 'control_processor' | 'keypad' | 'scheduling_panel'
  // Infrastructure
  | 'rack' | 'network_switch' | 'floor_box' | 'wall_box' | 'cable_tray'
  | 'hdmi_outlet' | 'usb_outlet' | 'network_outlet'
  // Furniture
  | 'table' | 'chair' | 'podium' | 'credenza' | 'cabinet';

export interface AvRoomObject {
  id: string;
  company_id: string;
  room_id: string;
  object_type: ObjectType;
  category: DeviceCategory | string;
  object_name: string;
  product_id: string | null;
  position_x: number;
  position_y: number;
  position_z: number;
  rotation_x: number;
  rotation_y: number;
  rotation_z: number;
  width: number | null;
  height: number | null;
  depth: number | null;
  mounting_height: number | null;
  mounting_type: string | null;
  quantity: number;
  notes: string | null;
  metadata_json: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

// A locally-created object before it's been saved (no id/company_id/room_id
// yet — those get assigned server-side on the next save_objects PUT, which
// is a full-replace so the client is always the source of truth in between
// saves; see api/av_rooms.py save_objects()).
export type DraftRoomObject = Omit<
  AvRoomObject,
  'id' | 'company_id' | 'room_id' | 'created_at' | 'updated_at'
> & { _localId: string };

export type AnyRoomObject = AvRoomObject | DraftRoomObject;

export interface DeviceLibraryEntry {
  category: DeviceCategory;
  label: string;
  objectType: ObjectType;
  group: string;
  defaultWidth: number;  // meters, footprint on the 2D plan
  defaultDepth: number;
  color: string;
}

// A saved object has a server-assigned `id`; a draft only has `_localId`.
// Used as the React key and as the selection identifier everywhere the
// canvas/properties panel need to refer to "this object" regardless of
// whether it's been persisted yet.
export function getObjectKey(o: AnyRoomObject): string {
  return 'id' in o && o.id ? o.id : (o as DraftRoomObject)._localId;
}
