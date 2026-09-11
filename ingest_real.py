"""
ingest_real.py — Strata Cadastre (SIH26011)
Getting REAL drone/LiDAR/photogrammetry data into pipeline.py.

This replaces the NotImplementedError branch in main.py's run_ingest().
Everything here produces the exact DataFrame shape pipeline.py already
consumes — columns x, y, z, classification, in local ENU metres — so no
downstream code changes.

WHY THERE IS A PURE-PYTHON LAS READER IN HERE
----------------------------------------------
The obvious answer is `pip install laspy`. That is still the right answer
when it works, and read_las() below uses laspy automatically if it is
importable. But this module also carries a complete, dependency-free LAS
1.2/1.4 reader and writer built directly on `struct`, for three reasons:

  1. It was actually TESTED. laspy/PDAL/GDAL could not be installed in the
     environment this file was written in, so any code written against them
     would have shipped unverified. The pure-Python path was round-trip
     tested against files it wrote itself.
  2. Demo-day insurance. If pip fails on a teammate's laptop an hour before
     judging, the ingest path still works.
  3. It is readable. A judge asking "do you actually understand the LiDAR
     format or did you just call a library?" gets a real answer.

WHAT IT DOES NOT DO
-------------------
  - **.LAZ is not supported here.** LAZ is compressed LAS; decompressing it
    needs laszip/lazrs. Install `laspy[lazrs]`, or convert once with
    `laszip -i cloud.laz -o cloud.las` and use the .las.
  - Point formats 6-10 (LAS 1.4's new base) are read for X/Y/Z and
    classification only; the extra fields are skipped, not parsed.
  - No CRS reprojection. VLR/GeoTIFF CRS records are not decoded — you tell
    it the origin via recenter_to_local_enu(). For a single parcel that is
    sufficient and avoids a pyproj dependency.

WHERE TO ACTUALLY GET DATA (read this before writing any code)
---------------------------------------------------------------
Free sub-metre LiDAR covering Ahmedabad essentially does not exist in the
public domain. Realistic sources, best first:

  1. PHONE PHOTOGRAMMETRY — the one that actually works for a hackathon.
     Walk around a building taking 100-200 overlapping photos, process
     free in Meshroom (AliceVision) or COLMAP, export a dense point cloud
     as .ply, and load it with read_ply() below. This produces a genuine
     point cloud of a genuine Ahmedabad building, needs no drone permit,
     and costs nothing. Scale is arbitrary until you fix it — measure one
     real edge with a tape and use scale_point_cloud().
  2. AN ARCHITECT'S SANCTIONED PLAN (.dxf/.pdf) — from your field visits.
     This is the sanctioned-envelope half of the deviation detector and is
     far more valuable than any point cloud.
  3. OpenTopography — real LiDAR tiles, mostly US/EU. Useless as Ahmedabad
     evidence, but perfect for proving your LAS path handles real files.
  4. Google Open Buildings / Microsoft Global ML Building Footprints /
     OpenStreetMap — real 2D Ahmedabad footprints, some with
     `building:levels`. Real geometry, no Z.
  5. Bhuvan / CartoDEM (ISRO-NRSC) and Copernicus GLO-30 — free DEMs, but
     at ~30 m per pixel. A 15 m building is half a pixel. Use these for
     terrain context ONLY; they cannot extract buildings, and any claim
     that they can will not survive a knowledgeable judge.

DRONE FLIGHTS: check the DGCA Digital Sky zone map before flying anything.
Much of Ahmedabad sits in restricted airspace near the airport.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd


# ============================================================================
# LAS format constants (ASPRS LAS 1.2 / 1.4)
# ============================================================================

LAS_SIGNATURE = b"LASF"
LAS_HEADER_SIZE_12 = 227          # Public Header Block, LAS 1.2

# Byte sizes of each Point Data Record Format.
POINT_RECORD_LENGTHS = {
    0: 20, 1: 28, 2: 26, 3: 34, 4: 57, 5: 63,
    6: 30, 7: 36, 8: 38, 9: 59, 10: 67,
}

# ASPRS standard classification codes we care about.
CLASS_GROUND = 2
CLASS_BUILDING = 6


# ============================================================================
# Pure-Python LAS reader
# ============================================================================

def _parse_las_header(raw: bytes) -> Dict:
    """Decode the LAS Public Header Block. Field offsets are fixed by the
    ASPRS specification and identical for 1.2 and 1.4 up to byte 227."""
    if raw[0:4] != LAS_SIGNATURE:
        raise ValueError(
            f"Not a LAS file: expected signature {LAS_SIGNATURE!r}, got {raw[0:4]!r}. "
            "If this is a .laz, it is compressed — see this module's docstring."
        )

    version_major = raw[24]
    version_minor = raw[25]
    header_size = struct.unpack_from("<H", raw, 94)[0]
    offset_to_point_data = struct.unpack_from("<I", raw, 96)[0]
    point_format_id = raw[104] & 0b00111111      # high bits flag LAZ compression
    point_record_length = struct.unpack_from("<H", raw, 105)[0]
    legacy_point_count = struct.unpack_from("<I", raw, 107)[0]

    scale_x, scale_y, scale_z = struct.unpack_from("<3d", raw, 131)
    offset_x, offset_y, offset_z = struct.unpack_from("<3d", raw, 155)
    max_x, min_x, max_y, min_y, max_z, min_z = struct.unpack_from("<6d", raw, 179)

    point_count = legacy_point_count
    # LAS 1.4 moved the authoritative count to a 64-bit field at byte 247;
    # the legacy 32-bit field is 0 for files with >4.29e9 points.
    if version_major == 1 and version_minor >= 4 and header_size >= 375:
        wide_count = struct.unpack_from("<Q", raw, 247)[0]
        if wide_count:
            point_count = wide_count

    return {
        "version": f"{version_major}.{version_minor}",
        "header_size": header_size,
        "offset_to_point_data": offset_to_point_data,
        "point_format_id": point_format_id,
        "point_record_length": point_record_length,
        "point_count": point_count,
        "scale": (scale_x, scale_y, scale_z),
        "offset": (offset_x, offset_y, offset_z),
        "bounds": {"min": (min_x, min_y, min_z), "max": (max_x, max_y, max_z)},
    }


def read_las_pure(path: str | Path, max_points: Optional[int] = None) -> pd.DataFrame:
    """Read a .las file with no external dependencies.

    Returns a DataFrame with x, y, z (float, real-world units after scale
    and offset are applied) and classification (int) — the exact shape
    pipeline.py's functions expect.
    """
    path = Path(path)
    raw = path.read_bytes()
    header = _parse_las_header(raw)

    fmt = header["point_format_id"]
    if fmt not in POINT_RECORD_LENGTHS:
        raise ValueError(f"Unsupported point data record format: {fmt}")

    n = header["point_count"]
    if max_points is not None:
        n = min(n, max_points)

    rec_len = header["point_record_length"]
    start = header["offset_to_point_data"]
    needed = start + n * rec_len
    if len(raw) < needed:
        # Tolerate a truncated file rather than failing outright — partial
        # data is usually still useful, and silent wrong answers are worse
        # than a warning.
        n = (len(raw) - start) // rec_len
        print(f"  warning: file is shorter than its header claims; reading {n} points")

    body = np.frombuffer(raw, dtype=np.uint8, count=n * rec_len, offset=start)
    body = body.reshape(n, rec_len)

    # X, Y, Z are int32 at bytes 0-11 in every point format.
    xyz_int = body[:, 0:12].copy().view(np.int32).reshape(n, 3)

    scale = np.array(header["scale"])
    offset = np.array(header["offset"])
    xyz = xyz_int.astype(np.float64) * scale + offset

    # Classification byte position differs between the two format families:
    # formats 0-5 put it at byte 15; formats 6-10 moved it to byte 16.
    class_byte = 16 if fmt >= 6 else 15
    classification = body[:, class_byte].astype(np.int32)
    if fmt < 6:
        # In formats 0-5 the upper 3 bits are synthetic/key-point/withheld
        # flags, so the class itself is only the low 5 bits.
        classification = classification & 0b00011111

    return pd.DataFrame({
        "x": xyz[:, 0], "y": xyz[:, 1], "z": xyz[:, 2],
        "classification": classification,
    })


def read_las(path: str | Path, max_points: Optional[int] = None) -> pd.DataFrame:
    """Read a LAS/LAZ file. Uses laspy when available (which also unlocks
    .laz), and falls back to the dependency-free reader otherwise."""
    path = Path(path)
    try:
        import laspy  # noqa: F401
    except ImportError:
        if path.suffix.lower() == ".laz":
            raise ImportError(
                "Reading .laz needs compression support: pip install 'laspy[lazrs]', "
                "or convert once with: laszip -i input.laz -o input.las"
            )
        return read_las_pure(path, max_points)

    import laspy
    with laspy.open(str(path)) as reader:
        las = reader.read()
    df = pd.DataFrame({
        "x": np.asarray(las.x, dtype=np.float64),
        "y": np.asarray(las.y, dtype=np.float64),
        "z": np.asarray(las.z, dtype=np.float64),
        "classification": np.asarray(las.classification, dtype=np.int32),
    })
    return df.iloc[:max_points] if max_points else df


def write_las(df: pd.DataFrame, path: str | Path, scale: float = 0.001) -> None:
    """Write a DataFrame to LAS 1.2, point format 0. Dependency-free.

    Used to test the reader, and genuinely useful for converting a
    photogrammetry .ply into a LAS your GIS tooling will accept.
    """
    path = Path(path)
    n = len(df)
    x, y, z = df["x"].to_numpy(), df["y"].to_numpy(), df["z"].to_numpy()
    classification = df.get("classification", pd.Series(np.ones(n))).to_numpy().astype(np.uint8)

    off_x, off_y, off_z = float(x.min()), float(y.min()), float(z.min())

    header = bytearray(LAS_HEADER_SIZE_12)
    header[0:4] = LAS_SIGNATURE
    header[24] = 1                                              # version major
    header[25] = 2                                              # version minor
    header[26:58] = b"Strata Cadastre".ljust(32, b"\0")          # system identifier
    header[58:90] = b"ingest_real.py".ljust(32, b"\0")           # generating software
    struct.pack_into("<H", header, 94, LAS_HEADER_SIZE_12)
    struct.pack_into("<I", header, 96, LAS_HEADER_SIZE_12)       # offset to point data
    struct.pack_into("<I", header, 100, 0)                       # number of VLRs
    header[104] = 0                                              # point format 0
    struct.pack_into("<H", header, 105, POINT_RECORD_LENGTHS[0])
    struct.pack_into("<I", header, 107, n)
    struct.pack_into("<3d", header, 131, scale, scale, scale)
    struct.pack_into("<3d", header, 155, off_x, off_y, off_z)
    struct.pack_into("<6d", header, 179,
                     float(x.max()), off_x, float(y.max()), off_y, float(z.max()), off_z)

    xi = np.rint((x - off_x) / scale).astype(np.int32)
    yi = np.rint((y - off_y) / scale).astype(np.int32)
    zi = np.rint((z - off_z) / scale).astype(np.int32)

    records = np.zeros((n, POINT_RECORD_LENGTHS[0]), dtype=np.uint8)
    records[:, 0:12] = np.column_stack([xi, yi, zi]).view(np.uint8).reshape(n, 12)
    records[:, 15] = classification

    with open(path, "wb") as fh:
        fh.write(bytes(header))
        fh.write(records.tobytes())


# ============================================================================
# PLY reader — for photogrammetry output (Meshroom / COLMAP / RealityCapture)
# ============================================================================

def read_ply(path: str | Path, max_points: Optional[int] = None) -> pd.DataFrame:
    """Read an ASCII or binary-little-endian PLY point cloud.

    This is the path for phone photogrammetry: Meshroom and COLMAP both
    export dense clouds as .ply. Classification is set to 1 (unclassified)
    throughout — photogrammetry carries no ASPRS classes, so run
    pipeline.classify_ground_points() on the result to derive ground.
    """
    path = Path(path)
    with open(path, "rb") as fh:
        if fh.readline().strip() != b"ply":
            raise ValueError(f"{path} is not a PLY file")

        fmt = None
        vertex_count = 0
        properties: list[tuple[str, str]] = []
        in_vertex_element = False

        while True:
            line = fh.readline()
            if not line:
                raise ValueError("PLY header ended before end_header")
            parts = line.strip().split()
            if not parts:
                continue
            key = parts[0]
            if key == b"format":
                fmt = parts[1].decode()
            elif key == b"element":
                in_vertex_element = parts[1] == b"vertex"
                if in_vertex_element:
                    vertex_count = int(parts[2])
            elif key == b"property" and in_vertex_element:
                properties.append((parts[1].decode(), parts[2].decode()))
            elif key == b"end_header":
                break

        if fmt is None:
            raise ValueError("PLY header declared no format")
        if fmt == "ascii":
            rows = []
            for _ in range(vertex_count):
                rows.append([float(v) for v in fh.readline().split()[:3]])
            xyz = np.array(rows, dtype=np.float64)
        elif fmt == "binary_little_endian":
            np_types = {
                "float": np.float32, "float32": np.float32,
                "double": np.float64, "float64": np.float64,
                "uchar": np.uint8, "uint8": np.uint8, "char": np.int8, "int8": np.int8,
                "ushort": np.uint16, "uint16": np.uint16, "short": np.int16, "int16": np.int16,
                "uint": np.uint32, "uint32": np.uint32, "int": np.int32, "int32": np.int32,
            }
            dtype = np.dtype([(name, np_types[t]) for t, name in properties])
            data = np.frombuffer(fh.read(vertex_count * dtype.itemsize),
                                  dtype=dtype, count=vertex_count)
            xyz = np.column_stack([data["x"], data["y"], data["z"]]).astype(np.float64)
        else:
            raise ValueError(
                f"Unsupported PLY format {fmt!r}. Re-export as ASCII or "
                "binary_little_endian (both Meshroom and COLMAP can)."
            )

    if max_points:
        xyz = xyz[:max_points]

    return pd.DataFrame({
        "x": xyz[:, 0], "y": xyz[:, 1], "z": xyz[:, 2],
        "classification": np.ones(len(xyz), dtype=np.int32),
    })


def read_xyz(path: str | Path, delimiter: Optional[str] = None) -> pd.DataFrame:
    """Read a plain text XYZ / CSV point cloud (x y z [classification])."""
    arr = np.loadtxt(path, delimiter=delimiter)
    classification = (arr[:, 3].astype(np.int32) if arr.shape[1] >= 4
                      else np.ones(len(arr), dtype=np.int32))
    return pd.DataFrame({
        "x": arr[:, 0], "y": arr[:, 1], "z": arr[:, 2],
        "classification": classification,
    })


def load_point_cloud(path: str | Path, max_points: Optional[int] = None) -> pd.DataFrame:
    """Dispatch on file extension. The single entry point to use."""
    suffix = Path(path).suffix.lower()
    if suffix in (".las", ".laz"):
        return read_las(path, max_points)
    if suffix == ".ply":
        return read_ply(path, max_points)
    if suffix in (".xyz", ".txt", ".csv", ".pts"):
        return read_xyz(path)
    raise ValueError(
        f"Unrecognised point cloud extension {suffix!r}. "
        "Supported: .las .laz .ply .xyz .txt .csv .pts"
    )


# ============================================================================
# Georeferencing — real coordinates into pipeline.py's local ENU frame
# ============================================================================

def recenter_to_local_enu(df: pd.DataFrame,
                           origin: Optional[Tuple[float, float, float]] = None
                           ) -> Tuple[pd.DataFrame, Dict]:
    """pipeline.py works in local ENU metres with (0, 0) at the parcel
    centroid. Real LAS arrives in UTM or state-plane coordinates with large
    absolute values, which wrecks both the voxel grids and float precision.

    Pass the parcel origin explicitly (e.g. the CORS benchmark in the same
    CRS as the cloud), or leave it None to use the cloud's own XY centroid
    with Z anchored to its minimum.

    Returns the recentred frame plus the origin used, which you MUST keep —
    it's what converts results back to real-world coordinates, and
    geodesy.py needs the Z datum to compute orthometric elevation.
    """
    if origin is None:
        origin = (float(df["x"].mean()), float(df["y"].mean()), float(df["z"].min()))

    out = df.copy()
    out["x"] = df["x"] - origin[0]
    out["y"] = df["y"] - origin[1]
    out["z"] = df["z"] - origin[2]
    return out, {"origin_x": origin[0], "origin_y": origin[1], "origin_z": origin[2]}


def scale_point_cloud(df: pd.DataFrame, measured_distance_m: float,
                       point_a: Tuple[float, float, float],
                       point_b: Tuple[float, float, float]) -> Tuple[pd.DataFrame, float]:
    """Photogrammetry reconstructions are scale-free — Meshroom has no idea
    whether your building is 15 m or 15 km wide. Fix it by measuring one
    real distance on site with a tape, identifying the same two points in
    the cloud, and calling this.

    Skipping this step is the most common reason a photogrammetry pipeline
    produces confident, completely wrong volumes.
    """
    reconstructed = float(np.linalg.norm(np.array(point_b) - np.array(point_a)))
    if reconstructed <= 0:
        raise ValueError("point_a and point_b are the same point in the cloud")

    factor = measured_distance_m / reconstructed
    out = df.copy()
    out[["x", "y", "z"]] = df[["x", "y", "z"]] * factor
    return out, factor


def subsample(df: pd.DataFrame, max_points: int, seed: int = 42) -> pd.DataFrame:
    """Uniform random subsample. A real drone survey can run to tens of
    millions of points; the fusion pipeline does not need them all, and a
    demo that takes four minutes to render is a demo nobody watches."""
    if len(df) <= max_points:
        return df
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(df), size=max_points, replace=False)
    return df.iloc[np.sort(idx)].reset_index(drop=True)


# ============================================================================
# Sanctioned plan (DXF) — the other half of the deviation detector
# ============================================================================

def read_dxf_footprint(path: str | Path, layer: Optional[str] = None) -> np.ndarray:
    """Extract a closed footprint polygon from a DXF sanctioned plan.

    Needs ezdxf (`pip install ezdxf`), which could not be installed or
    tested in this build environment — so unlike everything above, this
    function is UNVERIFIED. Treat it as a starting point.

    Practical note from the real world: architects' DXFs are messy. The
    outer wall is rarely one clean LWPOLYLINE on a helpfully named layer.
    Expect to open the file in a viewer, find which layer actually holds
    the outer boundary, and pass its name explicitly. If that fights you,
    tracing the footprint by hand into a coordinate list is a legitimate
    and much faster answer for a hackathon — the geometry is what matters,
    not how it was obtained.
    """
    try:
        import ezdxf
    except ImportError:
        raise ImportError("Reading DXF needs ezdxf: pip install ezdxf")

    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    query = "LWPOLYLINE" if layer is None else f'LWPOLYLINE[layer=="{layer}"]'
    best, best_area = None, 0.0
    for entity in msp.query(query):
        pts = np.array([(p[0], p[1]) for p in entity.get_points()])
        if len(pts) < 3:
            continue
        area = 0.5 * abs(np.dot(pts[:, 0], np.roll(pts[:, 1], -1))
                          - np.dot(pts[:, 1], np.roll(pts[:, 0], -1)))
        if area > best_area:
            best, best_area = pts, area

    if best is None:
        location = f" on layer {layer!r}" if layer else ""
        raise ValueError(
            f"No closed LWPOLYLINE found in {path}{location}. "
            "Open the file in a DXF viewer and pass the correct layer name."
        )
    return best


# ============================================================================
# Self-tests — these actually run
# ============================================================================

def _self_test() -> None:
    import tempfile

    print("=" * 74)
    print("ingest_real.py self-tests")
    print("=" * 74)

    rng = np.random.default_rng(11)
    n = 5000
    # A realistic UTM 43N-ish cloud: large absolute coordinates, like a real file.
    source = pd.DataFrame({
        "x": rng.uniform(457000, 457015, n),
        "y": rng.uniform(2545000, 2545011, n),
        "z": rng.uniform(0, 22.4, n),
        "classification": rng.choice([CLASS_GROUND, CLASS_BUILDING], n),
    })

    with tempfile.TemporaryDirectory() as tmp:
        las_path = Path(tmp) / "test.las"

        print("\n--- 1. LAS write -> read round trip ---")
        write_las(source, las_path)
        print(f"  wrote {las_path.stat().st_size:,} bytes")

        header = _parse_las_header(las_path.read_bytes())
        print(f"  header: LAS {header['version']}, format {header['point_format_id']}, "
              f"{header['point_count']:,} points")
        assert header["point_count"] == n, "header point count wrong"

        back = read_las_pure(las_path)
        assert len(back) == n, f"expected {n} points, read {len(back)}"

        for axis in ("x", "y", "z"):
            err = float(np.abs(back[axis].to_numpy() - source[axis].to_numpy()).max())
            print(f"  max {axis} error: {err:.6f} m")
            assert err < 0.001, f"{axis} round trip lost precision: {err}"

        class_match = (back["classification"].to_numpy() == source["classification"].to_numpy()).all()
        print(f"  classification preserved: {class_match}")
        assert class_match, "classification did not survive the round trip"

        print("\n--- 2. Dispatcher ---")
        via_dispatch = load_point_cloud(las_path, max_points=100)
        print(f"  load_point_cloud() with max_points=100 -> {len(via_dispatch)} points")
        assert len(via_dispatch) == 100

        print("\n--- 3. PLY (ASCII) round trip ---")
        ply_path = Path(tmp) / "test.ply"
        sub = source.iloc[:500]
        with open(ply_path, "w") as fh:
            fh.write("ply\nformat ascii 1.0\n")
            fh.write(f"element vertex {len(sub)}\n")
            fh.write("property float x\nproperty float y\nproperty float z\n")
            fh.write("end_header\n")
            for _, r in sub.iterrows():
                fh.write(f"{r.x} {r.y} {r.z}\n")
        ply_df = read_ply(ply_path)
        print(f"  read {len(ply_df)} points from ASCII PLY")
        assert len(ply_df) == len(sub)
        assert float(np.abs(ply_df["x"].to_numpy() - sub["x"].to_numpy()).max()) < 0.01

    print("\n--- 4. Recentre to local ENU ---")
    local, origin = recenter_to_local_enu(source)
    print(f"  origin: ({origin['origin_x']:.1f}, {origin['origin_y']:.1f}, {origin['origin_z']:.2f})")
    print(f"  x range {local['x'].min():.2f} to {local['x'].max():.2f} m "
          f"(was {source['x'].min():.0f} to {source['x'].max():.0f})")
    assert abs(local["x"].mean()) < 0.01, "recentring did not zero the X centroid"
    assert local["z"].min() >= -1e-9, "Z should be anchored at zero"

    print("\n--- 5. Photogrammetry scale correction ---")
    unscaled = source.copy()
    unscaled[["x", "y", "z"]] *= 0.037          # arbitrary reconstruction scale
    scaled, factor = scale_point_cloud(
        unscaled, measured_distance_m=10.0,
        point_a=(0, 0, 0), point_b=(0.37, 0, 0),  # 10 m measured as 0.37 units
    )
    print(f"  recovered scale factor: {factor:.4f} (expected ~27.03)")
    assert abs(factor - 27.027) < 0.01, "scale recovery wrong"

    print("\n--- 6. Feeding a real-format cloud into pipeline.py ---")
    from pipeline import classify_ground_points, compute_dsm_dem, polygon_area

    derived = classify_ground_points(local, cell_size=1.0, ground_tolerance_m=0.3)
    print(f"  classify_ground_points: {int((derived == 2).sum()):,} ground / "
          f"{int((derived == 6).sum()):,} non-ground")
    dsm, dem, ndsm, grid = compute_dsm_dem(local, derived, cell_size=0.5)
    print(f"  DSM/DEM grid: {grid['nx']} x {grid['ny']} cells @ {grid['cell_size']} m")
    print(f"  nDSM max height: {np.nanmax(ndsm):.2f} m")
    assert grid["nx"] > 0 and grid["ny"] > 0

    print("\n" + "=" * 74)
    print("All ingest_real.py self-tests passed.")
    print("=" * 74)


if __name__ == "__main__":
    _self_test()
