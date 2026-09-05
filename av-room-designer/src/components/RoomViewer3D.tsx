import { useEffect, useMemo, useState } from 'react';
import { Canvas, useThree, type ThreeEvent } from '@react-three/fiber';
import { OrbitControls, Text, Billboard } from '@react-three/drei';
import * as THREE from 'three';
import type { AnyRoomObject, AvRoom } from '../types';
import { getObjectKey } from '../types';
import { libraryEntry } from '../deviceLibrary';
import { toMeters, fromMeters } from '../units';

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
function FurnitureMaterial({ color, selected }: { color: string; selected: boolean }) {
  return (
    <meshStandardMaterial
      color={color}
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

  return (
    <>
      <mesh position={[0, heightM - topThickness / 2, 0]} castShadow receiveShadow>
        <boxGeometry args={[widthM, topThickness, depthM]} />
        <FurnitureMaterial color={color} selected={selected} />
      </mesh>
      {[
        [-legInsetX, -legInsetZ],
        [legInsetX, -legInsetZ],
        [-legInsetX, legInsetZ],
        [legInsetX, legInsetZ],
      ].map(([sx, sz], i) => (
        <mesh key={i} position={[sx, legHeight / 2, sz]} castShadow>
          <boxGeometry args={[legSize, legHeight, legSize]} />
          <FurnitureMaterial color={color} selected={selected} />
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

  return (
    <>
      {/* Seat */}
      <mesh position={[0, seatH, 0]} castShadow receiveShadow>
        <boxGeometry args={[widthM, seatThickness, depthM]} />
        <FurnitureMaterial color={color} selected={selected} />
      </mesh>
      {/* Backrest along the -Z edge (the chair's "back") */}
      <mesh position={[0, seatH + (heightM - seatH) / 2, -depthM / 2 + backThickness / 2]} castShadow>
        <boxGeometry args={[widthM, Math.max(heightM - seatH, 0.05), backThickness]} />
        <FurnitureMaterial color={color} selected={selected} />
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
          <FurnitureMaterial color={color} selected={selected} />
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

function DeviceBox({
  obj,
  room,
  selected,
  onSelect,
  setDraggingKey,
}: {
  obj: AnyRoomObject;
  room: AvRoom;
  selected: boolean;
  onSelect: (key: string) => void;
  setDraggingKey: (key: string) => void;
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
  let shape;
  if (category === 'table') shape = <TableShape {...shapeProps} />;
  else if (category === 'chair') shape = <ChairShape {...shapeProps} />;
  else shape = <GenericBox {...shapeProps} />;

  return (
    <group
      position={[xM, baseM, zM]}
      rotation={[0, rotY, 0]}
      onPointerDown={(e: ThreeEvent<PointerEvent>) => {
        e.stopPropagation();
        onSelect(key);
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
}: {
  room: AvRoom;
  objects: AnyRoomObject[];
  selectedKey: string | null;
  onSelect: (key: string | null) => void;
  onMoveObject: (key: string, positionX: number, positionY: number) => void;
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

  return (
    <div className="avrd-canvas-wrap">
      <Canvas
        shadows
        camera={{ position: cameraPos, fov: 45, near: 0.1, far: 200 }}
        onPointerMissed={() => onSelect(null)}
      >
        <color attach="background" args={['#dde3ea']} />

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
          <meshStandardMaterial color="#e4e0d6" roughness={0.95} metalness={0} />
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

        {objects.map((obj) => (
          <DeviceBox
            key={getObjectKey(obj)}
            obj={obj}
            room={room}
            selected={selectedKey === getObjectKey(obj)}
            onSelect={onSelect}
            setDraggingKey={setDraggingKey}
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
