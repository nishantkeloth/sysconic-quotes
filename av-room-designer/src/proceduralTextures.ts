import * as THREE from 'three';

// Zero-cost realism lever: instead of flat solid-color materials (which is
// what reads as "toy-like"/"CAD-like" -- see the earlier visual-quality
// feedback) or licensed/paid PBR asset packs (which is what true photoreal
// furniture actually needs, and isn't something to buy without sign-off),
// generate subtle procedural surface variation on an HTML canvas at
// runtime and use it as a THREE.CanvasTexture. This gets real furniture
// materials (wood grain, woven fabric, a plank floor) most of the way to
// "looks like an actual surface" for free, no external asset dependency,
// no licensing question. Textures are cached per (kind, color) pair so
// multiple chairs/tables of the same color share one canvas instead of
// regenerating it per instance.

const cache = new Map<string, THREE.Texture>();

function makeCanvas(size: number): { canvas: HTMLCanvasElement; ctx: CanvasRenderingContext2D } {
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('2D canvas context unavailable');
  return { canvas, ctx };
}

function finalizeTexture(canvas: HTMLCanvasElement, repeat: number): THREE.Texture {
  const tex = new THREE.CanvasTexture(canvas);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(repeat, repeat);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.needsUpdate = true;
  return tex;
}

// Small deterministic PRNG (mulberry32) seeded from the color string, so
// the same color always generates the same grain pattern instead of a new
// random one every time a material happens to re-render.
function seededRandom(seed: string): () => number {
  let h = 1779033703 ^ seed.length;
  for (let i = 0; i < seed.length; i++) {
    h = Math.imul(h ^ seed.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  return function () {
    h = Math.imul(h ^ (h >>> 16), 2246822507);
    h = Math.imul(h ^ (h >>> 13), 3266489909);
    h ^= h >>> 16;
    return (h >>> 0) / 4294967296;
  };
}

export function woodTexture(baseColor: string, repeat = 2): THREE.Texture {
  const key = `wood:${baseColor}`;
  const hit = cache.get(key);
  if (hit) return hit;

  const size = 256;
  const { canvas, ctx } = makeCanvas(size);
  const base = new THREE.Color(baseColor);
  ctx.fillStyle = `#${base.getHexString()}`;
  ctx.fillRect(0, 0, size, size);

  const rand = seededRandom(baseColor);
  for (let i = 0; i < 36; i++) {
    const y = rand() * size;
    const lighten = (rand() - 0.5) * 0.14;
    const shade = base.clone().offsetHSL(0, 0, lighten);
    ctx.strokeStyle = `#${shade.getHexString()}`;
    ctx.globalAlpha = 0.2 + rand() * 0.35;
    ctx.lineWidth = 1 + rand() * 2.5;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.bezierCurveTo(
      size * 0.3,
      y + (rand() - 0.5) * 14,
      size * 0.7,
      y + (rand() - 0.5) * 14,
      size,
      y + (rand() - 0.5) * 6
    );
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  const tex = finalizeTexture(canvas, repeat);
  cache.set(key, tex);
  return tex;
}

export function fabricTexture(baseColor: string, repeat = 3): THREE.Texture {
  const key = `fabric:${baseColor}`;
  const hit = cache.get(key);
  if (hit) return hit;

  const size = 128;
  const { canvas, ctx } = makeCanvas(size);
  const base = new THREE.Color(baseColor);
  ctx.fillStyle = `#${base.getHexString()}`;
  ctx.fillRect(0, 0, size, size);

  const rand = seededRandom(baseColor + '-fabric');
  const imageData = ctx.getImageData(0, 0, size, size);
  const data = imageData.data;
  for (let i = 0; i < data.length; i += 4) {
    // Fine speckle noise (woven-fabric feel) rather than smooth streaks.
    const noise = (rand() - 0.5) * 18;
    data[i] = Math.min(255, Math.max(0, data[i] + noise));
    data[i + 1] = Math.min(255, Math.max(0, data[i + 1] + noise));
    data[i + 2] = Math.min(255, Math.max(0, data[i + 2] + noise));
  }
  ctx.putImageData(imageData, 0, 0);

  // Faint woven grid on top of the speckle.
  ctx.globalAlpha = 0.06;
  ctx.strokeStyle = '#000000';
  ctx.lineWidth = 1;
  for (let x = 0; x < size; x += 4) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, size);
    ctx.stroke();
  }
  for (let y = 0; y < size; y += 4) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(size, y);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  const tex = finalizeTexture(canvas, repeat);
  cache.set(key, tex);
  return tex;
}

export function plankFloorTexture(baseColor: string, repeat = 6): THREE.Texture {
  const key = `plank:${baseColor}`;
  const hit = cache.get(key);
  if (hit) return hit;

  const size = 256;
  const { canvas, ctx } = makeCanvas(size);
  const base = new THREE.Color(baseColor);
  const rand = seededRandom(baseColor + '-plank');

  const plankCount = 6;
  const plankH = size / plankCount;
  for (let p = 0; p < plankCount; p++) {
    const lighten = (rand() - 0.5) * 0.1;
    const shade = base.clone().offsetHSL(0, 0, lighten);
    ctx.fillStyle = `#${shade.getHexString()}`;
    ctx.fillRect(0, p * plankH, size, plankH);
    // Subtle grain lines within the plank.
    ctx.globalAlpha = 0.15;
    ctx.strokeStyle = '#000000';
    for (let g = 0; g < 3; g++) {
      const gy = p * plankH + rand() * plankH;
      ctx.beginPath();
      ctx.moveTo(0, gy);
      ctx.lineTo(size, gy + (rand() - 0.5) * 4);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    // Plank seam.
    ctx.strokeStyle = 'rgba(0,0,0,0.35)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, p * plankH);
    ctx.lineTo(size, p * plankH);
    ctx.stroke();
  }

  const tex = finalizeTexture(canvas, repeat);
  cache.set(key, tex);
  return tex;
}
