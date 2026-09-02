import type { AnyRoomObject, RoomUnits } from '../types';
import { libraryEntry } from '../deviceLibrary';
import { UNIT_LABELS } from '../units';

// Right sidebar: editable fields for whichever object is currently
// selected on the canvas. All numeric fields are in the room's own units
// (see RoomCanvas2D's header comment) so no conversion happens here --
// this panel just reads/writes the object's raw stored values.
export default function DevicePropertiesPanel({
  object,
  units,
  onChange,
  onDelete,
}: {
  object: AnyRoomObject | null;
  units: RoomUnits;
  onChange: (patch: Partial<AnyRoomObject>) => void;
  onDelete: () => void;
}) {
  if (!object) {
    return (
      <div className="avrd-sidebar-right">
        <h4>Properties</h4>
        <div className="avrd-empty-panel">Select a device on the canvas, or drag a new one in from the library.</div>
      </div>
    );
  }

  const entry = libraryEntry(object.category as string);
  const unitLabel = UNIT_LABELS[units];

  return (
    <div className="avrd-sidebar-right">
      <h4>{entry?.label || object.category}</h4>

      <div className="avrd-field">
        <label>Name</label>
        <input
          value={object.object_name}
          onChange={(e) => onChange({ object_name: e.target.value })}
        />
      </div>

      <div className="avrd-modal-row">
        <div className="avrd-field" style={{ flex: 1 }}>
          <label>Position X ({unitLabel})</label>
          <input
            type="number"
            step="any"
            value={object.position_x}
            onChange={(e) => onChange({ position_x: Number(e.target.value) })}
          />
        </div>
        <div className="avrd-field" style={{ flex: 1 }}>
          <label>Position Y ({unitLabel})</label>
          <input
            type="number"
            step="any"
            value={object.position_y}
            onChange={(e) => onChange({ position_y: Number(e.target.value) })}
          />
        </div>
      </div>

      <div className="avrd-modal-row">
        <div className="avrd-field" style={{ flex: 1 }}>
          <label>Width ({unitLabel})</label>
          <input
            type="number"
            step="any"
            value={object.width ?? ''}
            placeholder="default"
            onChange={(e) => onChange({ width: e.target.value ? Number(e.target.value) : null })}
          />
        </div>
        <div className="avrd-field" style={{ flex: 1 }}>
          <label>Depth ({unitLabel})</label>
          <input
            type="number"
            step="any"
            value={object.depth ?? ''}
            placeholder="default"
            onChange={(e) => onChange({ depth: e.target.value ? Number(e.target.value) : null })}
          />
        </div>
      </div>

      {(object.category === 'display' ||
        object.category === 'camera' ||
        object.category === 'projector' ||
        object.category === 'ceiling_microphone') && (
        <div className="avrd-field">
          <label>Mounting height ({unitLabel})</label>
          <input
            type="number"
            step="any"
            value={object.mounting_height ?? ''}
            placeholder="e.g. 1.5"
            onChange={(e) =>
              onChange({ mounting_height: e.target.value ? Number(e.target.value) : null })
            }
          />
        </div>
      )}

      <div className="avrd-field">
        <label>Quantity</label>
        <input
          type="number"
          min={1}
          value={object.quantity}
          onChange={(e) => onChange({ quantity: Math.max(1, Number(e.target.value) || 1) })}
        />
      </div>

      <div className="avrd-field">
        <label>Notes</label>
        <input value={object.notes ?? ''} onChange={(e) => onChange({ notes: e.target.value })} />
      </div>

      <button className="avrd-btn danger" style={{ width: '100%', marginTop: 8 }} onClick={onDelete}>
        Remove device
      </button>
    </div>
  );
}
