import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
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
const SNAP_STEP_M = 0.1; // 10cm -- independent of the visual grid spacing below
const ZOOM_MIN = 0.3;
const ZOOM_MAX = 4;
const ZOOM_STEP = 1.08;

export default function RoomCanvas2D({
  room,
  objects,
  selectedKey,
  onSelect,
  onMoveObject,
  onDropCategory,
  onBeginEdit,
  showOverlays,
  showDimensions,
  snapToGrid,
  stageRef,
}: {
  room: AvRoom;
  objects: AnyRoomObject[];
  selectedKey: string | null;
  onSelect: (key: string | null) => void;
  onMoveObject: (key: string, positionX: number, positionY: number) => void;
  onDropCategory: (category: string, positionX: number, positionY: number) => void;
  onBeginEdit?: () => void;
  showOverlays: boolean;
  showDimensions?: boolean;
  snapToGrid?: boolean;
  stageRef?: React.MutableRefObject<Konva.Stage | null>;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  // Stage-level zoom/pan -- purely a view transform. Every existing
  // px<->room-units conversion below stays in Stage-LOCAL (unscaled,
  // unpanned) coordinates and is untouched by this; Konva applies the
  // scale/position transform for free at render time. The one place that
  // genuinely needs to account for it is the native HTML5 drag-and-drop
  // handler below, since e.clientX/clientY are real screen pixels.
  const [zoom, setZoom] = useState(1);
  const [stagePos, setStagePos] = useState({ x: 0, y: 0 });

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

  function handleWheel(e: Konva.KonvaEventObject<WheelEvent>) {
    e.evt.preventDefault();
    const stage = e.target.getStage();
    if (!stage) return;
    const pointer = stage.getPointerPosition();
    if (!pointer) return;
    const mousePointTo = {
      x: (pointer.x - stagePos.x) / zoom,
      y: (pointer.y - stagePos.y) / zoom,
    };
    const direction = e.evt.deltaY > 0 ? -1 : 1;
    const newZoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, direction > 0 ? zoom * ZOOM_STEP : zoom / ZOOM_STEP));
    setZoom(newZoom);
    setStagePos({
      x: pointer.x - mousePointTo.x * newZoom,
      y: pointer.y - mousePointTo.y * newZoom,
    });
  }

  function zoomBy(factor: number) {
    const center = { x: size.w / 2, y: size.h / 2 };
    const mousePointTo = { x: (center.x - stagePos.x) / zoom, y: (center.y - stagePos.y) / zoom };
    const newZoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, zoom * factor));
    setZoom(newZoom);
    setStagePos({ x: center.x - mousePointTo.x * newZoom, y: center.y - mousePointTo.y * newZoom });
  }

  function resetView() {
    setZoom(1);
    setStagePos({ x: 0, y: 0 });
  }

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
        // e.clientX/Y are real screen pixels -- undo the Stage's own
        // zoom/pan transform first to get back to the Stage-local
        // coordinates pxToRoomUnits expects (see its own comment above).
        const screenX = e.clientX - rect.left;
        const screenY = e.clientY - rect.top;
        const localX = (screenX - stagePos.x) / zoom;
        const localY = (screenY - stagePos.y) / zoom;
        const { x, y } = pxToRoomUnits(localX, localY);
        onDropCategory(category, x, y);
      }}
    >
      <Stage
        ref={stageRef}
        width={size.w}
        height={size.h}
        scaleX={zoom}
        scaleY={zoom}
        x={stagePos.x}
        y={stagePos.y}
        draggable
        onWheel={handleWheel}
        onDragEnd={(e) => {
          if (e.target === e.target.getStage()) setStagePos({ x: e.target.x(), y: e.target.y() });
        }}
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
                onDragStart={() => onBeginEdit?.()}
                onDragEnd={(e) => {
                  const node = e.target;
                  const centerPx = node.x() + node.width() / 2;
                  const centerPy = node.y() + node.height() / 2;
                  let { x, y } = pxToRoomUnits(centerPx, centerPy);
                  if (snapToGrid) {
                    const step = fromMeters(SNAP_STEP_M, room.units);
                    x = Math.round(x / step) * step;
                    y = Math.round(y / step) * step;
                  }
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

          {/* Installation dimension line (spec §15): for the selected
              object only, the shortest distance from its footprint to the
              nearest wall on each axis, styled like the AVIO reference's
              red "0.84m" measurement line. Always labeled in meters
              regardless of the room's configured units, for a single
              consistent, at-a-glance scale. */}
          {showDimensions &&
            selectedKey &&
            (() => {
              const obj = objects.find((o) => getObjectKey(o) === selectedKey);
              if (!obj) return null;
              const entry = libraryEntry(obj.category as string);
              const wM = toMeters(obj.width ?? fromMeters(entry?.defaultWidth ?? 0.3, room.units), room.units);
              const dM = toMeters(obj.depth ?? fromMeters(entry?.defaultDepth ?? 0.3, room.units), room.units);
              const xM = toMeters(obj.position_x, room.units);
              const yM = toMeters(obj.position_y, room.units);
              const left = xM - wM / 2;
              const right = roomWidthM - (xM + wM / 2);
              const top = yM - dM / 2;
              const bottom = roomLengthM - (yM + dM / 2);

              const segs: { axis: 'h' | 'v'; value: number; from: number; to: number; fixed: number }[] = [];
              if (left <= right) segs.push({ axis: 'h', value: left, from: 0, to: xM - wM / 2, fixed: yM });
              else segs.push({ axis: 'h', value: right, from: xM + wM / 2, to: roomWidthM, fixed: yM });
              if (top <= bottom) segs.push({ axis: 'v', value: top, from: 0, to: yM - dM / 2, fixed: xM });
              else segs.push({ axis: 'v', value: bottom, from: yM + dM / 2, to: roomLengthM, fixed: xM });

              return segs.map((seg, i) => {
                if (seg.value < 0.02) return null; // touching the wall already -- label would just clutter
                if (seg.axis === 'h') {
                  const py = originY + seg.fixed * ppm;
                  const px1 = originX + seg.from * ppm;
                  const px2 = originX + seg.to * ppm;
                  return (
                    <Fragment key={`dim-h-${i}`}>
                      <Line points={[px1, py, px2, py]} stroke="#dc2626" strokeWidth={1.5} dash={[4, 3]} listening={false} />
                      <Line points={[px1, py - 5, px1, py + 5]} stroke="#dc2626" strokeWidth={1.5} listening={false} />
                      <Line points={[px2, py - 5, px2, py + 5]} stroke="#dc2626" strokeWidth={1.5} listening={false} />
                      <Text
                        x={(px1 + px2) / 2 - 20}
                        y={py - 16}
                        text={`${seg.value.toFixed(2)}m`}
                        fontSize={11}
                        fontStyle="bold"
                        fill="#dc2626"
                        listening={false}
                      />
                    </Fragment>
                  );
                }
                const px = originX + seg.fixed * ppm;
                const py1 = originY + seg.from * ppm;
                const py2 = originY + seg.to * ppm;
                return (
                  <Fragment key={`dim-v-${i}`}>
                    <Line points={[px, py1, px, py2]} stroke="#dc2626" strokeWidth={1.5} dash={[4, 3]} listening={false} />
                    <Line points={[px - 5, py1, px + 5, py1]} stroke="#dc2626" strokeWidth={1.5} listening={false} />
                    <Line points={[px - 5, py2, px + 5, py2]} stroke="#dc2626" strokeWidth={1.5} listening={false} />
                    <Text
                      x={px + 6}
                      y={(py1 + py2) / 2 - 6}
                      text={`${seg.value.toFixed(2)}m`}
                      fontSize={11}
                      fontStyle="bold"
                      fill="#dc2626"
                      listening={false}
                    />
                  </Fragment>
                );
              });
            })()}
        </Layer>
      </Stage>

      <div className="avrd-zoom-controls">
        <button onClick={() => zoomBy(1 / ZOOM_STEP)} title="Zoom out">
          −
        </button>
        <span className="avrd-zoom-label">{Math.round(zoom * 100)}%</span>
        <button onClick={() => zoomBy(ZOOM_STEP)} title="Zoom in">
          +
        </button>
        <button onClick={resetView} title="Reset zoom/pan">
          ⤾
        </button>
      </div>
    </div>
  );
}
