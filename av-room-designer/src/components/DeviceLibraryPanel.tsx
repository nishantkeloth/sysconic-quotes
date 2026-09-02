import { DEVICE_LIBRARY, DEVICE_LIBRARY_GROUPS } from '../deviceLibrary';
import type { DeviceCategory } from '../types';

// Left sidebar: static, grouped list of every device/furniture type from
// the spec's section 8 catalog. Uses native HTML5 drag-and-drop (dataTransfer
// carries the category string) so RoomCanvas2D's plain <div> drop target
// doesn't need any Konva-specific drag machinery -- see onDrop in
// RoomDesignerPage.
export default function DeviceLibraryPanel() {
  return (
    <div className="avrd-sidebar-left">
      {DEVICE_LIBRARY_GROUPS.map((group) => (
        <div key={group}>
          <div className="avrd-lib-group">{group}</div>
          {DEVICE_LIBRARY.filter((d) => d.group === group).map((d) => (
            <div
              key={d.category}
              className="avrd-lib-item"
              draggable
              onDragStart={(e) => {
                e.dataTransfer.setData('text/av-device-category', d.category as DeviceCategory);
                e.dataTransfer.effectAllowed = 'copy';
              }}
              title={`Drag onto the room to place a ${d.label}`}
            >
              <span className="avrd-lib-swatch" style={{ background: d.color }} />
              {d.label}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
