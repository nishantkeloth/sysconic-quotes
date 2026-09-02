import { useState } from 'react';
import type { AnyRoomObject, RoomUnits } from '../types';
import { getMappedProduct } from '../types';
import { libraryEntry } from '../deviceLibrary';
import { UNIT_LABELS } from '../units';
import ProductPickerModal from './ProductPickerModal';
import type { ProductSearchResult } from '../api/client';
import { overlayKind, getCameraOverlay, getMicOverlay, getDisplayOverlay } from '../overlays';

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
  const [showPicker, setShowPicker] = useState(false);

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
  const mapped = getMappedProduct(object);

  function handleSelectProduct(p: ProductSearchResult) {
    setShowPicker(false);
    onChange({
      product_id: p.id,
      object_name: `${p.brand} ${p.model}`.trim(),
      metadata_json: {
        ...object!.metadata_json,
        mapped_product: {
          product_id: p.id,
          brand: p.brand,
          model: p.model,
          sku: p.sku,
          default_cost: p.default_cost,
          cost_currency: p.cost_currency,
          image_url: p.image_url,
          mapped_at: new Date().toISOString(),
        },
      },
    });
  }

  function handleUnmap() {
    const { mapped_product, ...restMeta } = object!.metadata_json as Record<string, unknown>;
    onChange({ product_id: null, metadata_json: restMeta });
  }

  function patchMeta(patch: Record<string, unknown>) {
    onChange({ metadata_json: { ...object!.metadata_json, ...patch } });
  }

  const kind = overlayKind(object.category as string);

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

      <div className="avrd-field">
        <label>Product</label>
        {mapped ? (
          <div className="avrd-card" style={{ cursor: 'default', padding: '8px 12px' }}>
            <div className="avrd-card-title" style={{ fontSize: 13 }}>
              {mapped.brand} {mapped.model}
            </div>
            <div className="avrd-card-sub">
              {mapped.sku ? `SKU ${mapped.sku} · ` : ''}
              {mapped.default_cost ? `${mapped.cost_currency} ${mapped.default_cost.toLocaleString()}` : 'No cost set'}
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
              <button className="avrd-btn" style={{ flex: 1 }} onClick={() => setShowPicker(true)}>
                Change
              </button>
              <button className="avrd-btn danger" style={{ flex: 1 }} onClick={handleUnmap}>
                Unmap
              </button>
            </div>
          </div>
        ) : (
          <button className="avrd-btn primary" style={{ width: '100%' }} onClick={() => setShowPicker(true)}>
            Map to Product
          </button>
        )}
      </div>

      {showPicker && (
        <ProductPickerModal onClose={() => setShowPicker(false)} onSelect={handleSelectProduct} />
      )}

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

      <div className="avrd-field">
        <label>Height off floor ({unitLabel})</label>
        <input
          type="number"
          step="any"
          value={object.position_z}
          onChange={(e) => onChange({ position_z: Number(e.target.value) })}
        />
        <div style={{ fontSize: 11, color: 'var(--gray-500)', marginTop: 3 }}>
          Where the device's base sits above the floor -- 0 for floor-standing items, table height for
          table devices, wall/ceiling mount height for mounted items. Drives the 3D view's vertical
          placement.
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
        <div className="avrd-field" style={{ flex: 1 }}>
          <label>Height ({unitLabel})</label>
          <input
            type="number"
            step="any"
            value={object.height ?? ''}
            placeholder="default"
            onChange={(e) => onChange({ height: e.target.value ? Number(e.target.value) : null })}
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
        <label>Facing (rotation °)</label>
        <input
          type="number"
          step="any"
          value={object.rotation_z}
          onChange={(e) => onChange({ rotation_z: Number(e.target.value) % 360 })}
        />
      </div>

      {kind === 'camera' && (
        <div className="avrd-modal-row">
          <div className="avrd-field" style={{ flex: 1 }}>
            <label>Field of view (°)</label>
            <input
              type="number"
              step="any"
              value={getCameraOverlay(object, units).fov_h}
              onChange={(e) => patchMeta({ fov_h: Number(e.target.value) })}
            />
          </div>
          <div className="avrd-field" style={{ flex: 1 }}>
            <label>Range ({unitLabel})</label>
            <input
              type="number"
              step="any"
              value={getCameraOverlay(object, units).fov_range}
              onChange={(e) => patchMeta({ fov_range: Number(e.target.value) })}
            />
          </div>
        </div>
      )}

      {kind === 'mic' && (
        <div className="avrd-field">
          <label>Pickup radius ({unitLabel})</label>
          <input
            type="number"
            step="any"
            value={getMicOverlay(object, units).pickup_radius}
            onChange={(e) => patchMeta({ pickup_radius: Number(e.target.value) })}
          />
        </div>
      )}

      {kind === 'display' && (
        <>
          <div className="avrd-modal-row">
            <div className="avrd-field" style={{ flex: 1 }}>
              <label>Min viewing dist. ({unitLabel})</label>
              <input
                type="number"
                step="any"
                value={getDisplayOverlay(object, units).viewing_distance_min}
                onChange={(e) => patchMeta({ viewing_distance_min: Number(e.target.value) })}
              />
            </div>
            <div className="avrd-field" style={{ flex: 1 }}>
              <label>Max viewing dist. ({unitLabel})</label>
              <input
                type="number"
                step="any"
                value={getDisplayOverlay(object, units).viewing_distance_max}
                onChange={(e) => patchMeta({ viewing_distance_max: Number(e.target.value) })}
              />
            </div>
          </div>
          <div className="avrd-field">
            <label>Viewing angle (°)</label>
            <input
              type="number"
              step="any"
              value={getDisplayOverlay(object, units).viewing_angle}
              onChange={(e) => patchMeta({ viewing_angle: Number(e.target.value) })}
            />
          </div>
        </>
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
