/**
 * CadastreViewer3D.jsx — Strata Cadastre (SIH26011), Phase 4
 * React + CesiumJS interactive 3D cadastral viewer — spec MODULE D.
 *
 * ---------------------------------------------------------------------------
 * SETUP (the part that most often eats demo day — do this well before)
 * ---------------------------------------------------------------------------
 *   npm create vite@latest strata-ui -- --template react
 *   cd strata-ui
 *   npm install cesium vite-plugin-cesium
 *
 * vite.config.js:
 *   import { defineConfig } from 'vite';
 *   import react from '@vitejs/plugin-react';
 *   import cesium from 'vite-plugin-cesium';
 *   export default defineConfig({ plugins: [react(), cesium()] });
 *
 * Then drop this file in src/ and render <CadastreViewer3D /> from App.jsx.
 * vite-plugin-cesium handles CESIUM_BASE_URL and copies Cesium's static
 * assets (workers, widget CSS); without it you get a blank globe and
 * console 404s, which is the single most common CesiumJS setup failure.
 *
 * ---------------------------------------------------------------------------
 * CESIUM ION TOKEN — OPTIONAL, AND DELIBERATELY SO
 * ---------------------------------------------------------------------------
 * Paste your token into CESIUM_ION_TOKEN below to get Cesium World Terrain
 * and OSM Buildings (real surrounding city geometry). Leave it EMPTY and the
 * viewer still works: it falls back to OpenStreetMap raster imagery on the
 * plain WGS84 ellipsoid, with the synthetic neighbouring blocks defined in
 * this file standing in for city context. That fallback is intentional —
 * a demo that hard-depends on a token is a demo that dies if the token
 * expires or the venue wifi blocks ion.
 *
 * ---------------------------------------------------------------------------
 * WHAT WAS AND WASN'T TESTED
 * ---------------------------------------------------------------------------
 * This file was syntax-checked with the TypeScript parser (zero diagnostics)
 * but NOT executed: react, react-dom and cesium are not installed in the
 * environment it was written in, and there's no browser/WebGL context there
 * either. Same honest caveat as pipeline.py (no PDAL/Shapely/Trimesh) and
 * main.py (no FastAPI). Everything below follows current CesiumJS API
 * conventions, with one specific compatibility guard worth knowing about:
 * the Viewer constructor's `imageryProvider` option was DEPRECATED in
 * CesiumJS 1.104 and REMOVED in 1.107, replaced by `baseLayer`. Passing
 * only one of the two breaks on half the versions in the wild, so
 * buildViewerOptions() below passes BOTH as `false` (each version reads the
 * key it knows and ignores the other), then attaches imagery after
 * construction via the stable imageryLayers.addImageryProvider() API.
 *
 * ---------------------------------------------------------------------------
 * DATA
 * ---------------------------------------------------------------------------
 * BUILDING_DATA below mirrors, floor for floor, the synthetic building that
 * pipeline.py generates and main.py registers: G+5 sanctioned on a 15x11 m
 * footprint at 3.2 m floor height, with a 1.5 m setback required on the top
 * sanctioned level; as-built has floor 5 built out past that setback and an
 * entirely unauthorised floor 6 on top. The 3D-ULPINs use the exact 28-char
 * format main.py's generate_3d_ulpin() produces. Wire this to the live API
 * by replacing loadCadastreData() with a fetch of
 * GET /api/v1/units/{ulpin} + GET /api/v1/analytics/deviations/{base_ulpin}.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as Cesium from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";

/* ===========================================================================
 * CONFIG
 * =========================================================================== */

// <<< PASTE YOUR CESIUM ION ACCESS TOKEN HERE (optional — see header) >>>
const CESIUM_ION_TOKEN = "";

const BASE_LAT = 23.0225;   // Ahmedabad, Gujarat — same parcel as Phases 1-3
const BASE_LON = 72.5714;
const BASE_ULPIN = "11000088421000";
const LGD_CODE = "240124";

const FLOOR_HEIGHT = 3.2;
const FOOTPRINT_W = 15.0;
const FOOTPRINT_D = 11.0;
const SETBACK_M = 1.5;

const PALETTE = {
  surface: Cesium.Color.fromCssColorString("#3fbfb0"),
  residential: Cesium.Color.fromCssColorString("#d6b24a"),
  commercial: Cesium.Color.fromCssColorString("#c1594f"),
  common: Cesium.Color.fromCssColorString("#7c8a99"),
  sanctioned: Cesium.Color.fromCssColorString("#4f9a63"),
  unauthorised: Cesium.Color.fromCssColorString("#d64545"),
  neighbour: Cesium.Color.fromCssColorString("#33505f"),
  selected: Cesium.Color.fromCssColorString("#24e5ff"),
};

/* ===========================================================================
 * GEOMETRY HELPERS — local ENU metres -> WGS84 degrees
 * =========================================================================== */

const METRES_PER_DEG_LAT = 111320;
const metresPerDegLon = (lat) => 111320 * Math.cos((lat * Math.PI) / 180);

/** Converts a local-ENU [x, y] metre offset (as used throughout Phases 1-3)
 *  into [lon, lat] degrees around the parcel origin. */
function localToLonLat([x, y], originLat = BASE_LAT, originLon = BASE_LON) {
  return [
    originLon + x / metresPerDegLon(originLat),
    originLat + y / METRES_PER_DEG_LAT,
  ];
}

/** Flat [lon, lat, lon, lat, ...] array Cesium's fromDegreesArray wants. */
function footprintToDegreesArray(footprint, originLat, originLon) {
  const flat = [];
  for (const pt of footprint) {
    const [lon, lat] = localToLonLat(pt, originLat, originLon);
    flat.push(lon, lat);
  }
  return flat;
}

function rectFootprint(width, depth) {
  const hw = width / 2;
  const hd = depth / 2;
  return [
    [-hw, -hd],
    [hw, -hd],
    [hw, hd],
    [-hw, hd],
  ];
}

function polygonAreaSqm(footprint) {
  let area = 0;
  for (let i = 0; i < footprint.length; i++) {
    const [x1, y1] = footprint[i];
    const [x2, y2] = footprint[(i + 1) % footprint.length];
    area += x1 * y2 - x2 * y1;
  }
  return Math.abs(area / 2);
}

/* ===========================================================================
 * DATA — mirrors pipeline.py's synthetic building + main.py's ULPINs
 * =========================================================================== */

const FULL_FOOTPRINT = rectFootprint(FOOTPRINT_W, FOOTPRINT_D);
const SETBACK_FOOTPRINT = rectFootprint(
  FOOTPRINT_W - 2 * SETBACK_M,
  FOOTPRINT_D - 2 * SETBACK_M
);

/** Assembles the 28-character 3D-ULPIN exactly as main.py's
 *  generate_3d_ulpin() does: [LGD 6][Base 14][Type 1][Floor 3][Unit 4]. */
function makeUlpin3d(verticalType, floorIndex, unitCode) {
  const floorSegment = String(Math.abs(floorIndex)).padStart(3, "0");
  const unitSegment = String(unitCode).padStart(4, "0");
  return `${LGD_CODE}${BASE_ULPIN}${verticalType}${floorSegment}${unitSegment}`;
}

/** Splits a 28-char 3D-ULPIN into its five labelled segments for the
 *  inspect panel's breakdown display. */
function breakdownUlpin(ulpin) {
  return [
    { label: "LGD locator", value: ulpin.slice(0, 6) },
    { label: "Base 2D ULPIN", value: ulpin.slice(6, 20) },
    { label: "Vertical type", value: ulpin.slice(20, 21) },
    { label: "Floor index", value: ulpin.slice(21, 24) },
    { label: "Unit ID", value: ulpin.slice(24, 28) },
  ];
}

const VERTICAL_TYPE_LABELS = {
  B: "Basement / subsurface",
  G: "Ground",
  F: "Floor",
  T: "Terrace / air-rights",
  U: "Subsurface utility",
  E: "Elevated corridor",
};

function loadCadastreData() {
  const units = [];

  // Basement parking — one below-grade level, vertical type B.
  units.push({
    id: "B01",
    ulpin: makeUlpin3d("B", 1, 1),
    verticalType: "B",
    floorIndex: -1,
    label: "Basement parking",
    usageType: "PARKING",
    asBuilt: FULL_FOOTPRINT,
    sanctioned: FULL_FOOTPRINT,
    zMin: -3.0,
    zMax: 0,
    colorKey: "common",
    owner: "Shreeji Heights Owners Association",
    udsPercent: 0,
    reraRegistered: true,
    lien: null,
    saleDeedHash: "0x8f2a…c471",
    kycStatus: "VERIFIED",
    cadastralStatus: "OC_ISSUED_ACTIVE",
  });

  const floorMeta = [
    { usage: "COMMERCIAL", label: "Retail + lobby", colorKey: "commercial", owner: "Rakesh Patel" },
    { usage: "RESIDENTIAL", label: "Flats 101 / 102", colorKey: "residential", owner: "Priya Shah" },
    { usage: "RESIDENTIAL", label: "Flats 201 / 202", colorKey: "residential", owner: "Nilesh Vora" },
    { usage: "RESIDENTIAL", label: "Flats 301 / 302", colorKey: "residential", owner: "Kavita Joshi" },
    { usage: "RESIDENTIAL", label: "Flats 401 / 402", colorKey: "residential", owner: "Sanjay Desai" },
    { usage: "RESIDENTIAL", label: "Flats 501 / 502", colorKey: "residential", owner: "Meera Trivedi" },
    { usage: "RESIDENTIAL", label: "Flats 601 / 602", colorKey: "residential", owner: "Ashok Chauhan" },
  ];

  const liens = {
    2: { lender: "State Bank of India", amount: "Rs. 32,00,000", reference: "SBI/AHM/2024/00871" },
    4: { lender: "HDFC Bank Ltd.", amount: "Rs. 41,50,000", reference: "HDFC/AHM/2023/04412" },
  };

  for (let f = 0; f <= 6; f++) {
    const meta = floorMeta[f];
    // Sanctioned envelope: floors 0-4 full footprint, floor 5 setback,
    // floor 6 never sanctioned at all.
    let sanctioned;
    if (f <= 4) sanctioned = FULL_FOOTPRINT;
    else if (f === 5) sanctioned = SETBACK_FOOTPRINT;
    else sanctioned = null;

    // As-built: floors 0-5 full footprint (5 ignores its setback),
    // floor 6 built to the setback envelope but with no sanction.
    const asBuilt = f <= 5 ? FULL_FOOTPRINT : SETBACK_FOOTPRINT;

    units.push({
      id: `F${String(f).padStart(2, "0")}`,
      ulpin: makeUlpin3d("F", f, f),
      verticalType: "F",
      floorIndex: f,
      label: meta.label,
      usageType: meta.usage,
      asBuilt,
      sanctioned,
      zMin: f * FLOOR_HEIGHT,
      zMax: (f + 1) * FLOOR_HEIGHT,
      colorKey: meta.colorKey,
      owner: meta.owner,
      udsPercent: f === 0 ? 0 : Number((100 / 6).toFixed(4)),
      reraRegistered: f <= 5,
      lien: liens[f] || null,
      saleDeedHash: `0x${(f * 7919).toString(16).padStart(4, "0")}…a${f}f2`,
      kycStatus: f <= 5 ? "VERIFIED" : "PENDING",
      cadastralStatus: f === 6 ? "DISPUTED" : "OC_ISSUED_ACTIVE",
    });
  }

  return units;
}

/** Synthetic surrounding blocks so the scene reads as a city rather than
 *  one building floating on a globe. Replaced by real OSM Buildings when a
 *  Cesium ion token is supplied. Offsets are local-ENU metres. */
const NEIGHBOUR_BLOCKS = [
  { dx: -42, dy: -18, w: 18, d: 14, floors: 4 },
  { dx: -38, dy: 22, w: 16, d: 20, floors: 7 },
  { dx: 36, dy: -26, w: 20, d: 16, floors: 5 },
  { dx: 34, dy: 24, w: 14, d: 18, floors: 9 },
  { dx: -6, dy: 46, w: 26, d: 16, floors: 3 },
  { dx: 8, dy: -48, w: 22, d: 18, floors: 6 },
  { dx: -70, dy: 8, w: 20, d: 22, floors: 11 },
  { dx: 68, dy: -6, w: 18, d: 20, floors: 8 },
  { dx: -64, dy: -44, w: 24, d: 16, floors: 5 },
  { dx: 60, dy: 44, w: 20, d: 18, floors: 6 },
];

/* ===========================================================================
 * CESIUM VIEWER CONSTRUCTION
 * =========================================================================== */

/** Cross-version viewer options. `imageryProvider` was deprecated in
 *  CesiumJS 1.104 and removed in 1.107 in favour of `baseLayer`; each
 *  version reads the key it knows and ignores the other, so passing both
 *  as false reliably suppresses the default ion base layer either way.
 *  Imagery is then attached post-construction via the stable
 *  imageryLayers.addImageryProvider() API. */
function buildViewerOptions() {
  return {
    baseLayer: false,
    imageryProvider: false,
    baseLayerPicker: false,
    geocoder: false,
    homeButton: false,
    sceneModePicker: false,
    navigationHelpButton: false,
    animation: false,
    timeline: false,
    fullscreenButton: false,
    infoBox: false,
    selectionIndicator: false,
    shouldAnimate: false,
  };
}

/* ===========================================================================
 * COMPONENT
 * =========================================================================== */

export default function CadastreViewer3D() {
  const containerRef = useRef(null);
  const viewerRef = useRef(null);
  const entityUnitMap = useRef(new Map());
  const strataEntitiesRef = useRef([]);
  const selectedEntityRef = useRef(null);

  const [viewerReady, setViewerReady] = useState(false);
  const [initError, setInitError] = useState(null);
  const [tier, setTier] = useState("public"); // "public" | "registrar"
  const [deviationMode, setDeviationMode] = useState(false);
  const [explodeFactor, setExplodeFactor] = useState(1);
  const [maxVisibleFloor, setMaxVisibleFloor] = useState(6);
  const [showBasement, setShowBasement] = useState(true);
  const [selectedUnit, setSelectedUnit] = useState(null);

  const units = useMemo(() => loadCadastreData(), []);

  const totalUnauthorisedVolume = useMemo(() => {
    let total = 0;
    for (const unit of units) {
      const height = unit.zMax - unit.zMin;
      const builtArea = polygonAreaSqm(unit.asBuilt);
      const sanctionedArea = unit.sanctioned ? polygonAreaSqm(unit.sanctioned) : 0;
      total += Math.max(0, builtArea - sanctionedArea) * height;
    }
    return total;
  }, [units]);

  /* ---------- 1. Create the viewer once ---------- */
  useEffect(() => {
    if (!containerRef.current) return undefined;

    let viewer;
    try {
      if (CESIUM_ION_TOKEN) {
        Cesium.Ion.defaultAccessToken = CESIUM_ION_TOKEN;
      }

      viewer = new Cesium.Viewer(containerRef.current, buildViewerOptions());
      viewerRef.current = viewer;

      // Imagery, attached post-construction so it works on every version.
      try {
        viewer.imageryLayers.addImageryProvider(
          new Cesium.OpenStreetMapImageryProvider({
            url: "https://tile.openstreetmap.org/",
          })
        );
      } catch (imageryError) {
        // A viewer with no imagery is still perfectly usable for volumetric
        // cadastre work — the buildings are the point, not the basemap.
        console.warn("Base imagery failed to load; continuing without it.", imageryError);
      }

      viewer.scene.globe.depthTestAgainstTerrain = false;
      viewer.scene.skyAtmosphere.show = true;

      // Real surrounding city geometry, only when a token is available.
      if (CESIUM_ION_TOKEN && typeof Cesium.createOsmBuildingsAsync === "function") {
        Cesium.createOsmBuildingsAsync()
          .then((tileset) => {
            if (!viewer.isDestroyed()) viewer.scene.primitives.add(tileset);
          })
          .catch((err) => console.warn("OSM Buildings unavailable:", err));
      }

      viewer.camera.setView({
        destination: Cesium.Cartesian3.fromDegrees(BASE_LON, BASE_LAT - 0.0011, 150),
        orientation: {
          heading: Cesium.Math.toRadians(20),
          pitch: Cesium.Math.toRadians(-32),
          roll: 0,
        },
      });

      // Click-to-inspect.
      viewer.screenSpaceEventHandler.setInputAction((movement) => {
        const picked = viewer.scene.pick(movement.position);
        if (Cesium.defined(picked) && picked.id && entityUnitMap.current.has(picked.id.id)) {
          setSelectedUnit(entityUnitMap.current.get(picked.id.id));
          selectedEntityRef.current = picked.id.id;
        } else {
          setSelectedUnit(null);
          selectedEntityRef.current = null;
        }
      }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

      // Neighbouring blocks — added once, never rebuilt.
      for (const [index, block] of NEIGHBOUR_BLOCKS.entries()) {
        const footprint = rectFootprint(block.w, block.d).map(([x, y]) => [
          x + block.dx,
          y + block.dy,
        ]);
        viewer.entities.add({
          id: `neighbour-${index}`,
          polygon: {
            hierarchy: Cesium.Cartesian3.fromDegreesArray(
              footprintToDegreesArray(footprint, BASE_LAT, BASE_LON)
            ),
            height: 0,
            extrudedHeight: block.floors * FLOOR_HEIGHT,
            material: PALETTE.neighbour.withAlpha(0.55),
            outline: true,
            outlineColor: Cesium.Color.fromCssColorString("#4a6b7a").withAlpha(0.7),
          },
        });
      }

      setViewerReady(true);
    } catch (err) {
      console.error("Cesium viewer failed to initialise:", err);
      setInitError(err instanceof Error ? err.message : String(err));
    }

    return () => {
      if (viewer && !viewer.isDestroyed()) viewer.destroy();
      viewerRef.current = null;
      entityUnitMap.current.clear();
      strataEntitiesRef.current = [];
    };
  }, []);

  /* ---------- 2. Rebuild the strata volumes when controls change ---------- */
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed() || !viewerReady) return;

    for (const entity of strataEntitiesRef.current) {
      viewer.entities.remove(entity);
    }
    strataEntitiesRef.current = [];
    entityUnitMap.current.clear();

    const addVolume = ({ id, footprint, holes, zMin, zMax, color, unit, alpha }) => {
      const hierarchy = holes
        ? new Cesium.PolygonHierarchy(
            Cesium.Cartesian3.fromDegreesArray(
              footprintToDegreesArray(footprint, BASE_LAT, BASE_LON)
            ),
            [
              new Cesium.PolygonHierarchy(
                Cesium.Cartesian3.fromDegreesArray(
                  footprintToDegreesArray(holes, BASE_LAT, BASE_LON)
                )
              ),
            ]
          )
        : Cesium.Cartesian3.fromDegreesArray(
            footprintToDegreesArray(footprint, BASE_LAT, BASE_LON)
          );

      const entity = viewer.entities.add({
        id,
        polygon: {
          hierarchy,
          height: zMin,
          extrudedHeight: zMax,
          material: color.withAlpha(alpha ?? 0.82),
          outline: true,
          outlineColor: color.brighten(0.4, new Cesium.Color()),
        },
      });
      strataEntitiesRef.current.push(entity);
      if (unit) entityUnitMap.current.set(id, unit);
      return entity;
    };

    for (const unit of units) {
      if (unit.floorIndex < 0 && !showBasement) continue;
      if (unit.floorIndex > maxVisibleFloor) continue;

      // Exploded view: separate floors vertically without changing their
      // own thickness, so relative storey heights stay readable.
      const gap = (explodeFactor - 1) * FLOOR_HEIGHT * 1.6;
      const lift = unit.floorIndex >= 0 ? unit.floorIndex * gap : 0;
      const zMin = unit.zMin + lift;
      const zMax = unit.zMax + lift;

      if (!deviationMode) {
        addVolume({
          id: `unit-${unit.id}`,
          footprint: unit.asBuilt,
          zMin,
          zMax,
          color: PALETTE[unit.colorKey] || PALETTE.common,
          unit,
        });
        continue;
      }

      // Deviation inspection mode: sanctioned envelope green, anything
      // built beyond it red. Where a floor overran a setback, the red part
      // is the ring between the two footprints — rendered as a polygon
      // with the sanctioned envelope punched out as a hole.
      if (!unit.sanctioned) {
        addVolume({
          id: `unit-${unit.id}-unauthorised`,
          footprint: unit.asBuilt,
          zMin,
          zMax,
          color: PALETTE.unauthorised,
          unit: { ...unit, deviationType: "UNAUTHORISED_ADDITIONAL_FLOOR" },
        });
        continue;
      }

      addVolume({
        id: `unit-${unit.id}-sanctioned`,
        footprint: unit.sanctioned,
        zMin,
        zMax,
        color: PALETTE.sanctioned,
        unit,
        alpha: 0.7,
      });

      const builtArea = polygonAreaSqm(unit.asBuilt);
      const sanctionedArea = polygonAreaSqm(unit.sanctioned);
      if (builtArea - sanctionedArea > 0.01) {
        addVolume({
          id: `unit-${unit.id}-encroachment`,
          footprint: unit.asBuilt,
          holes: unit.sanctioned,
          zMin,
          zMax,
          color: PALETTE.unauthorised,
          unit: { ...unit, deviationType: "SETBACK_OR_FOOTPRINT_ENCROACHMENT" },
        });
      }
    }

    // Keep any open inspect panel pointing at an entity that still exists.
    if (selectedEntityRef.current && !entityUnitMap.current.has(selectedEntityRef.current)) {
      setSelectedUnit(null);
      selectedEntityRef.current = null;
    }
  }, [units, viewerReady, deviationMode, explodeFactor, maxVisibleFloor, showBasement]);

  const resetView = useCallback(() => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed()) return;
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(BASE_LON, BASE_LAT - 0.0011, 150),
      orientation: {
        heading: Cesium.Math.toRadians(20),
        pitch: Cesium.Math.toRadians(-32),
        roll: 0,
      },
      duration: 1.2,
    });
  }, []);

  return (
    <div className="strata-root">
      <style>{STYLES}</style>

      <div ref={containerRef} className="strata-canvas" />

      {initError && (
        <div className="strata-error">
          <strong>The 3D viewer could not start.</strong>
          <p>{initError}</p>
          <p>
            This usually means Cesium&rsquo;s static assets aren&rsquo;t being served — check that
            <code> vite-plugin-cesium </code> is in your vite config — or that the browser has no
            WebGL support.
          </p>
        </div>
      )}

      <header className="strata-topbar">
        <div>
          <div className="strata-title">Shreeji Heights</div>
          <div className="strata-parcel">{BASE_ULPIN} · Ahmedabad, Gujarat</div>
        </div>
        <div className="strata-stats">
          <div className="strata-stat">
            <div className="strata-stat-value">{units.length}</div>
            <div className="strata-stat-label">registered units</div>
          </div>
          <div className="strata-stat">
            <div className="strata-stat-value">{totalUnauthorisedVolume.toFixed(0)} m³</div>
            <div className="strata-stat-label">unauthorised volume</div>
          </div>
        </div>
      </header>

      <aside className="strata-panel">
        {selectedUnit ? (
          <InspectPanel unit={selectedUnit} tier={tier} onClose={() => setSelectedUnit(null)} />
        ) : (
          <div className="strata-empty">
            <h2>Inspect a unit</h2>
            <p>
              Click any volume to open its 3D-ULPIN record. Switch to deviation mode to see
              sanctioned volume in green and unauthorised construction in red.
            </p>
          </div>
        )}
      </aside>

      <div className="strata-controls">
        <div className="strata-group">
          <label className="strata-group-label">Access tier (DPDP Act 2023)</label>
          <div className="strata-segmented">
            <button
              type="button"
              className={tier === "public" ? "active" : ""}
              onClick={() => setTier("public")}
            >
              Citizen
            </button>
            <button
              type="button"
              className={tier === "registrar" ? "active" : ""}
              onClick={() => setTier("registrar")}
            >
              Registrar
            </button>
          </div>
        </div>

        <div className="strata-group">
          <label className="strata-group-label">Inspection</label>
          <button
            type="button"
            className={`strata-toggle ${deviationMode ? "on" : ""}`}
            onClick={() => setDeviationMode((v) => !v)}
          >
            {deviationMode ? "Deviation mode: ON" : "Deviation mode: OFF"}
          </button>
        </div>

        <div className="strata-group">
          <label className="strata-group-label" htmlFor="explode">
            Exploded view
          </label>
          <input
            id="explode"
            type="range"
            min="1"
            max="4"
            step="0.05"
            value={explodeFactor}
            onChange={(e) => setExplodeFactor(Number(e.target.value))}
          />
        </div>

        <div className="strata-group">
          <label className="strata-group-label" htmlFor="slicer">
            Floor slicer — up to floor {maxVisibleFloor}
          </label>
          <input
            id="slicer"
            type="range"
            min="0"
            max="6"
            step="1"
            value={maxVisibleFloor}
            onChange={(e) => setMaxVisibleFloor(Number(e.target.value))}
          />
          <label className="strata-check">
            <input
              type="checkbox"
              checked={showBasement}
              onChange={(e) => setShowBasement(e.target.checked)}
            />
            Show basement
          </label>
        </div>

        <button type="button" className="strata-ghost" onClick={resetView}>
          Reset view
        </button>
      </div>

      <div className="strata-legend">
        {(deviationMode
          ? [
              { color: "#4f9a63", label: "Sanctioned volume" },
              { color: "#d64545", label: "Unauthorised construction" },
            ]
          : [
              { color: "#d6b24a", label: "Residential" },
              { color: "#c1594f", label: "Commercial" },
              { color: "#7c8a99", label: "Common / parking" },
              { color: "#33505f", label: "Neighbouring parcels" },
            ]
        ).map((item) => (
          <div key={item.label} className="strata-legend-row">
            <span className="strata-swatch" style={{ background: item.color }} />
            {item.label}
          </div>
        ))}
      </div>

      <div className="strata-tag">PS SIH26011 · Strata Cadastre</div>
    </div>
  );
}

/* ===========================================================================
 * INSPECT PANEL — DPDP-tiered field visibility
 * =========================================================================== */

function InspectPanel({ unit, tier, onClose }) {
  const isRegistrar = tier === "registrar";
  const segments = breakdownUlpin(unit.ulpin);

  return (
    <div>
      <div className="strata-panel-head">
        <h2>{unit.label}</h2>
        <button type="button" className="strata-close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>

      <div className="strata-ulpin">{unit.ulpin}</div>
      <div className="strata-ulpin-breakdown">
        {segments.map((seg) => (
          <div key={seg.label} className="strata-seg">
            <span className="strata-seg-value">{seg.value}</span>
            <span className="strata-seg-label">{seg.label}</span>
          </div>
        ))}
      </div>

      {unit.deviationType && (
        <div className="strata-flag">{unit.deviationType.replace(/_/g, " ")}</div>
      )}

      <Field label="Vertical type" value={VERTICAL_TYPE_LABELS[unit.verticalType]} />
      <Field label="Usage" value={unit.usageType} />
      <Field
        label="Elevation (Z)"
        value={`${unit.zMin.toFixed(1)} – ${unit.zMax.toFixed(1)} m`}
        mono
      />
      <Field label="Carpet area" value={`${polygonAreaSqm(unit.asBuilt).toFixed(1)} m²`} mono />
      <Field label="UDS share" value={`${unit.udsPercent}%`} mono />
      <Field label="Cadastral status" value={unit.cadastralStatus.replace(/_/g, " ")} />
      <Field label="RERA" value={unit.reraRegistered ? "Registered" : "Not registered"} />

      <div className="strata-tier-divider">
        {isRegistrar ? "Registrar-tier fields" : "Restricted under DPDP Act 2023"}
      </div>

      {isRegistrar ? (
        <>
          <Field label="Owner" value={unit.owner} />
          <Field label="KYC status" value={unit.kycStatus} />
          <Field label="Sale deed hash" value={unit.saleDeedHash} mono />
          <Field
            label="Lien"
            value={
              unit.lien
                ? `${unit.lien.lender} — ${unit.lien.amount} (${unit.lien.reference})`
                : "No active lien"
            }
          />
        </>
      ) : (
        <div className="strata-redacted">
          Owner identity, KYC status, sale deed hash and encumbrance detail are withheld from
          the citizen tier. Switch to Registrar to view.
        </div>
      )}
    </div>
  );
}

function Field({ label, value, mono }) {
  return (
    <div className="strata-field">
      <span className="strata-field-label">{label}</span>
      <span className={`strata-field-value${mono ? " mono" : ""}`}>{value}</span>
    </div>
  );
}

/* ===========================================================================
 * STYLES
 * =========================================================================== */

const STYLES = `
.strata-root {
  --void: #0a121c; --slate: #101c28; --hairline: #24404a; --hairline-soft: #17272e;
  --paper: #dce7e8; --paper-dim: #8fa6ac; --paper-faint: #5c747a;
  --signal: #2fd1c4; --danger: #d64545;
  position: fixed; inset: 0; background: var(--void); color: var(--paper);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  overflow: hidden;
}
.strata-canvas { position: absolute; inset: 0; }
.strata-canvas .cesium-widget-credits { display: none !important; }

.strata-topbar {
  position: absolute; top: 0; left: 0; right: 340px; height: 62px; z-index: 10;
  display: flex; align-items: center; justify-content: space-between; padding: 0 20px;
  background: linear-gradient(to bottom, rgba(10,18,28,0.94), rgba(10,18,28,0.6) 75%, transparent);
  pointer-events: none;
}
.strata-title { font-size: 16px; font-weight: 600; }
.strata-parcel { font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; color: var(--signal); margin-top: 2px; }
.strata-stats { display: flex; gap: 24px; }
.strata-stat { text-align: right; }
.strata-stat-value { font-family: ui-monospace, Menlo, monospace; font-size: 15px; font-weight: 600; }
.strata-stat-label { font-size: 10.5px; color: var(--paper-dim); margin-top: 2px; }

.strata-panel {
  position: absolute; top: 0; right: 0; bottom: 0; width: 340px; z-index: 12;
  background: var(--slate); border-left: 1px solid var(--hairline);
  padding: 20px; overflow-y: auto;
}
.strata-panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.strata-panel-head h2 { font-size: 16px; margin: 0 0 12px; }
.strata-close { background: none; border: none; color: var(--paper-dim); font-size: 20px; line-height: 1; cursor: pointer; }
.strata-close:hover { color: var(--paper); }
.strata-empty h2 { font-size: 12.5px; color: var(--paper-dim); margin: 0 0 12px; padding-left: 8px; border-left: 3px solid var(--hairline); }
.strata-empty p { font-size: 13px; line-height: 1.65; color: var(--paper-dim); margin: 0; }

.strata-ulpin {
  font-family: ui-monospace, Menlo, monospace; font-size: 14px; font-weight: 600;
  word-break: break-all; padding: 10px 12px; background: var(--void);
  border-left: 3px solid var(--signal); border-radius: 3px;
}
.strata-ulpin-breakdown { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 16px; }
.strata-seg { flex: 1 1 auto; background: var(--void); border: 1px solid var(--hairline-soft); border-radius: 3px; padding: 6px 8px; }
.strata-seg-value { display: block; font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--signal); }
.strata-seg-label { display: block; font-size: 9.5px; color: var(--paper-faint); margin-top: 2px; }

.strata-flag {
  background: rgba(214,69,69,0.14); border: 1px solid var(--danger); color: #f0a8a8;
  border-radius: 3px; padding: 8px 10px; font-size: 11.5px; margin-bottom: 14px; letter-spacing: 0.2px;
}

.strata-field { display: flex; justify-content: space-between; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--hairline-soft); }
.strata-field-label { font-size: 12px; color: var(--paper-dim); flex-shrink: 0; }
.strata-field-value { font-size: 13px; text-align: right; }
.strata-field-value.mono { font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; }

.strata-tier-divider { margin: 18px 0 8px; font-size: 11px; color: var(--paper-faint); text-transform: uppercase; letter-spacing: 0.6px; }
.strata-redacted { font-size: 12.5px; line-height: 1.6; color: var(--paper-faint); background: var(--void); border: 1px dashed var(--hairline); border-radius: 3px; padding: 12px; }

.strata-controls {
  position: absolute; left: 20px; bottom: 20px; z-index: 12; width: 224px;
  background: rgba(16,28,40,0.92); border: 1px solid var(--hairline); border-radius: 3px;
  padding: 16px; display: flex; flex-direction: column; gap: 16px;
}
.strata-group-label { display: block; font-size: 11.5px; font-weight: 600; color: var(--paper-dim); margin-bottom: 8px; padding-left: 8px; border-left: 3px solid var(--hairline); }
.strata-segmented { display: flex; border: 1px solid var(--hairline); border-radius: 3px; overflow: hidden; }
.strata-segmented button { flex: 1; background: none; border: none; color: var(--paper-dim); font-size: 12px; padding: 7px 8px; cursor: pointer; }
.strata-segmented button.active { background: var(--signal); color: var(--void); font-weight: 600; }
.strata-toggle { width: 100%; background: none; border: 1px solid var(--hairline); border-radius: 3px; color: var(--paper-dim); font-size: 12px; padding: 8px; cursor: pointer; }
.strata-toggle.on { border-color: var(--danger); color: #f0a8a8; }
.strata-controls input[type="range"] { width: 100%; accent-color: var(--signal); }
.strata-check { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--paper-dim); margin-top: 10px; }
.strata-ghost { background: none; border: 1px solid var(--hairline); border-radius: 3px; color: var(--paper-dim); font-size: 12px; padding: 8px; cursor: pointer; }
.strata-ghost:hover { border-color: var(--signal); color: var(--paper); }

.strata-legend {
  position: absolute; left: 20px; bottom: 300px; z-index: 12; width: 224px;
  background: rgba(16,28,40,0.9); border: 1px solid var(--hairline); border-radius: 3px; padding: 12px 14px;
}
.strata-legend-row { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--paper-dim); padding: 3px 0; }
.strata-swatch { width: 10px; height: 10px; flex-shrink: 0; border-radius: 2px; }

.strata-tag { position: absolute; right: 356px; bottom: 12px; z-index: 12; font-family: ui-monospace, Menlo, monospace; font-size: 10.5px; color: var(--paper-faint); }

.strata-error {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); z-index: 30;
  max-width: 420px; background: var(--slate); border: 1px solid var(--danger); border-radius: 3px;
  padding: 20px; font-size: 13px; line-height: 1.6;
}
.strata-error code { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--signal); }

@media (max-width: 900px) {
  .strata-panel { width: 100%; top: auto; height: 46vh; border-left: none; border-top: 1px solid var(--hairline); }
  .strata-topbar { right: 0; }
  .strata-legend { display: none; }
  .strata-controls { left: 12px; right: 12px; width: auto; bottom: calc(46vh + 12px); }
  .strata-tag { display: none; }
}
`;
