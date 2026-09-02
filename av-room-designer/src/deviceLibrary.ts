import type { DeviceLibraryEntry } from './types';

// Matches the categories/groupings from the spec's section 8 device panel.
// defaultWidth/defaultDepth/defaultHeight are rough real-world footprints
// in meters, used to size the icon on the 2D plan and the box in the 3D
// view (RoomViewer3D) -- actual dimensions come from the mapped product
// once one is selected (spec section 9), once product specs are structured
// enough to provide them (they aren't yet -- see types.ts's MappedProduct
// comment).
export const DEVICE_LIBRARY: DeviceLibraryEntry[] = [
  // Video
  { category: 'display', label: 'Display', objectType: 'device', group: 'Video', defaultWidth: 1.2, defaultDepth: 0.1, defaultHeight: 0.75, color: '#2563eb' },
  { category: 'led_wall', label: 'LED Wall', objectType: 'device', group: 'Video', defaultWidth: 3, defaultDepth: 0.2, defaultHeight: 2, color: '#1d4ed8' },
  { category: 'projector', label: 'Projector', objectType: 'device', group: 'Video', defaultWidth: 0.4, defaultDepth: 0.3, defaultHeight: 0.15, color: '#3b82f6' },
  { category: 'projection_screen', label: 'Projection Screen', objectType: 'device', group: 'Video', defaultWidth: 2.4, defaultDepth: 0.1, defaultHeight: 1.8, color: '#60a5fa' },
  { category: 'camera', label: 'Camera', objectType: 'device', group: 'Video', defaultWidth: 0.2, defaultDepth: 0.2, defaultHeight: 0.15, color: '#0ea5e9' },
  { category: 'document_camera', label: 'Document Camera', objectType: 'device', group: 'Video', defaultWidth: 0.2, defaultDepth: 0.2, defaultHeight: 0.3, color: '#38bdf8' },

  // Audio
  { category: 'ceiling_microphone', label: 'Ceiling Microphone', objectType: 'device', group: 'Audio', defaultWidth: 0.3, defaultDepth: 0.3, defaultHeight: 0.05, color: '#16a34a' },
  { category: 'table_microphone', label: 'Table Microphone', objectType: 'device', group: 'Audio', defaultWidth: 0.15, defaultDepth: 0.15, defaultHeight: 0.1, color: '#22c55e' },
  { category: 'wireless_microphone', label: 'Wireless Microphone', objectType: 'device', group: 'Audio', defaultWidth: 0.1, defaultDepth: 0.1, defaultHeight: 0.05, color: '#4ade80' },
  { category: 'speaker', label: 'Speaker', objectType: 'device', group: 'Audio', defaultWidth: 0.25, defaultDepth: 0.25, defaultHeight: 0.3, color: '#15803d' },
  { category: 'soundbar', label: 'Soundbar', objectType: 'device', group: 'Audio', defaultWidth: 0.9, defaultDepth: 0.1, defaultHeight: 0.1, color: '#166534' },
  { category: 'amplifier', label: 'Amplifier', objectType: 'device', group: 'Audio', defaultWidth: 0.45, defaultDepth: 0.4, defaultHeight: 0.15, color: '#14532d' },

  // Conferencing
  { category: 'teams_room_device', label: 'Teams Room Device', objectType: 'device', group: 'Conferencing', defaultWidth: 0.3, defaultDepth: 0.1, defaultHeight: 0.1, color: '#7c3aed' },
  { category: 'zoom_room_device', label: 'Zoom Room Device', objectType: 'device', group: 'Conferencing', defaultWidth: 0.3, defaultDepth: 0.1, defaultHeight: 0.1, color: '#8b5cf6' },
  { category: 'codec', label: 'Codec', objectType: 'device', group: 'Conferencing', defaultWidth: 0.4, defaultDepth: 0.3, defaultHeight: 0.1, color: '#a78bfa' },
  { category: 'collaboration_bar', label: 'Collaboration Bar', objectType: 'device', group: 'Conferencing', defaultWidth: 0.6, defaultDepth: 0.1, defaultHeight: 0.1, color: '#c4b5fd' },

  // Control
  { category: 'touch_panel', label: 'Touch Panel', objectType: 'device', group: 'Control', defaultWidth: 0.25, defaultDepth: 0.05, defaultHeight: 0.15, color: '#d97706' },
  { category: 'control_processor', label: 'Control Processor', objectType: 'device', group: 'Control', defaultWidth: 0.45, defaultDepth: 0.4, defaultHeight: 0.15, color: '#f59e0b' },
  { category: 'keypad', label: 'Keypad', objectType: 'device', group: 'Control', defaultWidth: 0.1, defaultDepth: 0.05, defaultHeight: 0.1, color: '#fbbf24' },
  { category: 'scheduling_panel', label: 'Scheduling Panel', objectType: 'device', group: 'Control', defaultWidth: 0.2, defaultDepth: 0.05, defaultHeight: 0.15, color: '#fcd34d' },

  // Infrastructure
  { category: 'rack', label: 'Rack', objectType: 'device', group: 'Infrastructure', defaultWidth: 0.6, defaultDepth: 0.8, defaultHeight: 1.6, color: '#475569' },
  { category: 'network_switch', label: 'Network Switch', objectType: 'device', group: 'Infrastructure', defaultWidth: 0.45, defaultDepth: 0.3, defaultHeight: 0.1, color: '#64748b' },
  { category: 'floor_box', label: 'Floor Box', objectType: 'device', group: 'Infrastructure', defaultWidth: 0.2, defaultDepth: 0.2, defaultHeight: 0.05, color: '#94a3b8' },
  { category: 'wall_box', label: 'Wall Box', objectType: 'device', group: 'Infrastructure', defaultWidth: 0.15, defaultDepth: 0.08, defaultHeight: 0.1, color: '#94a3b8' },
  { category: 'cable_tray', label: 'Cable Tray', objectType: 'device', group: 'Infrastructure', defaultWidth: 1, defaultDepth: 0.2, defaultHeight: 0.1, color: '#a1a1aa' },
  { category: 'hdmi_outlet', label: 'HDMI Outlet', objectType: 'device', group: 'Infrastructure', defaultWidth: 0.1, defaultDepth: 0.05, defaultHeight: 0.1, color: '#a1a1aa' },
  { category: 'usb_outlet', label: 'USB Outlet', objectType: 'device', group: 'Infrastructure', defaultWidth: 0.1, defaultDepth: 0.05, defaultHeight: 0.1, color: '#a1a1aa' },
  { category: 'network_outlet', label: 'Network Outlet', objectType: 'device', group: 'Infrastructure', defaultWidth: 0.1, defaultDepth: 0.05, defaultHeight: 0.1, color: '#a1a1aa' },

  // Furniture
  { category: 'table', label: 'Table', objectType: 'furniture', group: 'Furniture', defaultWidth: 2.4, defaultDepth: 1.2, defaultHeight: 0.75, color: '#92400e' },
  { category: 'chair', label: 'Chair', objectType: 'furniture', group: 'Furniture', defaultWidth: 0.5, defaultDepth: 0.5, defaultHeight: 0.9, color: '#b45309' },
  { category: 'podium', label: 'Podium', objectType: 'furniture', group: 'Furniture', defaultWidth: 0.6, defaultDepth: 0.5, defaultHeight: 1.1, color: '#78350f' },
  { category: 'credenza', label: 'Credenza', objectType: 'furniture', group: 'Furniture', defaultWidth: 1.8, defaultDepth: 0.5, defaultHeight: 0.8, color: '#a16207' },
  { category: 'cabinet', label: 'Cabinet', objectType: 'furniture', group: 'Furniture', defaultWidth: 0.9, defaultDepth: 0.5, defaultHeight: 1, color: '#854d0e' },
];

export const DEVICE_LIBRARY_GROUPS = Array.from(new Set(DEVICE_LIBRARY.map((d) => d.group)));

export function libraryEntry(category: string): DeviceLibraryEntry | undefined {
  return DEVICE_LIBRARY.find((d) => d.category === category);
}
