import { useEffect, useMemo, useRef, useState } from 'react';
import { Stage, Layer, Rect, Line, Text, Arc, Circle } from 'react-konva';
import type Konva from 'konva';
import type { AnyRoomObject, AvRoom } from '../types';
import { getObjectKey } from '../types';
import { libraryEntry } from '../deviceLibrary';
import { toMeters, fromMeters } from '../units';
import {
  overlayKind,
  getCameraOverlay,
  getMicOverlay,
  getDisplayOverlay,
  facingToKonvaDegrees,
} from '../overlays';

// Room floorplan convention used throughout this component: `width` is the
// room's X-axis (left-right) extent, `length` is its Y-axis (front-back /
// depth) extent -- matching how the New Room form labels them. Object
// position_x/position_y and width/height/depth are all stored in the
// room's own `units` (per api/av_rooms.py); this component converts to
// meters only internally, purely to compute a consistent pixels-per-meter
// scale for drawing.
const MARGIN_PX = 40;

export default function RoomCanvas2D({
  room,
  objects,
  selectedKey,
  onSelect,
  onMoveObject,
  onDropCategory,
  showOverlays,
}: {
  room: AvRoom;
  objects: AnyRoomObject[];
  selectedKey: string | null;
  onSelect: (key: string | null) => void;
  onMoveObject: (key: string, positionX: number, positionY: number) => void;
  onDropCategory: (category: string, positionX: number, positionY: number) => void;
  showOverlays: boolean;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const box = entries[0].contentRect;
      setSize({ w: box.width, h: box.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const roomWidthM = toMeters(room.width || 4, room.units);
  const roomLengthM = toMeters(room.length || 4, room.units);

  const ppm = useMemo(() => {
    const availW = Math.max(size.w - MARGIN_PX * 2, 50);
    const availH = Math.max(size.h - MARGIN_PX * 2, 50);
    return Math.max(Math.min(availW / roomWidthM, availH / roomLengthM), 1);
  }, [size, roomWidthM, roomLengthM]);

  const roomPxW = roomWidthM * ppm;
  const roomPxH = roomLengthM * ppm;
  const originX = (size.w - roomPxW) / 2;
  const originY = (size.h - roomPxH) / 2;

  // Grid line every 1m (or every 1 room-unit if the room is tiny in meters,
  // e.g. a room measured in mm -- falls back gracefully either way).
  const gridStepM = roomWidthM > 12 || roomLengthM > 12 ? 2 : 1;
  const vLines: number[] = [];
  for (let x = 0; x <= roomWidthM + 0.001; x += gridStepM) vLines.push(x);
  const hLines: number[] = [];
  for (let y = 0; y <= roomLengthM + 0.001; y += gridStepM) hLines.push(y);

  function pxToRoomUnits(px: number, py: number): { x: number; y: number } {
    const meX = (px - originX) / ppm;
    const meY = (py - originY) / ppm;
    return { x: fromMeters(meX, room.units), y: fromMeters(meY, room.units) };
  }

  return (
    <div
      ref={wrapRef}
      className="avrd-canvas-wrap"
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
      }}
      onDrop={(e) => {
        e.preventDefault();
        const category = e.dataTransfer.getData('text/av-device-category');
        if (!category) return;
        const rect = wrapRef.current!.getBoundingClientRect();
        const { x, y } = pxToRoomUnits(e.clientX - rect.left, e.clientY - rect.top);
        onDropCategory(category, x, y);
      }}
    >
      <Stage
        width={size.w}
        height={size.h}
        onMouseDown={(e: Konva.KonvaEventObject<MouseEvent>) => {
          if (e.target === e.target.getStage()) onSelect(null);
        }}
      >
        <Layer>
          {/* Room boundary */}
          <Rect
            x={originX}
            y={originY}
            width={roomPxW}
            height={roomPxH}
            fill="#ffffff"
            stroke="#0f2544"
            strokeWidth={2}
          />
          {/* Grid */}
          {vLines.map((x) => (
            <Line
              key={`v${x}`}
              points={[originX + x * ppm, originY, originX + x * ppm, originY + roomPxH]}
              stroke="#e2e6ec"
              strokeWidth={1}
            />
          ))}
          {hLines.map((y) => (
            <Line
              key={`h${y}`}
              points={[originX, originY + y * ppm, originX + roomPxW, originY + y * ppm]}
              stroke="#e2e6ec"
              strokeWidth={1}
            />
          ))}

          {/* Engineering overlays (camera FOV / mic pickup / display viewing
              zone), drawn below the device shapes so devices stay clickable
              and visually on top. listening={false} on every overlay shape
              so they never intercept clicks meant for the device or the
              stage's own deselect-on-empty-click handler. */}
          {showOverlays &&
            objects.map((obj) => {
              const kind = overlayKind(obj.category as string);
              if (!kind) return null;
              const key = getObjectKey(obj);
              const xM = toMeters(obj.position_x, room.units);
              const yM = toMeters(obj.position_y, room.units);
              const px = originX + xM * ppm;
              const py = originY + yM * ppm;
              const facing = facingToKonvaDegrees(obj.rotation_z || 0);

              if (kind === 'camera') {
                const ov = getCameraOverlay(obj, room.units);
                const rangeM = toMeters(ov.fov_range, room.units);
                return (
                  <Arc
                    key={`${key}-ov`}
                    x={px}
                    y={py}
                    innerRadius={0}
                    outerRadius={Math.max(rangeM * ppm, 1)}
                    angle={ov.fov_h}
                    rotation={facing - ov.fov_h / 2}
                    fill="rgba(37, 99, 235, 0.18)"
                    stroke="rgba(37, 99, 235, 0.4)"
                    strokeWidth={1}
                    listening={false}
                  />
                );
              }
              if (kind === 'mic') {
                const ov = getMicOverlay(obj, room.units);
                const radiusM = toMeters(ov.pickup_radius, room.units);
                return (
                  <Circle
                    key={`${key}-ov`}
                    x={px}
                    y={py}
                    radius={Math.max(radiusM * ppm, 1)}
                    fill="rgba(22, 163, 74, 0.14)"
                    stroke="rgba(22, 163, 74, 0.35)"
                    strokeWidth={1}
                    listening={false}
                  />
                );
              }
              // display
              const ov = getDisplayOverlay(obj, room.units);
              const minM = toMeters(ov.viewing_distance_min, room.units);
              const maxM = toMeters(ov.viewing_distance_max, room.units);
              return (
                <Arc
                  key={`${key}-ov`}
                  x={px}
                  y={py}
                  innerRadius={Math.max(minM * ppm, 1)}
                  outerRadius={Math.max(maxM * ppm, 2)}
                  angle={ov.viewing_angle}
                  rotation={facing - ov.viewing_angle / 2}
                  fill="rgba(217, 119, 6, 0.14)"
                  stroke="rgba(217, 119, 6, 0.35)"
                  strokeWidth={1}
                  listening={false}
                />
              );
            })}

          {objects.map((obj) => {
            const key = getObjectKey(obj);
            const entry = libraryEntry(obj.category as string);
            const wM = toMeters(obj.width ?? fromMeters(entry?.defaultWidth ?? 0.3, room.units), room.units);
            const dM = toMeters(obj.depth ?? fromMeters(entry?.defaultDepth ?? 0.3, room.units), room.units);
            const xM = toMeters(obj.position_x, room.units);
            const yM = toMeters(obj.position_y, room.units);
            const px = originX + xM * ppm - (wM * ppm) / 2;
            const py = originY + yM * ppm - (dM * ppm) / 2;
            const selected = selectedKey === key;

            return (
              <Rect
                key={key}
                x={px}
                y={py}
                width={Math.max(wM * ppm, 6)}
                height={Math.max(dM * ppm, 6)}
                fill={entry?.color || '#64748b'}
                opacity={selected ? 1 : 0.85}
                stroke={selected ? '#1a1f2b' : undefined}
                strokeWidth={selected ? 2 : 0}
                cornerRadius={3}
                draggable
                onClick={() => onSelect(key)}
                onTap={() => onSelect(key)}
                onDragEnd={(e) => {
                  const node = e.target;
                  const centerPx = node.x() + node.width() / 2;
                  const centerPy = node.y() + node.height() / 2;
                  const { x, y } = pxToRoomUnits(centerPx, centerPy);
                  onMoveObject(key, x, y);
                }}
              />
            );
          })}

          {objects.map((obj) => {
            const key = getObjectKey(obj);
            const entry = libraryEntry(obj.category as string);
            const xM = toMeters(obj.position_x, room.units);
            const yM = toMeters(obj.position_y, room.units);
            const dM = toMeters(obj.depth ?? fromMeters(entry?.defaultDepth ?? 0.3, room.units), room.units);
            const px = originX + xM * ppm;
            const py = originY + yM * ppm + (dM * ppm) / 2 + 12;
            return (
              <Text
                key={`${key}-label`}
                x={px}
                y={py}
                text={obj.object_name || entry?.label || obj.category}
                fontSize={11}
                fill="#3a4453"
                offsetX={30}
                listening={false}
              />
            );
          })}
        </Layer>
      </Stage>
    </div>
  );
}
