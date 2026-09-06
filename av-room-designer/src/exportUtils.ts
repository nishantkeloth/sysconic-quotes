import type Konva from 'konva';
import type { AnyRoomObject, AvRoom } from './types';
import { getMappedProduct } from './types';
import { libraryEntry } from './deviceLibrary';

// Both exports are pure client-side (no backend round-trip): a CSV bill of
// materials built straight from the objects already loaded in the page, and
// a PNG snapshot of whatever the Konva Stage is currently showing. Neither
// needs a server endpoint, so neither is wired into api/av_rooms.py.

export function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Deferred so Safari/Firefox reliably start the download before the blob
  // URL is revoked -- revoking synchronously right after click() is a known
  // source of "downloads randomly fail" bugs.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function csvCell(value: string | number | null | undefined): string {
  const s = value === null || value === undefined ? '' : String(value);
  // Quote whenever the cell could otherwise be misread (comma, quote,
  // newline) -- doubling embedded quotes per RFC 4180.
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function exportObjectsAsCsv(room: AvRoom, objects: AnyRoomObject[]) {
  const header = [
    'Category',
    'Device Name',
    'Brand',
    'Model',
    'SKU',
    'Quantity',
    'Unit Cost',
    'Currency',
    'Extended Cost',
    'Mapped to Product',
  ];
  const rows = objects.map((o) => {
    const entry = libraryEntry(o.category as string);
    const mapped = getMappedProduct(o);
    const unitCost = mapped?.default_cost ?? '';
    const extended = mapped?.default_cost != null ? mapped.default_cost * (o.quantity || 1) : '';
    return [
      entry?.label || o.category,
      o.object_name || '',
      mapped?.brand || '',
      mapped?.model || '',
      mapped?.sku || '',
      o.quantity || 1,
      unitCost,
      mapped?.cost_currency || '',
      extended,
      mapped ? 'Yes' : 'No',
    ];
  });

  const lines = [header, ...rows].map((row) => row.map(csvCell).join(','));
  // Leading BOM so Excel opens the file as UTF-8 instead of guessing wrong
  // on non-ASCII brand/model names.
  const csv = '﻿' + lines.join('\r\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const safeName = (room.room_name || 'room').replace(/[^a-z0-9-_]+/gi, '_');
  triggerDownload(blob, `${safeName}-bom.csv`);
}

export function exportStageAsPng(stage: Konva.Stage, roomName: string) {
  // pixelRatio 2 for a crisp export regardless of the current on-screen
  // zoom level set via the canvas's own wheel-zoom.
  const dataUrl = stage.toDataURL({ pixelRatio: 2 });
  const blob = dataUrlToBlob(dataUrl);
  const safeName = (roomName || 'room').replace(/[^a-z0-9-_]+/gi, '_');
  triggerDownload(blob, `${safeName}-floorplan.png`);
}

// Composites a captured WebGL frame with a title bar so a 3D "hero shot"
// reads as a presentation image handed to a client, not a raw screenshot
// of the interactive editor.
export function composeHeroImage(dataUrl: string, title: string): Promise<Blob> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const barH = Math.max(56, Math.round(img.height * 0.06));
      const canvas = document.createElement('canvas');
      canvas.width = img.width;
      canvas.height = img.height + barH;
      const ctx = canvas.getContext('2d');
      if (!ctx) {
        reject(new Error('2D canvas context unavailable'));
        return;
      }
      ctx.fillStyle = '#0f2544';
      ctx.fillRect(0, 0, canvas.width, barH);
      ctx.drawImage(img, 0, barH);
      ctx.fillStyle = '#ffffff';
      ctx.font = `600 ${Math.max(18, Math.round(barH * 0.42))}px "Segoe UI", system-ui, sans-serif`;
      ctx.textBaseline = 'middle';
      ctx.fillText(title, 28, barH / 2 + 1);
      canvas.toBlob((blob) => {
        if (blob) resolve(blob);
        else reject(new Error('Canvas toBlob failed'));
      }, 'image/png');
    };
    img.onerror = () => reject(new Error('Failed to load captured 3D frame'));
    img.src = dataUrl;
  });
}

export function dataUrlToBlob(dataUrl: string): Blob {
  const [header, base64] = dataUrl.split(',');
  const mimeMatch = /data:(.*);base64/.exec(header);
  const mime = mimeMatch ? mimeMatch[1] : 'image/png';
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}
