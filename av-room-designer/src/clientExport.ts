import Konva from 'konva';
import type { AnyRoomObject, AvRoom } from './types';
import { toMeters, fromMeters } from './units';
import { libraryEntry } from './deviceLibrary';
import { triggerDownload, dataUrlToBlob } from './exportUtils';

// A purpose-built export, deliberately separate from the interactive
// editing canvas (RoomCanvas2D): the feedback that drove this file was
// "the output isn't good enough to send to a client" -- the live editor's
// screenshot always looked like a screenshot of an editing tool (grid
// lines everywhere, no title, no legend, arbitrary zoom/pan state) rather
// than something meant to be read cold, on its own, by someone who wasn't
// in the room while it was built. This renders a fresh, print-oriented
// page from scratch on an offscreen Konva stage (never attached to the
// visible DOM) with a title block, an equipment schedule, and overall
// dimensions, then exports it as a single high-res PNG.

const PAGE_W = 1800;
const PAGE_H = 1273; // ~ISO landscape page proportions at print resolution
const MARGIN = 56;
const HEADER_H = 92;
const LEGEND_W = 380;
const FOOTER_H = 34;

function numberBadge(layer: Konva.Layer, cx: number, cy: number, num: number, radius = 11) {
  layer.add(new Konva.Circle({ x: cx, y: cy, radius, fill: '#ffffff', stroke: '#0f2544', strokeWidth: 1.5 }));
  layer.add(
    new Konva.Text({
      x: cx - radius,
      y: cy - 6,
      width: radius * 2,
      align: 'center',
      text: String(num),
      fontSize: 11,
      fontStyle: 'bold',
      fill: '#0f2544',
    })
  );
}

export function exportClientFloorPlan(
  room: AvRoom,
  objects: AnyRoomObject[],
  opts?: { projectName?: string }
) {
  // Detached container -- deliberately never appended to document.body.
  // Konva just needs a container element to hang its internal <canvas>
  // nodes off of; it doesn't need to be visible or attached for drawing
  // and toDataURL() to work correctly.
  const container = document.createElement('div');
  const stage = new Konva.Stage({ container, width: PAGE_W, height: PAGE_H });
  const layer = new Konva.Layer();
  stage.add(layer);

  layer.add(new Konva.Rect({ x: 0, y: 0, width: PAGE_W, height: PAGE_H, fill: '#ffffff' }));

  // ── Title block ──────────────────────────────────────────────────────
  layer.add(
    new Konva.Text({ x: MARGIN, y: 28, text: room.room_name, fontSize: 26, fontStyle: 'bold', fill: '#0f2544' })
  );
  const subtitle = [opts?.projectName, room.room_type, room.width && room.length ? `${room.length} × ${room.width} ${room.units}` : null]
    .filter(Boolean)
    .join('   ·   ');
  layer.add(new Konva.Text({ x: MARGIN, y: 62, text: subtitle, fontSize: 14, fill: '#3a4453' }));
  layer.add(
    new Konva.Text({
      x: PAGE_W - MARGIN - 260,
      y: 28,
      width: 260,
      align: 'right',
      text: new Date().toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' }),
      fontSize: 13,
      fill: '#6b7686',
    })
  );
  layer.add(new Konva.Line({ points: [MARGIN, HEADER_H, PAGE_W - MARGIN, HEADER_H], stroke: '#e2e6ec', strokeWidth: 1 }));

  // ── Plan area geometry ───────────────────────────────────────────────
  const planX0 = MARGIN;
  const planY0 = HEADER_H + 34;
  const planW = PAGE_W - MARGIN * 2 - LEGEND_W - 32;
  const planH = PAGE_H - planY0 - MARGIN - FOOTER_H - 30;

  const roomWidthM = toMeters(room.width || 4, room.units);
  const roomLengthM = toMeters(room.length || 4, room.units);
  const ppm = Math.max(Math.min(planW / roomWidthM, planH / roomLengthM), 1);
  const roomPxW = roomWidthM * ppm;
  const roomPxH = roomLengthM * ppm;
  const originX = planX0 + (planW - roomPxW) / 2;
  const originY = planY0 + (planH - roomPxH) / 2;

  // Room boundary + light grid.
  layer.add(new Konva.Rect({ x: originX, y: originY, width: roomPxW, height: roomPxH, fill: '#fbfbfc', stroke: '#0f2544', strokeWidth: 2 }));
  const gridStepM = roomWidthM > 12 || roomLengthM > 12 ? 2 : 1;
  for (let x = 0; x <= roomWidthM + 0.001; x += gridStepM) {
    layer.add(new Konva.Line({ points: [originX + x * ppm, originY, originX + x * ppm, originY + roomPxH], stroke: '#f0f2f5', strokeWidth: 1 }));
  }
  for (let y = 0; y <= roomLengthM + 0.001; y += gridStepM) {
    layer.add(new Konva.Line({ points: [originX, originY + y * ppm, originX + roomPxW, originY + y * ppm], stroke: '#f0f2f5', strokeWidth: 1 }));
  }

  // ── Devices + equipment schedule ─────────────────────────────────────
  const legend: { num: number; name: string; color: string }[] = [];
  objects.forEach((obj, i) => {
    const num = i + 1;
    const entry = libraryEntry(obj.category as string);
    const wM = toMeters(obj.width ?? fromMeters(entry?.defaultWidth ?? 0.3, room.units), room.units);
    const dM = toMeters(obj.depth ?? fromMeters(entry?.defaultDepth ?? 0.3, room.units), room.units);
    const xM = toMeters(obj.position_x, room.units);
    const yM = toMeters(obj.position_y, room.units);
    const color = entry?.color || '#64748b';

    const px = originX + xM * ppm - (wM * ppm) / 2;
    const py = originY + yM * ppm - (dM * ppm) / 2;
    layer.add(
      new Konva.Rect({
        x: px,
        y: py,
        width: Math.max(wM * ppm, 10),
        height: Math.max(dM * ppm, 10),
        fill: color,
        opacity: 0.9,
        stroke: '#1a1f2b',
        strokeWidth: 0.75,
        cornerRadius: 2,
      })
    );
    numberBadge(layer, originX + xM * ppm, originY + yM * ppm, num);
    legend.push({ num, name: obj.object_name || entry?.label || (obj.category as string), color });
  });

  // ── Overall dimensions (width along the bottom, length along the
  //    right side) -- a real equipment schedule with one line per device
  //    is far more useful here than per-device dimension chains, which
  //    would be unreadable clutter once a room has more than a few
  //    objects (training rooms can easily have 15-20). ──
  const dimOffset = 26;
  layer.add(new Konva.Line({ points: [originX, originY + roomPxH + dimOffset, originX + roomPxW, originY + roomPxH + dimOffset], stroke: '#dc2626', strokeWidth: 1.25 }));
  layer.add(new Konva.Line({ points: [originX, originY + roomPxH + dimOffset - 5, originX, originY + roomPxH + dimOffset + 5], stroke: '#dc2626', strokeWidth: 1.25 }));
  layer.add(new Konva.Line({ points: [originX + roomPxW, originY + roomPxH + dimOffset - 5, originX + roomPxW, originY + roomPxH + dimOffset + 5], stroke: '#dc2626', strokeWidth: 1.25 }));
  layer.add(
    new Konva.Text({
      x: originX,
      y: originY + roomPxH + dimOffset + 6,
      width: roomPxW,
      align: 'center',
      text: `${roomWidthM.toFixed(2)} m`,
      fontSize: 12,
      fontStyle: 'bold',
      fill: '#dc2626',
    })
  );
  layer.add(new Konva.Line({ points: [originX + roomPxW + dimOffset, originY, originX + roomPxW + dimOffset, originY + roomPxH], stroke: '#dc2626', strokeWidth: 1.25 }));
  layer.add(new Konva.Line({ points: [originX + roomPxW + dimOffset - 5, originY, originX + roomPxW + dimOffset + 5, originY], stroke: '#dc2626', strokeWidth: 1.25 }));
  layer.add(new Konva.Line({ points: [originX + roomPxW + dimOffset - 5, originY + roomPxH, originX + roomPxW + dimOffset + 5, originY + roomPxH], stroke: '#dc2626', strokeWidth: 1.25 }));
  const lenLabel = new Konva.Text({ text: `${roomLengthM.toFixed(2)} m`, fontSize: 12, fontStyle: 'bold', fill: '#dc2626' });
  lenLabel.rotation(90);
  lenLabel.position({ x: originX + roomPxW + dimOffset + 18, y: originY + roomPxH / 2 - 20 });
  layer.add(lenLabel);

  // ── North arrow ──────────────────────────────────────────────────────
  const naX = originX + roomPxW - 26;
  const naY = originY + 30;
  layer.add(new Konva.Line({ points: [naX, naY + 22, naX, naY - 14], stroke: '#0f2544', strokeWidth: 1.5 }));
  layer.add(new Konva.RegularPolygon({ x: naX, y: naY - 14, sides: 3, radius: 7, fill: '#0f2544', rotation: 0 }));
  layer.add(new Konva.Text({ x: naX - 8, y: naY + 26, text: 'N', fontSize: 12, fontStyle: 'bold', fill: '#0f2544' }));

  // ── Scale bar ────────────────────────────────────────────────────────
  const sbX = originX;
  const sbY = originY + roomPxH + 52;
  layer.add(new Konva.Line({ points: [sbX, sbY, sbX + ppm, sbY], stroke: '#0f2544', strokeWidth: 2 }));
  layer.add(new Konva.Line({ points: [sbX, sbY - 4, sbX, sbY + 4], stroke: '#0f2544', strokeWidth: 2 }));
  layer.add(new Konva.Line({ points: [sbX + ppm, sbY - 4, sbX + ppm, sbY + 4], stroke: '#0f2544', strokeWidth: 2 }));
  layer.add(new Konva.Text({ x: sbX, y: sbY + 8, text: '1 m', fontSize: 11, fill: '#3a4453' }));

  // ── Equipment schedule (legend) ──────────────────────────────────────
  const legX = PAGE_W - MARGIN - LEGEND_W;
  let legY = HEADER_H + 30;
  layer.add(new Konva.Text({ x: legX, y: legY, text: 'Equipment Schedule', fontSize: 15, fontStyle: 'bold', fill: '#0f2544' }));
  legY += 30;
  const legendBottom = PAGE_H - MARGIN - FOOTER_H;
  for (const item of legend) {
    if (legY > legendBottom) {
      layer.add(new Konva.Text({ x: legX, y: legY, text: `+ ${legend.length - legend.indexOf(item)} more…`, fontSize: 11, fontStyle: 'italic', fill: '#6b7686' }));
      break;
    }
    numberBadge(layer, legX + 11, legY + 8, item.num, 9);
    layer.add(new Konva.Rect({ x: legX + 28, y: legY + 2, width: 12, height: 12, fill: item.color, cornerRadius: 2 }));
    layer.add(new Konva.Text({ x: legX + 46, y: legY + 1, width: LEGEND_W - 46, text: item.name, fontSize: 12, fill: '#1a1f2b' }));
    legY += 23;
  }

  // ── Footer ───────────────────────────────────────────────────────────
  layer.add(new Konva.Text({ x: MARGIN, y: PAGE_H - MARGIN + 4, text: 'Generated with QTcal — AV Room Designer', fontSize: 10, fill: '#9aa3b0' }));

  layer.draw();
  const dataUrl = stage.toDataURL({ pixelRatio: 1 });
  stage.destroy();

  const safeName = (room.room_name || 'room').replace(/[^a-z0-9-_]+/gi, '_');
  triggerDownload(dataUrlToBlob(dataUrl), `${safeName}-floorplan-client.png`);
}
