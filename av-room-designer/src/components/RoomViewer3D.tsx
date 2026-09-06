import { Component, Suspense, useEffect, useMemo, useState, type MutableRefObject, type ReactNode } from 'react';
import { Canvas, useThree, type ThreeEvent } from '@react-three/fiber';
import { OrbitControls, Text, Billboard, useGLTF, Environment } from '@react-three/drei';
import * as THREE from 'three';
import type { AnyRoomObject, AvRoom } from '../types';
import { getObjectKey } from '../types';
import { libraryEntry } from '../deviceLibrary';
import { toMeters, fromMeters } from '../units';
import { overlayKind, getCameraOverlay, getMicOverlay, getDisplayOverlay } from '../overlays';
import { woodTexture, fabricTexture, plankFloorTexture } from '../proceduralTextures';

// Synchronized 3D room view (spec §17/§18: one shared room object model
// feeds both the 2D canvas and this view -- no separate 3D-only data).
// Coordinate mapping, used consistently everywhere in this file:
//   Three X  = our position_x  (left-right, same axis as the 2D canvas's X)
//   Three Z  = our position_y  ("depth into the room" axis in the 2D canvas)
//   Three Y  = our position_z  (height off floor -- the one axis the 2D
//              top-down canvas can't represent at all; see its own header
//              comment)
// Every object's Three-space vertical center = position_z + height/2, i.e.
// position_z is the height of the object's BASE above the floor (0 for
// floor-standing furniture, table height for a table mic, wall/ceiling
// mount height for mounted devices) -- edited via the "Height off floor"
// field in DevicePropertiesPanel.
//
// Per spec §43, models are generic colored boxes (not manufacturer-specific
// 3D assets) -- upgradeable to real GLTF/GLB later without touching this
// data model.

const WALL_THICKNESS_M = 0.08;

function DragController({
  draggingKey,
  setDraggingKey,
  room,
  onMoveObject,
}: {
  draggingKey: string | null;
  setDraggingKey: (k: string | null) => void;
  room: AvRoom;
  onMoveObject: (key: string, positionX: number, positionY: number) => void;
}) {
  // Drag is implemented against the mathematical y=0 ground plane via a raw
  // raycast, independent of whatever mesh happens to be under the pointer
  // at each frame (the dragged box itself, the floor, etc.) -- far more
  // robust than relying on a single mesh's onPointerMove firing every time.
  const { camera, gl } = useThree();

  useEffect(() => {
    if (!draggingKey) return;
    const raycaster = new THREE.Raycaster();
    const groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    const ndc = new THREE.Vector2();
    const point = new THREE.Vector3();

    function handleMove(ev: PointerEvent) {
      const rect = gl.domElement.getBoundingClientRect();
      ndc.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, camera);
      if (raycaster.ray.intersectPlane(groundPlane, point)) {
        onMoveObject(draggingKey as string, fromMeters(point.x, room.units), fromMeters(point.z, room.units));
      }
    }
    function handleUp() {
      setDraggingKey(null);
    }
    gl.domElement.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', handleUp);
    return () => {
      gl.domElement.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', handleUp);
    };
  }, [draggingKey, camera, gl, room, onMoveObject, setDraggingKey]);

  return null;
}

// Per spec §43, models are generic (not manufacturer-specific), but "generic
// box for everything" reads as unrecognizable clutter for the two shapes
// people notice most -- tables and chairs. These two get a composite
// silhouette (tabletop+legs, seat+backrest) instead of one solid block;
// everything else stays a simple box, which is a fine generic stand-in for
// compact equipment (cameras, panels, speakers, etc.). All local coordinates
// here have y=0 at the object's OWN BASE (floor/mount-surface contact
// point) -- the parent group in DeviceBox translates that up to
// baseM = position_z before any of this is placed in room space.
function FurnitureMaterial({
  color,
  selected,
  map,
}: {
  color: string;
  selected: boolean;
  map?: THREE.Texture;
}) {
  // When a procedural texture is supplied it already bakes the base color
  // into its pixels (see proceduralTextures.ts) -- tinting on top of that
  // with the same `color` would double-apply it (map color * material
  // color), so the material color only does the tinting when there's no
  // map to tint instead.
  return (
    <meshStandardMaterial
      color={map ? '#ffffff' : color}
      map={map}
      roughness={0.75}
      metalness={0.08}
      emissive={selected ? '#1a1f2b' : '#000000'}
      emissiveIntensity={selected ? 0.35 : 0}
    />
  );
}

function TableShape({
  widthM,
  depthM,
  heightM,
  color,
  selected,
}: {
  widthM: number;
  depthM: number;
  heightM: number;
  color: string;
  selected: boolean;
}) {
  const topThickness = Math.min(0.05, heightM * 0.2) || 0.03;
  const legSize = Math.max(0.03, Math.min(widthM, depthM) * 0.05);
  const legHeight = Math.max(heightM - topThickness, 0.05);
  const legInsetX = Math.max(widthM / 2 - legSize, 0.02);
  const legInsetZ = Math.max(depthM / 2 - legSize, 0.02);
  const topMap = useMemo(() => woodTexture(color, Math.max(1, Math.round((widthM + depthM) / 1.5))), [color, widthM, depthM]);
  const legMap = useMemo(() => woodTexture(color, 1), [color]);

  return (
    <>
      <mesh position={[0, heightM - topThickness / 2, 0]} castShadow receiveShadow>
        <boxGeometry args={[widthM, topThickness, depthM]} />
        <FurnitureMaterial color={color} selected={selected} map={topMap} />
      </mesh>
      {[
        [-legInsetX, -legInsetZ],
        [legInsetX, -legInsetZ],
        [-legInsetX, legInsetZ],
        [legInsetX, legInsetZ],
      ].map(([sx, sz], i) => (
        <mesh key={i} position={[sx, legHeight / 2, sz]} castShadow>
          <boxGeometry args={[legSize, legHeight, legSize]} />
          <FurnitureMaterial color={color} selected={selected} map={legMap} />
        </mesh>
      ))}
    </>
  );
}

function ChairShape({
  widthM,
  depthM,
  heightM,
  color,
  selected,
}: {
  widthM: number;
  depthM: number;
  heightM: number;
  color: string;
  selected: boolean;
}) {
  const seatH = heightM * 0.5;
  const seatThickness = Math.min(0.05, seatH * 0.3) || 0.03;
  const backThickness = Math.min(0.05, widthM * 0.15) || 0.03;
  const legSize = Math.max(0.02, Math.min(widthM, depthM) * 0.06);
  const legInsetX = Math.max(widthM / 2 - legSize, 0.02);
  const legInsetZ = Math.max(depthM / 2 - legSize, 0.02);
  const upholsteryMap = useMemo(() => fabricTexture(color, 2), [color]);
  const frameMap = useMemo(() => woodTexture(color, 1), [color]);

  return (
    <>
      {/* Seat */}
      <mesh position={[0, seatH, 0]} castShadow receiveShadow>
        <boxGeometry args={[widthM, seatThickness, depthM]} />
        <FurnitureMaterial color={color} selected={selected} map={upholsteryMap} />
      </mesh>
      {/* Backrest along the -Z edge (the chair's "back") */}
      <mesh position={[0, seatH + (heightM - seatH) / 2, -depthM / 2 + backThickness / 2]} castShadow>
        <boxGeometry args={[widthM, Math.max(heightM - seatH, 0.05), backThickness]} />
        <FurnitureMaterial color={color} selected={selected} map={upholsteryMap} />
      </mesh>
      {/* Legs */}
      {[
        [-legInsetX, -legInsetZ],
        [legInsetX, -legInsetZ],
        [-legInsetX, legInsetZ],
        [legInsetX, legInsetZ],
      ].map(([sx, sz], i) => (
        <mesh key={i} position={[sx, seatH / 2, sz]} castShadow>
          <boxGeometry args={[legSize, seatH, legSize]} />
          <FurnitureMaterial color={color} selected={selected} map={frameMap} />
        </mesh>
      ))}
    </>
  );
}

function GenericBox({
  widthM,
  depthM,
  heightM,
  color,
  selected,
}: {
  widthM: number;
  depthM: number;
  heightM: number;
  color: string;
  selected: boolean;
}) {
  return (
    <mesh position={[0, heightM / 2, 0]} castShadow receiveShadow>
      <boxGeometry args={[widthM, heightM, depthM]} />
      <FurnitureMaterial color={color} selected={selected} />
    </mesh>
  );
}

// Floor-projected engineering overlays (camera FOV / mic pickup / display
// viewing zone) -- the 3D counterpart of RoomCanvas2D's Konva Arc/Circle
// overlays, built from the exact same overlays.ts data so a change to one
// view's numbers always shows up in the other.
//
// These are rendered as independent, absolutely-positioned floor meshes
// (siblings of DeviceBox, not nested inside its rotated <group>) rather than
// children of the source device: a ceiling mic's coverage patch belongs on
// the floor, not floating up at the mic's own mount height, so projecting
// straight onto y~0 from the object's xM/zM is simpler and more correct
// than un-doing DeviceBox's height offset.
//
// Angle convention: authored like the floor plane -- a shape drawn in local
// XY (standard math angle, counterclockwise from +X) then laid flat via
// rotation-x=-90deg, which maps local Y to world -Z. That Z-flip is what
// turns the app's clockwise-from-"+Y-is-facing-0" convention (see
// overlays.ts's facingToKonvaDegrees) into the counterclockwise-from-+X
// convention Ring/CircleGeometry's thetaStart expects here: the Three-space
// center angle is simply the negation of the Konva facing angle.
function floorAngleRad(rotationZ: number): number {
  const facingKonvaDeg = (rotationZ || 0) + 90;
  return THREE.MathUtils.degToRad(-facingKonvaDeg);
}

// R3F assigns whatever we pass as `raycast` onto the mesh's raycast method;
// returning nothing means "never report an intersection," so clicks pass
// straight through these overlays to the device or floor underneath instead
// of being swallowed by a transparent coverage patch.
function disableRaycast() {
  return null;
}

function CameraOverlay3D({ obj, room }: { obj: AnyRoomObject; room: AvRoom }) {
  const ov = getCameraOverlay(obj, room.units);
  const xM = toMeters(obj.position_x, room.units);
  const zM = toMeters(obj.position_y, room.units);
  const rangeM = Math.max(toMeters(ov.fov_range, room.units), 0.05);
  const fovRad = THREE.MathUtils.degToRad(Math.max(ov.fov_h, 1));
  const thetaStart = floorAngleRad(obj.rotation_z) - fovRad / 2;
  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[xM, 0.012, zM]} raycast={disableRaycast}>
      <ringGeometry args={[0, rangeM, 48, 1, thetaStart, fovRad]} />
      <meshBasicMaterial color="#2563eb" transparent opacity={0.18} side={THREE.DoubleSide} depthWrite={false} />
    </mesh>
  );
}

function MicOverlay3D({ obj, room }: { obj: AnyRoomObject; room: AvRoom }) {
  const ov = getMicOverlay(obj, room.units);
  const xM = toMeters(obj.position_x, room.units);
  const zM = toMeters(obj.position_y, room.units);
  const radiusM = Math.max(toMeters(ov.pickup_radius, room.units), 0.05);
  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[xM, 0.012, zM]} raycast={disableRaycast}>
      <circleGeometry args={[radiusM, 48]} />
      <meshBasicMaterial color="#16a34a" transparent opacity={0.14} side={THREE.DoubleSide} depthWrite={false} />
    </mesh>
  );
}

function DisplayOverlay3D({ obj, room }: { obj: AnyRoomObject; room: AvRoom }) {
  const ov = getDisplayOverlay(obj, room.units);
  const xM = toMeters(obj.position_x, room.units);
  const zM = toMeters(obj.position_y, room.units);
  const minM = Math.max(toMeters(ov.viewing_distance_min, room.units), 0.05);
  const maxM = Math.max(toMeters(ov.viewing_distance_max, room.units), minM + 0.1);
  const angleRad = THREE.MathUtils.degToRad(Math.max(ov.viewing_angle, 1));
  const thetaStart = floorAngleRad(obj.rotation_z) - angleRad / 2;
  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[xM, 0.014, zM]} raycast={disableRaycast}>
      <ringGeometry args={[minM, maxM, 48, 1, thetaStart, angleRad]} />
      <meshBasicMaterial color="#d97706" transparent opacity={0.14} side={THREE.DoubleSide} depthWrite={false} />
    </mesh>
  );
}

function Overlay3D({ obj, room }: { obj: AnyRoomObject; room: AvRoom }) {
  const kind = overlayKind(obj.category as string);
  if (kind === 'camera') return <CameraOverlay3D obj={obj} room={room} />;
  if (kind === 'mic') return <MicOverlay3D obj={obj} room={room} />;
  if (kind === 'display') return <DisplayOverlay3D obj={obj} room={room} />;
  return null;
}

// Real furniture models: Kenney's "Furniture Kit" (kenney.nl), CC0-licensed,
// mirrored with stable per-file URLs at github.com/shorepine/kenney (see
// that repo's LICENSE.txt). Loaded straight from that URL at runtime via
// drei's useGLTF -- no local copy checked into this repo, so there's
// nothing extra to build/ship, at the cost of depending on that mirror
// staying up. There is no free CC0 kit for conference-room AV gear
// (cameras, mounted displays, ceiling mics, racks, control panels, etc.),
// so those categories -- and any furniture category not listed here --
// keep the procedural shapes below.
const KENNEY_FURNITURE_BASE = 'https://raw.githubusercontent.com/shorepine/kenney/main/3d/furniture';
const REAL_MODEL_URL: Partial<Record<string, string>> = {
  table: `${KENNEY_FURNITURE_BASE}/tableCross.glb`,
  chair: `${KENNEY_FURNITURE_BASE}/chairDesk.glb`,
  credenza: `${KENNEY_FURNITURE_BASE}/cabinetTelevision.glb`,
  cabinet: `${KENNEY_FURNITURE_BASE}/cabinetTelevision.glb`,
};
Object.values(REAL_MODEL_URL).forEach((u) => {
  if (u) useGLTF.preload(u);
});

// Catches a *rejected* load (network error, 404, CORS) -- Suspense alone
// only handles the *pending* case (a thrown promise); an actual thrown
// Error from the GLTFLoader needs a real error boundary or it takes down
// the whole 3D view instead of just this one device falling back to its
// procedural shape.
class ModelErrorBoundary extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

// Loads and normalizes a real glTF model to this file's local-space
// convention (y=0 at the object's own base, centered on X/Z) and to
// whatever width/depth/height the object currently has -- so a model built
// at some arbitrary real-world scale still respects the properties panel's
// dimensions exactly like the procedural shapes do. Materials are cloned
// per-instance (drei caches and reuses the same THREE.Object3D/materials
// across every device using the same URL) so selecting one chair doesn't
// highlight every chair sharing that model.
function GLTFFurniture({
  url,
  widthM,
  depthM,
  heightM,
  selected,
}: {
  url: string;
  widthM: number;
  depthM: number;
  heightM: number;
  selected: boolean;
}) {
  const { scene } = useGLTF(url);

  const cloned = useMemo(() => {
    const root = scene.clone(true);
    root.traverse((child) => {
      const mesh = child as THREE.Mesh;
      if (!mesh.isMesh) return;
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      mesh.material = Array.isArray(mesh.material)
        ? mesh.material.map((m) => m.clone())
        : (mesh.material as THREE.Material).clone();
      const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      mats.forEach((m) => {
        const std = m as THREE.MeshStandardMaterial;
        if ('emissive' in std) {
          std.emissive = new THREE.Color(selected ? '#1a1f2b' : '#000000');
          std.emissiveIntensity = selected ? 0.35 : 0;
        }
      });
    });
    return root;
  }, [scene, selected]);

  const { scale, offset } = useMemo(() => {
    const box = new THREE.Box3().setFromObject(cloned);
    const size = new THREE.Vector3();
    box.getSize(size);
    const center = new THREE.Vector3();
    box.getCenter(center);
    const sx = size.x > 1e-4 ? widthM / size.x : 1;
    const sy = size.y > 1e-4 ? heightM / size.y : 1;
    const sz = size.z > 1e-4 ? depthM / size.z : 1;
    return {
      scale: [sx, sy, sz] as [number, number, number],
      offset: [-center.x * sx, -box.min.y * sy, -center.z * sz] as [number, number, number],
    };
  }, [cloned, widthM, depthM, heightM]);

  return (
    <group scale={scale} position={offset}>
      <primitive object={cloned} />
    </group>
  );
}

// Exposes an imperative "capture the current frame at higher resolution"
// function via a ref, for the "Export Image" hero shot -- rendered inside
// the Canvas (only useThree() has access to the live gl/scene/camera).
// Temporarily bumps the renderer's pixel ratio for one extra render pass
// so the exported PNG is sharper than the on-screen canvas's own CSS size
// (which shrinks in Split view), then restores it and re-renders once more
// so the live view isn't left at the wrong resolution.
function ExportCapture({ captureRef }: { captureRef?: MutableRefObject<(() => string) | null> }) {
  const { gl, scene, camera } = useThree();
  useEffect(() => {
    if (!captureRef) return;
    captureRef.current = () => {
      const prevRatio = gl.getPixelRatio();
      gl.setPixelRatio(Math.min(3, prevRatio * 2));
      gl.render(scene, camera);
      const dataUrl = gl.domElement.toDataURL('image/png');
      gl.setPixelRatio(prevRatio);
      gl.render(scene, camera);
      return dataUrl;
    };
    return () => {
      if (captureRef) captureRef.current = null;
    };
  }, [captureRef, gl, scene, camera]);
  return null;
}

function DeviceBox({
  obj,
  room,
  selected,
  onSelect,
  setDraggingKey,
  onBeginEdit,
}: {
  obj: AnyRoomObject;
  room: AvRoom;
  selected: boolean;
  onSelect: (key: string) => void;
  setDraggingKey: (key: string) => void;
  onBeginEdit?: () => void;
}) {
  const key = getObjectKey(obj);
  const entry = libraryEntry(obj.category as string);

  const widthM = toMeters(obj.width ?? fromMeters(entry?.defaultWidth ?? 0.3, room.units), room.units);
  const depthM = toMeters(obj.depth ?? fromMeters(entry?.defaultDepth ?? 0.3, room.units), room.units);
  const heightM = toMeters(obj.height ?? fromMeters(entry?.defaultHeight ?? 0.3, room.units), room.units);
  const baseM = toMeters(obj.position_z || 0, room.units);
  const xM = toMeters(obj.position_x, room.units);
  const zM = toMeters(obj.position_y, room.units);
  // Sign chosen to approximate the 2D canvas's clockwise-from-above facing
  // convention (see overlays.ts's facingToKonvaDegrees); not pixel-verified
  // against 2D, fine for a generic box with no directional visual cue.
  const rotY = THREE.MathUtils.degToRad(-(obj.rotation_z || 0));

  const color = entry?.color || '#64748b';
  const category = obj.category as string;

  const shapeProps = { widthM, depthM, heightM, color, selected };
  let fallbackShape;
  if (category === 'table') fallbackShape = <TableShape {...shapeProps} />;
  else if (category === 'chair') fallbackShape = <ChairShape {...shapeProps} />;
  else fallbackShape = <GenericBox {...shapeProps} />;

  const modelUrl = REAL_MODEL_URL[category];
  const shape = modelUrl ? (
    <ModelErrorBoundary fallback={fallbackShape}>
      <Suspense fallback={fallbackShape}>
        <GLTFFurniture url={modelUrl} widthM={widthM} depthM={depthM} heightM={heightM} selected={selected} />
      </Suspense>
    </ModelErrorBoundary>
  ) : (
    fallbackShape
  );

  return (
    <group
      position={[xM, baseM, zM]}
      rotation={[0, rotY, 0]}
      onPointerDown={(e: ThreeEvent<PointerEvent>) => {
        e.stopPropagation();
        onSelect(key);
        onBeginEdit?.();
        setDraggingKey(key);
      }}
    >
      {shape}
      <Billboard position={[0, heightM + 0.18, 0]}>
        <Text fontSize={0.13} color="#1a1f2b" anchorX="center" anchorY="bottom">
          {obj.object_name || entry?.label || obj.category}
        </Text>
      </Billboard>
    </group>
  );
}

export default function RoomViewer3D({
  room,
  objects,
  selectedKey,
  onSelect,
  onMoveObject,
  onBeginEdit,
  showOverlays = true,
  captureRef,
}: {
  room: AvRoom;
  objects: AnyRoomObject[];
  selectedKey: string | null;
  onSelect: (key: string | null) => void;
  onMoveObject: (key: string, positionX: number, positionY: number) => void;
  onBeginEdit?: () => void;
  showOverlays?: boolean;
  captureRef?: MutableRefObject<(() => string) | null>;
}) {
  // Drag state is purely internal to this view -- the 2D canvas has its own
  // independent drag mechanism (Konva's built-in draggable), so there's
  // nothing else that needs to know a 3D drag is in progress.
  const [draggingKey, setDraggingKey] = useState<string | null>(null);

  const roomWidthM = toMeters(room.width || 4, room.units);
  const roomLengthM = toMeters(room.length || 4, room.units);
  const roomHeightM = toMeters(room.height || 2.7, room.units);

  // A gentler elevated 3/4 angle than before (that camera sat almost
  // directly overhead, which is what read as a "ceiling" in the first
  // pass) -- height scales with room size but caps out relatively low
  // relative to distance, closer to how real estate / room-planner tools
  // frame a room.
  const maxDim = Math.max(roomWidthM, roomLengthM);
  const cameraPos = useMemo<[number, number, number]>(
    () => [roomWidthM / 2 + maxDim * 0.95, roomHeightM * 1.15 + 1, roomLengthM + maxDim * 0.85],
    [roomWidthM, roomLengthM, roomHeightM, maxDim]
  );
  const target = useMemo<[number, number, number]>(
    () => [roomWidthM / 2, roomHeightM * 0.35, roomLengthM / 2],
    [roomWidthM, roomHeightM, roomLengthM]
  );
  const shadowExtent = maxDim + 2;
  const floorMap = useMemo(() => plankFloorTexture('#c9b896', Math.max(2, Math.round(maxDim))), [maxDim]);

  return (
    <div className="avrd-canvas-wrap">
      <Canvas
        shadows
        camera={{ position: cameraPos, fov: 45, near: 0.1, far: 200 }}
        onPointerMissed={() => onSelect(null)}
        gl={{ toneMapping: THREE.ACESFilmicToneMapping, toneMappingExposure: 1.05 }}
      >
        <color attach="background" args={['#dde3ea']} />

        {/* Image-based lighting: gives every material (procedural shapes
            and the loaded glTF furniture alike) real ambient reflections
            and color bounce instead of the flat, uniformly-lit look a
            couple of directional lights alone produce -- the single
            biggest lever for "looks like a real render" that doesn't
            require licensed assets. Loaded from drei's public, free HDRI
            bucket at runtime; background=false so it only lights the
            scene without replacing the flat sky color above. */}
        <Environment preset="apartment" background={false} />
        <ExportCapture captureRef={captureRef} />

        {/* Soft sky/ground ambient fill + one shadow-casting key light --
            the flat single ambient+directional pair from the first pass is
            what made everything look uniformly lit and "CAD-like"; this
            gives surfaces an actual light/shadow gradient. */}
        <hemisphereLight args={['#ffffff', '#c7cdd6', 0.65]} />
        <directionalLight
          castShadow
          position={[roomWidthM * 0.7 + 2, roomHeightM * 3 + 2, roomLengthM * 0.6 + 2]}
          intensity={1.1}
          shadow-mapSize-width={1536}
          shadow-mapSize-height={1536}
          shadow-camera-left={-shadowExtent}
          shadow-camera-right={shadowExtent}
          shadow-camera-top={shadowExtent}
          shadow-camera-bottom={-shadowExtent}
          shadow-camera-near={0.5}
          shadow-camera-far={shadowExtent * 4}
          shadow-bias={-0.0015}
        />
        {/* Low-intensity fill light from the opposite side so shadow-side
            faces aren't pure black. */}
        <directionalLight position={[-roomWidthM * 0.5 - 1, roomHeightM + 1, -roomLengthM * 0.4 - 1]} intensity={0.25} />

        {/* Floor -- padded slightly beyond the room footprint so dragging
            near the walls stays smooth; also the click target for
            deselect-on-empty-click. */}
        <mesh
          rotation={[-Math.PI / 2, 0, 0]}
          position={[roomWidthM / 2, 0, roomLengthM / 2]}
          onPointerDown={() => onSelect(null)}
          receiveShadow
        >
          <planeGeometry args={[roomWidthM + 2, roomLengthM + 2]} />
          <meshStandardMaterial map={floorMap} color="#ffffff" roughness={0.85} metalness={0} />
        </mesh>

        <gridHelper
          args={[
            Math.max(roomWidthM, roomLengthM) + 2,
            Math.max(1, Math.round(Math.max(roomWidthM, roomLengthM) + 2)),
            '#c3cad2',
            '#d3d9e0',
          ]}
          position={[roomWidthM / 2, 0.004, roomLengthM / 2]}
        />

        {/* Walls -- an "open box" (3 of 4 walls; the wall nearest the
            default camera angle, +Z / "front", is omitted) so the room is
            always readable without relying on transparency, which renders
            unpredictably once walls overlap in view. Opaque, light,
            architectural-model style rather than a saturated brand color. */}
        <mesh position={[roomWidthM / 2, roomHeightM / 2, 0]} receiveShadow>
          <boxGeometry args={[roomWidthM, roomHeightM, WALL_THICKNESS_M]} />
          <meshStandardMaterial color="#f4f5f7" roughness={0.92} metalness={0} />
        </mesh>
        <mesh position={[0, roomHeightM / 2, roomLengthM / 2]} receiveShadow>
          <boxGeometry args={[WALL_THICKNESS_M, roomHeightM, roomLengthM]} />
          <meshStandardMaterial color="#eef0f2" roughness={0.92} metalness={0} />
        </mesh>
        <mesh position={[roomWidthM, roomHeightM / 2, roomLengthM / 2]} receiveShadow>
          <boxGeometry args={[WALL_THICKNESS_M, roomHeightM, roomLengthM]} />
          <meshStandardMaterial color="#eef0f2" roughness={0.92} metalness={0} />
        </mesh>

        {showOverlays &&
          objects.map((obj) => <Overlay3D key={`${getObjectKey(obj)}-ov3d`} obj={obj} room={room} />)}

        {objects.map((obj) => (
          <DeviceBox
            key={getObjectKey(obj)}
            obj={obj}
            room={room}
            selected={selectedKey === getObjectKey(obj)}
            onSelect={onSelect}
            setDraggingKey={setDraggingKey}
            onBeginEdit={onBeginEdit}
          />
        ))}

        <DragController
          draggingKey={draggingKey}
          setDraggingKey={setDraggingKey}
          room={room}
          onMoveObject={onMoveObject}
        />

        <OrbitControls enabled={!draggingKey} target={target} makeDefault />
      </Canvas>
    </div>
  );
}
