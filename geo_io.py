"""
geo_io.py — Strata Cadastre (SIH26011)
GIS parcel layers and DEM/DSM raster ingestion.

Closes two problem-statement requirements:
  - "GIS parcel layers" as an integrated input source
  - "Digital Elevation Models (DEM/DSM)" as an integrated input source

and one Expected-Solution requirement:
  - "scalable and INTEROPERABLE 3D cadastral framework" — via GeoJSON
    export of 3D units, so the cadastre is readable by QGIS, ArcGIS,
    Leaflet, and any OGC API - Features client.

FORMATS
-------
IN   GeoJSON (.geojson/.json) — parcel boundaries. Pure-Python, no deps.
IN   ESRI ASCII Grid (.asc)   — DEM/DSM. Pure-Python, no deps.
OUT  GeoJSON                  — 2D footprints or 3D volumetric units.

WHY ASCII GRID AND NOT GEOTIFF
-------------------------------
GeoTIFF is the format DEMs actually ship in (Bhuvan/CartoDEM, Copernicus
GLO-30), and reading it properly needs rasterio or GDAL — neither of
which could be installed or tested in this build environment. Rather than
ship an unverified GeoTIFF reader, this module reads ESRI ASCII Grid,
which is a genuine, widely-supported interchange format that is trivially
parseable and therefore actually testable.

Converting is one command, and GDAL is present wherever QGIS is:

    gdal_translate -of AAIGrid input_dem.tif output_dem.asc

read_dem() also uses rasterio automatically when it IS installed, so on a
machine with the full stack you can pass a .tif directly.

COORDINATE HANDLING
-------------------
GeoJSON is WGS84 lon/lat by specification (RFC 7946). pipeline.py works in
local ENU metres. geojson_to_local_enu() converts, using the same
metres-per-degree approximation as CadastreViewer3D.jsx so the viewer and
the pipeline agree. That approximation is accurate to well under a metre
over a single parcel and breaks down over tens of kilometres — fine for
one building, wrong for a district-wide layer. Use a projected CRS
(EPSG:32643 for Gujarat) via pyproj if you need the latter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# Same constants as CadastreViewer3D.jsx — keep these in sync.
METRES_PER_DEG_LAT = 111320.0


def metres_per_deg_lon(latitude_deg: float) -> float:
    return 111320.0 * np.cos(np.radians(latitude_deg))


# ============================================================================
# GeoJSON input — parcel layers
# ============================================================================

def _ring_to_array(ring: List) -> np.ndarray:
    """GeoJSON rings are closed (last point repeats the first). Drop the
    duplicate — every polygon routine in this codebase treats rings as
    implicitly closed, and a repeated vertex silently adds a zero-length
    edge that breaks Douglas-Peucker simplification and shoelace winding."""
    arr = np.array([[float(pt[0]), float(pt[1])] for pt in ring], dtype=np.float64)
    if len(arr) > 1 and np.allclose(arr[0], arr[-1]):
        arr = arr[:-1]
    return arr


def read_geojson_parcels(path: str | Path) -> List[Dict]:
    """Read parcel polygons from a GeoJSON file.

    Accepts FeatureCollection, Feature, or a bare geometry. Handles
    Polygon and MultiPolygon; a MultiPolygon becomes one entry per part,
    with its properties copied to each.

    Returns a list of dicts: {"boundary": (N,2) lon/lat array, "holes":
    [...], "properties": {...}}.
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))

    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
    elif data.get("type") == "Feature":
        features = [data]
    elif "coordinates" in data:
        features = [{"type": "Feature", "geometry": data, "properties": {}}]
    else:
        raise ValueError(
            f"{path} is not recognisable GeoJSON — expected FeatureCollection, "
            "Feature, or a geometry object."
        )

    parcels: List[Dict] = []
    for feature in features:
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        gtype = geometry.get("type")
        coords = geometry.get("coordinates")
        if not coords:
            continue

        if gtype == "Polygon":
            polygons = [coords]
        elif gtype == "MultiPolygon":
            polygons = coords
        else:
            continue  # points and lines are not parcels

        for polygon in polygons:
            if not polygon:
                continue
            parcels.append({
                "boundary": _ring_to_array(polygon[0]),
                "holes": [_ring_to_array(r) for r in polygon[1:]],
                "properties": dict(properties),
            })

    if not parcels:
        raise ValueError(f"No Polygon or MultiPolygon features found in {path}")
    return parcels


def geojson_to_local_enu(boundary_lonlat: np.ndarray,
                          origin: Optional[Tuple[float, float]] = None
                          ) -> Tuple[np.ndarray, Dict]:
    """Convert a lon/lat ring to local ENU metres.

    Returns the converted ring and the origin used. KEEP THE ORIGIN — it
    is what converts results back to real-world coordinates, and every
    other module in this system (pipeline, viewer, geodesy) needs the same
    one to agree with each other.
    """
    boundary_lonlat = np.asarray(boundary_lonlat, dtype=np.float64)
    if origin is None:
        origin = (float(boundary_lonlat[:, 0].mean()), float(boundary_lonlat[:, 1].mean()))

    origin_lon, origin_lat = origin
    local = np.empty_like(boundary_lonlat)
    local[:, 0] = (boundary_lonlat[:, 0] - origin_lon) * metres_per_deg_lon(origin_lat)
    local[:, 1] = (boundary_lonlat[:, 1] - origin_lat) * METRES_PER_DEG_LAT
    return local, {"origin_lon": origin_lon, "origin_lat": origin_lat}


def local_enu_to_lonlat(local_xy: np.ndarray, origin: Dict) -> np.ndarray:
    """Inverse of geojson_to_local_enu()."""
    local_xy = np.asarray(local_xy, dtype=np.float64)
    origin_lon, origin_lat = origin["origin_lon"], origin["origin_lat"]
    out = np.empty_like(local_xy)
    out[:, 0] = local_xy[:, 0] / metres_per_deg_lon(origin_lat) + origin_lon
    out[:, 1] = local_xy[:, 1] / METRES_PER_DEG_LAT + origin_lat
    return out


# ============================================================================
# GeoJSON output — interoperability
# ============================================================================

def units_to_geojson(units: List[Dict], origin: Dict,
                      include_3d: bool = True) -> Dict:
    """Export 3D cadastral units as a GeoJSON FeatureCollection.

    Each unit becomes a Polygon whose vertices carry Z (the unit's base
    elevation) when include_3d is set — RFC 7946 permits a third position
    element, and QGIS, CesiumJS and PostGIS all read it. The full
    volumetric extent travels in the properties as z_min/z_max, because
    GeoJSON has no native solid type; a client that needs the true volume
    extrudes between them.

    For genuine volumetric interchange (CityGML/CityJSON LOD2, OGC 3D
    Tiles) this is the intermediate step, not the destination.
    """
    features = []
    for unit in units:
        footprint = np.asarray(unit["footprint_m"], dtype=np.float64)
        lonlat = local_enu_to_lonlat(footprint, origin)

        ring = []
        for i in range(len(lonlat)):
            if include_3d:
                ring.append([round(float(lonlat[i][0]), 9),
                              round(float(lonlat[i][1]), 9),
                              round(float(unit.get("z_min", 0.0)), 3)])
            else:
                ring.append([round(float(lonlat[i][0]), 9),
                              round(float(lonlat[i][1]), 9)])
        ring.append(list(ring[0]))   # GeoJSON rings must close

        properties = {
            "3d_ulpin": unit.get("3d_ulpin"),
            "unit_number": unit.get("unit_number"),
            "carpet_area_sqm": unit.get("carpet_area_sqm"),
            "z_min": unit.get("z_min"),
            "z_max": unit.get("z_max"),
            "layer_type": unit.get("layer_type"),
            "vertical_type": unit.get("vertical_type"),
        }
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {k: v for k, v in properties.items() if v is not None},
        })

    return {
        "type": "FeatureCollection",
        "name": "strata_cadastre_3d_units",
        # RFC 7946 fixes GeoJSON to WGS84, so this member is informational.
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }


def write_geojson(payload: Dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ============================================================================
# DEM / DSM raster input
# ============================================================================

def read_ascii_grid(path: str | Path) -> Tuple[np.ndarray, Dict]:
    """Read an ESRI ASCII Grid (.asc) DEM or DSM. No dependencies.

    Header is six lines: ncols, nrows, xllcorner/xllcenter,
    yllcorner/yllcenter, cellsize, NODATA_value (the last optional).
    NODATA cells become NaN so they propagate visibly through arithmetic
    rather than poisoning results with a sentinel like -9999.

    Returns (grid, metadata). Row 0 of the grid is the NORTHERNMOST row,
    which is the file's own order — do not flip it without also adjusting
    the transform in sample_grid().
    """
    path = Path(path)
    header: Dict[str, float] = {}
    values: List[float] = []

    expected_keys = {"ncols", "nrows", "xllcorner", "xllcenter",
                      "yllcorner", "yllcenter", "cellsize", "nodata_value"}

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if not parts:
                continue
            key = parts[0].lower()
            if key in expected_keys and len(parts) == 2:
                header[key] = float(parts[1])
            else:
                values.extend(float(v) for v in parts)

    for required in ("ncols", "nrows", "cellsize"):
        if required not in header:
            raise ValueError(f"{path} is missing the required '{required}' header line")

    ncols, nrows = int(header["ncols"]), int(header["nrows"])
    if len(values) != ncols * nrows:
        raise ValueError(
            f"{path} declares {ncols}x{nrows} = {ncols * nrows} cells "
            f"but contains {len(values)} values"
        )

    grid = np.array(values, dtype=np.float64).reshape(nrows, ncols)

    nodata = header.get("nodata_value")
    if nodata is not None:
        grid[grid == nodata] = np.nan

    cellsize = header["cellsize"]
    # Corner and centre conventions differ by half a cell.
    if "xllcenter" in header:
        xll = header["xllcenter"] - cellsize / 2
        yll = header["yllcenter"] - cellsize / 2
    else:
        xll = header.get("xllcorner", 0.0)
        yll = header.get("yllcorner", 0.0)

    metadata = {
        "ncols": ncols, "nrows": nrows, "cellsize": cellsize,
        "xllcorner": xll, "yllcorner": yll,
        "nodata_value": nodata,
        "bounds": {
            "min_x": xll, "max_x": xll + ncols * cellsize,
            "min_y": yll, "max_y": yll + nrows * cellsize,
        },
    }
    return grid, metadata


def read_dem(path: str | Path) -> Tuple[np.ndarray, Dict]:
    """Read a DEM/DSM. Uses rasterio for GeoTIFF when available, and falls
    back to the dependency-free ASCII Grid reader otherwise."""
    path = Path(path)
    if path.suffix.lower() in (".asc", ".txt", ".grd"):
        return read_ascii_grid(path)

    try:
        import rasterio
    except ImportError:
        raise ImportError(
            f"Reading {path.suffix} needs rasterio (pip install rasterio), or convert "
            f"once with:  gdal_translate -of AAIGrid {path.name} {path.stem}.asc"
        )

    with rasterio.open(str(path)) as src:
        grid = src.read(1).astype(np.float64)
        if src.nodata is not None:
            grid[grid == src.nodata] = np.nan
        transform = src.transform
        metadata = {
            "ncols": src.width, "nrows": src.height,
            "cellsize": abs(transform.a),
            "xllcorner": transform.c,
            "yllcorner": transform.f - src.height * abs(transform.e),
            "nodata_value": src.nodata,
            "crs": str(src.crs),
            "bounds": {
                "min_x": src.bounds.left, "max_x": src.bounds.right,
                "min_y": src.bounds.bottom, "max_y": src.bounds.top,
            },
        }
    return grid, metadata


def sample_grid(grid: np.ndarray, metadata: Dict, x: float, y: float) -> float:
    """Sample a DEM/DSM at one real-world coordinate (nearest cell).

    Returns NaN outside the grid or on a NODATA cell — never a fabricated
    elevation, because a silently invented ground height propagates into
    every plinth, volume and tax figure downstream.
    """
    cellsize = metadata["cellsize"]
    col = int((x - metadata["xllcorner"]) / cellsize)
    # Row 0 is the northernmost row, so the Y axis inverts here.
    row = int((metadata["bounds"]["max_y"] - y) / cellsize)

    if not (0 <= row < metadata["nrows"] and 0 <= col < metadata["ncols"]):
        return float("nan")
    return float(grid[row, col])


def compute_ndsm(dsm: np.ndarray, dem: np.ndarray) -> np.ndarray:
    """nDSM = DSM - DEM, the normalised height model.

    Both grids must share dimensions, cell size and origin. Mismatched
    grids are the classic silent error here: numpy will happily subtract
    two same-shaped arrays that cover different ground.
    """
    if dsm.shape != dem.shape:
        raise ValueError(
            f"DSM {dsm.shape} and DEM {dem.shape} differ in shape — resample "
            "them onto a common grid first (gdalwarp -tr / -te)."
        )
    return dsm - dem


def write_ascii_grid(grid: np.ndarray, path: str | Path, cellsize: float = 1.0,
                      xllcorner: float = 0.0, yllcorner: float = 0.0,
                      nodata_value: float = -9999.0) -> None:
    """Write an ESRI ASCII Grid. Used to test the reader, and to hand
    derived surfaces (nDSM) to QGIS."""
    grid = np.asarray(grid, dtype=np.float64)
    nrows, ncols = grid.shape
    out = np.where(np.isnan(grid), nodata_value, grid)

    lines = [
        f"ncols {ncols}", f"nrows {nrows}",
        f"xllcorner {xllcorner}", f"yllcorner {yllcorner}",
        f"cellsize {cellsize}", f"NODATA_value {nodata_value}",
    ]
    for row in out:
        lines.append(" ".join(f"{v:g}" for v in row))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import tempfile

    print("=" * 76)
    print("geo_io.py — GIS parcel layers + DEM/DSM ingestion")
    print("=" * 76)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # ---------------------------------------------------------------
        print("\n--- 1. GeoJSON parcel ingestion ---")
        # A 15 m x 11 m parcel near the Ahmedabad origin used throughout.
        base_lon, base_lat = 72.5714, 23.0225
        dlon = 15.0 / metres_per_deg_lon(base_lat)
        dlat = 11.0 / METRES_PER_DEG_LAT
        parcel_geojson = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"ulpin_2d": "11000088421000", "survey_number": "88421"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [base_lon, base_lat],
                        [base_lon + dlon, base_lat],
                        [base_lon + dlon, base_lat + dlat],
                        [base_lon, base_lat + dlat],
                        [base_lon, base_lat],
                    ]],
                },
            }],
        }
        gj_path = tmp_path / "parcels.geojson"
        gj_path.write_text(json.dumps(parcel_geojson))

        parcels = read_geojson_parcels(gj_path)
        print(f"  read {len(parcels)} parcel(s)")
        print(f"  properties: {parcels[0]['properties']}")
        print(f"  boundary vertices: {len(parcels[0]['boundary'])} (closing point dropped)")
        assert len(parcels) == 1
        assert len(parcels[0]["boundary"]) == 4, "closing duplicate was not dropped"

        local, origin = geojson_to_local_enu(parcels[0]["boundary"])
        width = local[:, 0].max() - local[:, 0].min()
        depth = local[:, 1].max() - local[:, 1].min()
        print(f"  converted to local ENU: {width:.3f} m x {depth:.3f} m (expected 15.000 x 11.000)")
        assert abs(width - 15.0) < 0.01, f"width {width}"
        assert abs(depth - 11.0) < 0.01, f"depth {depth}"

        back = local_enu_to_lonlat(local, origin)
        round_trip_error = float(np.abs(back - parcels[0]["boundary"]).max())
        print(f"  lon/lat round-trip error: {round_trip_error:.3e} degrees")
        assert round_trip_error < 1e-9

        # ---------------------------------------------------------------
        print("\n--- 2. MultiPolygon handling ---")
        multi = {
            "type": "Feature",
            "properties": {"ulpin_2d": "11000088422000"},
            "geometry": {"type": "MultiPolygon", "coordinates": [
                [[[72.571, 23.022], [72.572, 23.022], [72.572, 23.023], [72.571, 23.022]]],
                [[[72.573, 23.024], [72.574, 23.024], [72.574, 23.025], [72.573, 23.024]]],
            ]},
        }
        multi_path = tmp_path / "multi.geojson"
        multi_path.write_text(json.dumps(multi))
        multi_parcels = read_geojson_parcels(multi_path)
        print(f"  MultiPolygon expanded to {len(multi_parcels)} parcels, "
              f"properties copied to each: "
              f"{all(p['properties'].get('ulpin_2d') for p in multi_parcels)}")
        assert len(multi_parcels) == 2

        # ---------------------------------------------------------------
        print("\n--- 3. ASCII Grid DEM/DSM ---")
        nrows, ncols = 40, 50
        yy, xx = np.mgrid[0:nrows, 0:ncols]
        dem_grid = 55.0 + 0.02 * xx + 0.01 * yy          # gently sloping terrain
        dsm_grid = dem_grid.copy()
        dsm_grid[12:28, 15:35] += 19.2                    # a G+5 building
        dsm_grid[5:9, 40:45] += 7.0                       # a tree cluster
        dem_grid[0, 0] = np.nan                            # a NODATA cell

        dem_path = tmp_path / "terrain.asc"
        dsm_path = tmp_path / "surface.asc"
        write_ascii_grid(dem_grid, dem_path, cellsize=1.0, xllcorner=457000.0, yllcorner=2545000.0)
        write_ascii_grid(dsm_grid, dsm_path, cellsize=1.0, xllcorner=457000.0, yllcorner=2545000.0)

        dem_read, dem_meta = read_dem(dem_path)
        dsm_read, dsm_meta = read_dem(dsm_path)
        print(f"  DEM: {dem_meta['ncols']} x {dem_meta['nrows']} cells @ "
              f"{dem_meta['cellsize']} m, origin ({dem_meta['xllcorner']:.0f}, "
              f"{dem_meta['yllcorner']:.0f})")
        assert dem_read.shape == (nrows, ncols)

        nan_preserved = bool(np.isnan(dem_read[0, 0]))
        print(f"  NODATA became NaN rather than -9999: {nan_preserved}")
        assert nan_preserved, "NODATA sentinel leaked into the data"

        valid = ~np.isnan(dem_grid)
        max_diff = float(np.abs(dem_read[valid] - dem_grid[valid]).max())
        print(f"  round-trip max error on valid cells: {max_diff:.2e} m")
        assert max_diff < 1e-6

        ndsm = compute_ndsm(dsm_read, dem_read)
        building_height = float(np.nanmax(ndsm))
        print(f"  nDSM peak height: {building_height:.2f} m (true 19.20 m)")
        assert abs(building_height - 19.2) < 0.01

        try:
            compute_ndsm(dsm_read, dem_read[:10, :10])
            raise AssertionError("expected a shape-mismatch error")
        except ValueError as exc:
            print(f"  mismatched grids correctly rejected: {str(exc)[:58]}...")

        # ---------------------------------------------------------------
        print("\n--- 4. Sampling the DEM at real-world coordinates ---")
        sampled = sample_grid(dem_read, dem_meta, x=457025.5, y=2545020.5)
        print(f"  DEM at (457025.5, 2545020.5) = {sampled:.3f} m")
        assert not np.isnan(sampled), "in-bounds sample returned NaN"

        outside = sample_grid(dem_read, dem_meta, x=999999.0, y=999999.0)
        print(f"  out-of-bounds sample returns NaN (not a fabricated value): {np.isnan(outside)}")
        assert np.isnan(outside)

        # ---------------------------------------------------------------
        print("\n--- 5. GeoJSON export for interoperability ---")
        units = [
            {"3d_ulpin": "24012411000088421000F0040402", "unit_number": "402",
             "footprint_m": [[-7.5, -5.5], [7.5, -5.5], [7.5, 5.5], [-7.5, 5.5]],
             "carpet_area_sqm": 165.0, "z_min": 12.8, "z_max": 16.0,
             "layer_type": "Above-Ground Residential", "vertical_type": "F"},
            {"3d_ulpin": "24012411000088421000T0060601", "unit_number": "601",
             "footprint_m": [[-6.0, -4.0], [6.0, -4.0], [6.0, 4.0], [-6.0, 4.0]],
             "carpet_area_sqm": 96.0, "z_min": 19.2, "z_max": 22.4,
             "layer_type": "Terrace / Air-Rights", "vertical_type": "T"},
        ]
        exported = units_to_geojson(units, origin)
        out_path = tmp_path / "units_3d.geojson"
        write_geojson(exported, out_path)
        print(f"  exported {len(exported['features'])} features "
              f"({out_path.stat().st_size:,} bytes)")

        first_ring = exported["features"][0]["geometry"]["coordinates"][0]
        print(f"  ring closes: {first_ring[0] == first_ring[-1]}")
        print(f"  positions carry Z: {len(first_ring[0]) == 3}")
        assert first_ring[0] == first_ring[-1], "GeoJSON ring must close"
        assert len(first_ring[0]) == 3, "3D export should carry Z"

        reread = read_geojson_parcels(out_path)
        print(f"  re-read exported file: {len(reread)} features (valid GeoJSON)")
        assert len(reread) == 2

    print("\n" + "=" * 76)
    print("All geo_io.py tests passed.")
    print("=" * 76)
