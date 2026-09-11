"""
pipeline.py — Strata Cadastre (SIH26011), Phase 2
AI/ML fusion engine — spec MODULE A.

Covers all three sub-systems from the brief, in order:
  1. Building footprint extraction + epipolar relief-displacement correction
  2. LiDAR & CAD fusion engine (ground classification, DSM/DEM/nDSM,
     plinth/apex extraction, ICP alignment, multi-modal floor-count
     validation, sensor-to-cadastre precedence resolution per spec 1.3)
  3. Automated unauthorised-construction / deviation detector

SAMPLE DATA: every real input this module would normally take (drone
orthomosaic, LAS/LAZ point cloud, sanctioned DXF/DWG/IFC plan) is
generated synthetically by generate_synthetic_dataset() below, because
none of that is available during the hackathon. All assumptions about
its shape and units are documented in that function's docstring, and
restated inline wherever they matter.

LIBRARY SUBSTITUTIONS: the brief specifies PDAL, Shapely, Trimesh and
GeoPandas. None of the four are installed in the environment this file
was built and tested in (no network access to install them), so every
stage below is implemented directly on numpy/scipy/pandas instead —
tested by actually running it, not just written to look right. Each
function's docstring notes its production-library equivalent:
  - PDAL      -> reading/classifying/gridding a real LAS/LAZ/COPC file
                 (here: an in-memory point DataFrame, hand-rolled cell-
                 minimum ground filter, and numpy grid-binning for
                 DSM/DEM, in classify_ground_points / compute_dsm_dem).
  - Shapely   -> polygon predicates and boolean ops (here: a vectorised
                 ray-casting point-in-polygon test and voxel-grid set
                 operations, in points_in_polygon / voxelize_floors).
  - Trimesh   -> mesh/solid representation and CSG (here: the
                 ExtrudedFloor dataclass plus the same voxel-grid
                 approach, exact for the prismatic extruded-footprint
                 buildings this pipeline models).
  - GeoPandas -> tabular + geometry + CRS handling (here: plain pandas
                 DataFrames for attributes, numpy arrays for geometry,
                 no CRS transform needed since everything stays in one
                 local metre-based ENU frame — geodesy.py is the seam
                 where that frame gets tied to real-world WGS84/MSL).
Swap any of these in later without touching the rest of the pipeline —
each substituted function's signature and return shape is unchanged.

UNITS: metres and square/cubic metres throughout, in a local East-
North-Up (ENU) frame centred on the parcel (i.e. (0, 0) is the plot
centroid, not a real-world coordinate). geodesy.py is what ties a real
capture's Z into orthometric elevation; this module works one level
below that, on shape and volume.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from scipy.ndimage import distance_transform_edt


# ============================================================================
# 0. Shared geometry primitives
# ============================================================================

def polygon_area(vertices: np.ndarray) -> float:
    """Shoelace formula. vertices: (N,2), not required to be closed."""
    x, y = vertices[:, 0], vertices[:, 1]
    return float(0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def points_in_polygon(points_xy: np.ndarray, polygon_xy: np.ndarray) -> np.ndarray:
    """Vectorised even-odd ray-casting point-in-polygon test.
    points_xy: (M,2). polygon_xy: (N,2), not required to be closed.
    Returns a boolean array of shape (M,). Production equivalent:
    shapely.vectorized.contains / GeoSeries.contains."""
    x, y = points_xy[:, 0], points_xy[:, 1]
    n = len(polygon_xy)
    inside = np.zeros(len(points_xy), dtype=bool)
    j = n - 1
    for i in range(n):
        xi, yi = polygon_xy[i]
        xj, yj = polygon_xy[j]
        intersect = ((yi > y) != (yj > y)) & (
            x < (xj - xi) * (y - yi) / (yj - yi + 1e-15) + xi
        )
        inside ^= intersect
        j = i
    return inside


def _expand_rect(footprint_xy: np.ndarray, buffer_m: float) -> np.ndarray:
    """Exact outward offset for an axis-aligned rectangular footprint —
    every footprint generate_synthetic_dataset() produces is one. A
    general polygon needs a true Minkowski-sum buffer (shapely's
    Polygon.buffer()); this is the closed-form special case."""
    xmin, ymin = footprint_xy[:, 0].min(), footprint_xy[:, 1].min()
    xmax, ymax = footprint_xy[:, 0].max(), footprint_xy[:, 1].max()
    return np.array([
        [xmin - buffer_m, ymin - buffer_m],
        [xmax + buffer_m, ymin - buffer_m],
        [xmax + buffer_m, ymax + buffer_m],
        [xmin - buffer_m, ymax + buffer_m],
    ])


@dataclass
class ExtrudedFloor:
    """A single storey's footprint extruded between two elevations. The
    building-representation primitive this whole module uses in place of
    a Trimesh mesh — exact for prismatic, floor-by-floor construction,
    which is what both the sanctioned plan and the as-built survey are
    here (and what the vast majority of real cadastral buildings are)."""
    name: str
    footprint_xy: np.ndarray  # (N,2), metres, local ENU
    z_min: float              # metres, local ENU (see geodesy.py for the
    z_max: float               # geodetic seam — these are NOT orthometric heights)


# ============================================================================
# 1. SYNTHETIC DATASET GENERATOR
# ============================================================================

def generate_synthetic_dataset(rng_seed: int = 42) -> Dict:
    """
    Builds every input this pipeline needs, standing in for a real drone
    orthomosaic + LiDAR capture + sanctioned DXF/IFC floor plan.

    ASSUMPTIONS (units: metres / square metres / cubic metres unless
    stated otherwise; local ENU frame centred on the plot):
      - Building: "Shreeji Heights", base parcel IN-GJ-AMD-88421 (same
        parcel as the Task 1 visualizer and geodesy.py's worked example).
      - Sanctioned plan: rectangular footprint 15.0 x 11.0 m, floor
        height 3.2 m uniformly, G+5 (6 levels, index 0..5). The top
        sanctioned level (index 5) must observe a 1.5 m setback on all
        sides — a real, common municipal bylaw pattern for the topmost
        habitable/terrace floor.
      - As-built reality (what the LiDAR survey actually shows): floors
        0..4 match the sanctioned plan exactly. Floor 5 was built OUT TO
        the full footprint, ignoring the required setback (a footprint-
        level "setback/terrace encroachment"). One entire additional
        floor (index 6, footprint matching the *sanctioned* setback
        envelope) was added on top with no sanction at all (an
        "unauthorised additional floor").
      - Point cloud: synthetic LiDAR-like returns — classification 2
        (ASPRS "ground") for a scattered ring of points around the
        building out to 25 m, classification 6 ("building") for points
        sampled on every floor's wall faces and on whichever surfaces are
        actually exposed to a nadir-looking sensor (the true roof, plus
        any floor's footprint area not covered by the floor above it —
        e.g. floor 5's encroachment ring, which floor 6 doesn't fully
        cover). Vertical noise: +/-0.02-0.03 m, a rough stand-in for
        typical drone-LiDAR point precision.
      - Ground datum: Z = 0.0 at the plinth in this local frame (not a
        real elevation — geodesy.py is what would tie a REAL capture's Z
        to orthometric height via its own lat/lon-dependent geoid lookup).
      - Relief-displacement demo inputs (flying height, nadir offset) are
        a separate, independent synthetic example (see
        _demo_relief_displacement() in the __main__ block) — kept
        isolated from the tiered as-built geometry above so the
        correction formula's round-trip can be verified cleanly on its
        own, rather than complicated by a stepped building profile.

    Returns a dict with: sanctioned_floors, as_built_floors (both lists
    of ExtrudedFloor), point_cloud (a DataFrame with x, y, z,
    classification), and the scalar geometry constants used to build them
    (for the demo's ground-truth comparisons).
    """
    rng = np.random.default_rng(rng_seed)

    FOOTPRINT_W, FOOTPRINT_D = 15.0, 11.0
    FLOOR_HEIGHT = 3.2
    SANCTIONED_UPPER_FLOORS = 5   # G+5 sanctioned: indices 0..5
    SETBACK_M = 1.5

    half_w, half_d = FOOTPRINT_W / 2, FOOTPRINT_D / 2
    full_fp = np.array([
        [-half_w, -half_d], [half_w, -half_d], [half_w, half_d], [-half_w, half_d]
    ])
    setback_fp = np.array([
        [-half_w + SETBACK_M, -half_d + SETBACK_M],
        [half_w - SETBACK_M, -half_d + SETBACK_M],
        [half_w - SETBACK_M, half_d - SETBACK_M],
        [-half_w + SETBACK_M, half_d - SETBACK_M],
    ])

    # ---- Sanctioned plan (stands in for a parsed DXF/IFC file) ----
    sanctioned_floors: List[ExtrudedFloor] = []
    for f in range(SANCTIONED_UPPER_FLOORS + 1):
        z0 = f * FLOOR_HEIGHT
        fp = setback_fp if f == SANCTIONED_UPPER_FLOORS else full_fp
        sanctioned_floors.append(ExtrudedFloor(f"F{f:02d}", fp, z0, z0 + FLOOR_HEIGHT))

    # ---- As-built reality ----
    as_built_floors: List[ExtrudedFloor] = []
    for f in range(SANCTIONED_UPPER_FLOORS + 1):
        z0 = f * FLOOR_HEIGHT
        as_built_floors.append(ExtrudedFloor(f"F{f:02d}", full_fp, z0, z0 + FLOOR_HEIGHT))
    extra_idx = SANCTIONED_UPPER_FLOORS + 1
    z0 = extra_idx * FLOOR_HEIGHT
    as_built_floors.append(ExtrudedFloor(f"F{extra_idx:02d}", setback_fp, z0, z0 + FLOOR_HEIGHT))

    # ---- Synthetic point cloud over the AS-BUILT structure ----
    points, classes = [], []

    n_ground = 3000
    gx = rng.uniform(-25, 25, n_ground)
    gy = rng.uniform(-20, 20, n_ground)
    outside = ~points_in_polygon(np.column_stack([gx, gy]), full_fp)
    gx, gy = gx[outside], gy[outside]
    gz = rng.normal(0.0, 0.03, len(gx))
    points.append(np.column_stack([gx, gy, gz]))
    classes.append(np.full(len(gx), 2))

    for idx, floor in enumerate(as_built_floors):
        fp = floor.footprint_xy
        has_next = idx + 1 < len(as_built_floors)
        next_fp = as_built_floors[idx + 1].footprint_xy if has_next else None

        # exposed top surface: inside this floor's footprint, outside the
        # footprint of whatever sits directly above it (if anything)
        n_candidates = max(400, int(polygon_area(fp) * 60))
        cx = rng.uniform(fp[:, 0].min(), fp[:, 0].max(), n_candidates)
        cy = rng.uniform(fp[:, 1].min(), fp[:, 1].max(), n_candidates)
        inside_this = points_in_polygon(np.column_stack([cx, cy]), fp)
        if has_next:
            inside_next = points_in_polygon(np.column_stack([cx, cy]), next_fp)
            exposed = inside_this & ~inside_next
        else:
            exposed = inside_this
        rx, ry = cx[exposed], cy[exposed]
        if len(rx) > 0:
            rz = rng.normal(floor.z_max, 0.02, len(rx))
            points.append(np.column_stack([rx, ry, rz]))
            classes.append(np.full(len(rx), 6))

        # wall faces, every floor
        n_edges = len(fp)
        j = n_edges - 1
        for i in range(n_edges):
            p0, p1 = fp[j], fp[i]
            edge_len = float(np.linalg.norm(p1 - p0))
            n_wall_pts = max(20, int(edge_len * FLOOR_HEIGHT * 3))
            t = rng.uniform(0, 1, n_wall_pts)
            wx = p0[0] + t * (p1[0] - p0[0])
            wy = p0[1] + t * (p1[1] - p0[1])
            wz = rng.uniform(floor.z_min, floor.z_max, n_wall_pts)
            points.append(np.column_stack([wx, wy, wz]))
            classes.append(np.full(n_wall_pts, 6))
            j = i

    point_cloud = np.vstack(points)
    classification = np.concatenate(classes)
    pc_df = pd.DataFrame({
        "x": point_cloud[:, 0], "y": point_cloud[:, 1], "z": point_cloud[:, 2],
        "classification": classification.astype(int),
    })

    return {
        "sanctioned_floors": sanctioned_floors,
        "as_built_floors": as_built_floors,
        "point_cloud": pc_df,
        "special_volumes": generate_special_volumes(
            full_fp, setback_fp, SANCTIONED_UPPER_FLOORS, FLOOR_HEIGHT
        ),
        "constants": {
            "footprint_w": FOOTPRINT_W, "footprint_d": FOOTPRINT_D,
            "floor_height": FLOOR_HEIGHT,
            "sanctioned_upper_floors": SANCTIONED_UPPER_FLOORS,
            "setback_m": SETBACK_M,
            "full_footprint": full_fp, "setback_footprint": setback_fp,
        },
    }


def generate_special_volumes(full_fp: np.ndarray, setback_fp: np.ndarray,
                              upper_floors: int, floor_height: float) -> List[Dict]:
    """The non-storey vertical parcels the problem statement names
    explicitly: air-rights, subsurface utility networks, and elevated
    transport corridors.

    These are what distinguish a volumetric cadastre from a stack of
    floors. Each is a genuinely different kind of property right:

      T — Terrace / air-rights. The unbuilt volume above the sanctioned
          roof, up to the permitted height limit. Separately tradeable in
          many jurisdictions (TDR), and worthless to record in 2D because
          it has no distinct ground footprint of its own.
      U — Subsurface utility. A municipal water/sewer/fibre corridor
          running under the plot. Held as an easement by the utility, not
          by the landowner, and the single most common cause of
          excavation strikes when it is undocumented.
      E — Elevated transport corridor. A metro viaduct or flyover
          crossing the airspace above the parcel. The land beneath stays
          privately owned; the corridor volume does not.

    Each returns a dict rather than an ExtrudedFloor because these are not
    storeys — they carry their own vertical_type and do not participate in
    the floor-by-floor deviation comparison.
    """
    roof_z = (upper_floors + 1) * floor_height

    return [
        {
            "id": "T01",
            "vertical_type": "T",
            "label": "Air-rights volume above sanctioned roof",
            "footprint_xy": setback_fp,
            "z_min": roof_z,
            "z_max": roof_z + 9.0,      # to the permitted height limit
            "layer_type": "Terrace / Air-Rights",
            "usage_type": "MIXED",
            "owner": "Shreeji Heights Owners Association",
        },
        {
            "id": "U01",
            "vertical_type": "U",
            "label": "Municipal water & sewer corridor",
            "footprint_xy": np.array([
                [-full_fp[:, 0].max() - 3, -1.2], [full_fp[:, 0].max() + 3, -1.2],
                [full_fp[:, 0].max() + 3, 1.2], [-full_fp[:, 0].max() - 3, 1.2],
            ]),
            "z_min": -4.5,
            "z_max": -2.8,
            "layer_type": "Subsurface Utility Easement",
            "usage_type": "UTILITY",
            "owner": "Ahmedabad Municipal Corporation, Water Supply Dept.",
        },
        {
            "id": "E01",
            "vertical_type": "E",
            "label": "Metro viaduct corridor crossing plot airspace",
            "footprint_xy": np.array([
                [-full_fp[:, 0].max() - 8, 6.5], [full_fp[:, 0].max() + 8, 6.5],
                [full_fp[:, 0].max() + 8, 11.5], [-full_fp[:, 0].max() - 8, 11.5],
            ]),
            "z_min": 8.5,
            "z_max": 14.0,
            "layer_type": "Elevated Transport Corridor",
            "usage_type": "INDUSTRIAL",
            "owner": "Gujarat Metro Rail Corporation",
        },
    ]


# ============================================================================
# 2. FOOTPRINT EXTRACTION + EPIPOLAR RELIEF-DISPLACEMENT CORRECTION
# ============================================================================

def correct_relief_displacement(
    displaced_xy, nadir_xy, object_height_m: float, flying_height_m: float
) -> np.ndarray:
    """
    Maps a rooftop polygon's apparent (relief-displaced) ground-projected
    position back to the true ground-nadir footprint, using the standard
    central-projection relief-displacement model for a near-vertical
    aerial/drone photograph over locally flat terrain:

        apparent = nadir + [H / (H - h)] * (true - nadir)
        true     = nadir + [(H - h) / H] * (apparent - nadir)

    where H is flying height above the reference datum and h is the
    photographed point's height above that same datum. This scalar/
    radial correction is the standard first step before full DEM-based
    differential orthorectification, and is what "correcting for relief
    displacement" means for a single building of near-uniform height in
    a drone-orthomosaic footprint-extraction step.

    displaced_xy: (N,2) — the rooftop polygon as it appears in the
        (flat-datum ortho-rectified, but NOT height-corrected) orthomosaic.
    nadir_xy: (2,) — the ground-projected camera/flight-line position.
    object_height_m: h — building height above the SAME datum the
        orthomosaic itself is referenced to.
    flying_height_m: H — camera height above that datum; must exceed h.
    """
    displaced_xy = np.asarray(displaced_xy, dtype=float)
    nadir_xy = np.asarray(nadir_xy, dtype=float)
    if flying_height_m <= object_height_m:
        raise ValueError(
            f"flying_height_m ({flying_height_m}) must exceed object_height_m "
            f"({object_height_m}) — the camera has to be above the object."
        )
    scale = (flying_height_m - object_height_m) / flying_height_m
    return nadir_xy + scale * (displaced_xy - nadir_xy)


def _apply_relief_displacement(true_xy, nadir_xy, object_height_m: float, flying_height_m: float) -> np.ndarray:
    """Forward model (true ground footprint -> apparent displaced
    position). Only used to synthesise a self-test input for
    correct_relief_displacement() — a real pipeline never calls this,
    it only ever has the displaced (as-photographed) polygon to correct."""
    true_xy = np.asarray(true_xy, dtype=float)
    nadir_xy = np.asarray(nadir_xy, dtype=float)
    scale = flying_height_m / (flying_height_m - object_height_m)
    return nadir_xy + scale * (true_xy - nadir_xy)


# ============================================================================
# 3. LiDAR & CAD FUSION ENGINE
# ============================================================================

def classify_ground_points(pc_df: pd.DataFrame, cell_size: float = 1.0,
                            ground_tolerance_m: float = 0.3) -> np.ndarray:
    """
    Simplified cell-minimum ground/non-ground classifier — a lightweight
    stand-in for a full Cloth-Simulation or Progressive-Morphological
    ground filter (PDAL's filters.csf / filters.pmf in production). Bins
    points into cell_size x cell_size XY cells, takes each cell's minimum
    Z as a local ground estimate, and classifies any point within
    ground_tolerance_m of ITS cell's minimum as ground (ASPRS class 2);
    everything else as class 6 (building/other).
    Returns a fresh classification array; does not mutate pc_df.
    """
    x, y, z = pc_df["x"].values, pc_df["y"].values, pc_df["z"].values
    cell_x = np.floor(x / cell_size).astype(int)
    cell_y = np.floor(y / cell_size).astype(int)
    tmp = pd.DataFrame({"key": list(zip(cell_x, cell_y)), "z": z})
    cell_min = tmp.groupby("key")["z"].transform("min").values
    is_ground = (z - cell_min) <= ground_tolerance_m
    return np.where(is_ground, 2, 6)


def compute_dsm_dem(pc_df: pd.DataFrame, classification: np.ndarray,
                     cell_size: float = 0.5,
                     bounds: Optional[Tuple[float, float, float, float]] = None):
    """
    Grids the point cloud into cell_size x cell_size XY cells:
      DSM  = max Z per cell over ALL points (first-surface model)
      DEM  = max Z per cell over GROUND-classified points, nearest-filled
             into cells with no ground return
      nDSM = DSM - DEM (normalised / above-ground height model)
    Production equivalent: PDAL's writers.gdal over a CSF-classified
    cloud, or GDAL's gdal_grid — this is the same bin-and-resample idea.
    Returns (dsm, dem, ndsm, grid_info) where the three grids are
    (ny, nx) numpy arrays and grid_info carries the cell geometry needed
    to map grid indices back to real-world XY.
    """
    x, y, z = pc_df["x"].values, pc_df["y"].values, pc_df["z"].values
    if bounds is None:
        bounds = (float(x.min()), float(x.max()), float(y.min()), float(y.max()))
    xmin, xmax, ymin, ymax = bounds
    nx = max(1, int(np.ceil((xmax - xmin) / cell_size)))
    ny = max(1, int(np.ceil((ymax - ymin) / cell_size)))

    def grid_max(px, py, pz):
        if len(px) == 0:
            return np.full((ny, nx), np.nan)
        ix = np.clip(((px - xmin) / cell_size).astype(int), 0, nx - 1)
        iy = np.clip(((py - ymin) / cell_size).astype(int), 0, ny - 1)
        flat = iy * nx + ix
        flat_grid = np.full(nx * ny, -np.inf)
        np.maximum.at(flat_grid, flat, pz)
        flat_grid[flat_grid == -np.inf] = np.nan
        return flat_grid.reshape(ny, nx)

    dsm = grid_max(x, y, z)
    ground_mask = classification == 2
    dem = grid_max(x[ground_mask], y[ground_mask], z[ground_mask])

    if np.isnan(dem).any() and (~np.isnan(dem)).any():
        nan_mask = np.isnan(dem)
        _, idx = distance_transform_edt(nan_mask, return_distances=True, return_indices=True)
        dem = dem[tuple(idx)]

    ndsm = dsm - dem
    grid_info = {"xmin": xmin, "ymin": ymin, "cell_size": cell_size, "nx": nx, "ny": ny}
    return dsm, dem, ndsm, grid_info


def extract_plinth_and_apex(pc_df: pd.DataFrame, classification: np.ndarray,
                             footprint_xy: np.ndarray, buffer_m: float = 1.0) -> Dict:
    """
    Ground plinth elevation: median Z of ground-classified points in a
    buffer_m ring immediately outside the footprint (there are no ground
    returns from directly under a built structure).
    Building apex: max Z of any point within the footprint's XY extent.
    """
    x, y, z = pc_df["x"].values, pc_df["y"].values, pc_df["z"].values

    expanded = _expand_rect(footprint_xy, buffer_m)
    in_expanded = points_in_polygon(np.column_stack([x, y]), expanded)
    in_footprint = points_in_polygon(np.column_stack([x, y]), footprint_xy)
    ring = in_expanded & ~in_footprint & (classification == 2)
    if not ring.any():
        raise ValueError("no ground-classified points in the plinth ring; "
                          "widen buffer_m or check the ground classifier")
    plinth_elevation = float(np.median(z[ring]))

    if not in_footprint.any():
        raise ValueError("no points found within the footprint to establish apex height")
    apex_elevation = float(z[in_footprint].max())

    return {
        "plinth_elevation_m": round(plinth_elevation, 3),
        "apex_elevation_m": round(apex_elevation, 3),
        "apex_height_above_plinth_m": round(apex_elevation - plinth_elevation, 3),
    }


def icp_align(source_xyz: np.ndarray, target_xyz: np.ndarray,
              max_iterations: int = 50, tolerance: float = 1e-5):
    """
    Point-to-point Iterative Closest Point: aligns source_xyz onto
    target_xyz via a rigid transform (rotation + translation, no scale).
    Nearest-neighbour correspondences via a KD-tree; optimal rigid
    transform per iteration via the Kabsch/Umeyama SVD solution.

    Used to align the point cloud's derived building envelope against
    the sanctioned CAD/IFC floor plan (spec section 2.A.2). Production
    equivalent: PDAL's filters.icp, or Open3D's registration_icp — same
    algorithm, purpose-built implementation instead of hand-rolled.

    Returns (R, t, aligned_source, rmse_history): aligned_source =
    (R @ source_xyz.T).T + t, and rmse_history lets a caller confirm
    convergence rather than just trusting the loop ran.
    """
    source = np.asarray(source_xyz, dtype=float)
    target = np.asarray(target_xyz, dtype=float)
    tree = cKDTree(target)

    dim = source.shape[1]
    R = np.eye(dim)
    t = np.zeros(dim)
    current = source.copy()
    rmse_history: List[float] = []

    for iteration in range(max_iterations):
        _, indices = tree.query(current)
        matched_target = target[indices]

        src_centroid = current.mean(axis=0)
        tgt_centroid = matched_target.mean(axis=0)
        src_centered = current - src_centroid
        tgt_centered = matched_target - tgt_centroid

        H = src_centered.T @ tgt_centered
        U, S, Vt = np.linalg.svd(H)
        d = np.sign(np.linalg.det(Vt.T @ U.T))
        correction = np.eye(len(S))
        correction[-1, -1] = d
        R_step = Vt.T @ correction @ U.T
        t_step = tgt_centroid - R_step @ src_centroid

        current = (R_step @ current.T).T + t_step
        R = R_step @ R
        t = R_step @ t + t_step

        rmse = float(np.sqrt(np.mean(np.sum((current - matched_target) ** 2, axis=1))))
        rmse_history.append(rmse)
        if iteration > 0 and abs(rmse_history[-2] - rmse) < tolerance:
            break

    return R, t, current, rmse_history


def validate_floor_count(ndsm_apex_height_m: float, floor_height_estimate_m: float,
                          cad_declared_floor_count: int, cad_declared_floor_height_m: float,
                          tolerance_m: float = 1.0) -> Dict:
    """
    Spec section 1.2 is explicit: do NOT determine floor count purely
    from a CV/LiDAR height extraction, because floor heights vary
    (double-height lobbies, mezzanines). This treats the nDSM-implied
    floor count as one INPUT to reconcile against the CAD-declared count,
    never as the answer by itself — the naive division is still computed
    and returned, but labelled as such, not used as resolved_floor_count.
    """
    ndsm_implied_floors_naive = ndsm_apex_height_m / floor_height_estimate_m
    cad_implied_height = cad_declared_floor_count * cad_declared_floor_height_m
    height_discrepancy_m = ndsm_apex_height_m - cad_implied_height

    if abs(height_discrepancy_m) <= tolerance_m:
        resolved_floor_count, flag = cad_declared_floor_count, None
    elif height_discrepancy_m > tolerance_m:
        extra_floors = max(1, round(height_discrepancy_m / cad_declared_floor_height_m))
        resolved_floor_count = cad_declared_floor_count + extra_floors
        flag = "APEX_TALLER_THAN_SANCTIONED"
    else:
        resolved_floor_count, flag = cad_declared_floor_count, "APEX_SHORTER_THAN_SANCTIONED"

    return {
        "ndsm_implied_floor_count_naive": round(ndsm_implied_floors_naive, 2),
        "cad_declared_floor_count": cad_declared_floor_count,
        "height_discrepancy_m": round(height_discrepancy_m, 3),
        "resolved_floor_count": resolved_floor_count,
        "review_flag": flag,
    }


PRECEDENCE_ORDER = ["CORS_RTK_GNSS", "LIDAR_POINT_CLOUD", "SANCTIONED_CAD", "LEGACY_2D_GIS"]

DEFAULT_UNCERTAINTY_CM = {
    "CORS_RTK_GNSS": 10.0,
    "LIDAR_POINT_CLOUD": 30.0,
    "SANCTIONED_CAD": 100.0,
    "LEGACY_2D_GIS": 300.0,
}


def resolve_boundary_precedence(candidates: Dict[str, Dict]) -> Dict:
    """
    Spec section 1.3: CORS-RTK GNSS (10cm) > LiDAR Point Clouds >
    Sanctioned CAD Floor Plans > Legacy 2D GIS Parcel.

    candidates: a dict keyed by (a subset of) PRECEDENCE_ORDER, each
        value a dict with at least {"boundary": <array-like>} and
        optionally {"positional_uncertainty_cm": <float>} (falls back to
        DEFAULT_UNCERTAINTY_CM for that source if omitted). A source
        simply absent from the dict means it wasn't captured for this
        parcel, not that it failed.

    Returns the resolved boundary from the highest-precedence source
    present, plus source_confidence_score / positional_uncertainty_cm —
    field names matching parcels_base_2d / parcels_vertical_3d in
    schema_3d_cadastre.sql directly, so this plugs straight into it.
    """
    worst_case_cm = DEFAULT_UNCERTAINTY_CM["LEGACY_2D_GIS"]
    for source in PRECEDENCE_ORDER:
        entry = candidates.get(source)
        if entry is None:
            continue
        uncertainty_cm = entry.get("positional_uncertainty_cm", DEFAULT_UNCERTAINTY_CM[source])
        confidence = max(0.0, min(1.0, 1.0 - uncertainty_cm / worst_case_cm))
        superseded = [
            s for s in PRECEDENCE_ORDER
            if s in candidates and s != source
            and PRECEDENCE_ORDER.index(s) > PRECEDENCE_ORDER.index(source)
        ]
        return {
            "resolved_source": source,
            "boundary": entry["boundary"],
            "positional_uncertainty_cm": uncertainty_cm,
            "source_confidence_score": round(confidence, 3),
            "superseded_sources": superseded,
        }
    raise ValueError("no boundary candidates supplied from any known source")


# ============================================================================
# 4. UNAUTHORISED CONSTRUCTION / DEVIATION DETECTOR
# ============================================================================

def voxelize_floors(floors: List[ExtrudedFloor], cell_size: float = 0.25,
                     grid_bounds: Optional[Tuple[float, float, float, float, float, float]] = None):
    """
    Rasterises a list of ExtrudedFloor objects onto a shared 3D boolean
    occupancy grid — the numpy-native stand-in for a Trimesh voxelisation/
    boolean. Exact for prismatic (extruded-footprint) buildings, and the
    same underlying idea production BIM clash-detection tools actually
    fall back to for robustness with messy real-world meshes anyway.
    """
    if grid_bounds is None:
        all_xy = np.vstack([f.footprint_xy for f in floors])
        xmin, ymin = all_xy.min(axis=0) - cell_size
        xmax, ymax = all_xy.max(axis=0) + cell_size
        zmin, zmax = min(f.z_min for f in floors), max(f.z_max for f in floors)
        grid_bounds = (xmin, xmax, ymin, ymax, zmin, zmax)
    xmin, xmax, ymin, ymax, zmin, zmax = grid_bounds

    nx = max(1, int(np.ceil((xmax - xmin) / cell_size)))
    ny = max(1, int(np.ceil((ymax - ymin) / cell_size)))
    nz = max(1, int(np.ceil((zmax - zmin) / cell_size)))

    xs = xmin + (np.arange(nx) + 0.5) * cell_size
    ys = ymin + (np.arange(ny) + 0.5) * cell_size
    gx, gy = np.meshgrid(xs, ys, indexing="xy")
    cell_pts = np.column_stack([gx.ravel(), gy.ravel()])

    occ = np.zeros((nz, ny, nx), dtype=bool)
    for floor in floors:
        inside_xy = points_in_polygon(cell_pts, floor.footprint_xy).reshape(ny, nx)
        z_lo = max(0, int(np.floor((floor.z_min - zmin) / cell_size)))
        z_hi = min(nz, int(np.ceil((floor.z_max - zmin) / cell_size)))
        occ[z_lo:z_hi, :, :] |= inside_xy[np.newaxis, :, :]

    grid_info = {"xmin": xmin, "ymin": ymin, "zmin": zmin, "cell_size": cell_size,
                 "nx": nx, "ny": ny, "nz": nz}
    return occ, grid_info


def detect_deviations(as_built_floors: List[ExtrudedFloor],
                       sanctioned_floors: List[ExtrudedFloor],
                       cell_size: float = 0.25) -> Dict:
    """
    Delta-V = V_as-built \\ V_sanctioned (spec section 2.A.3), computed as
    a voxel-level set difference: occ_as_built AND NOT occ_sanctioned.
    Reports total unauthorised volume plus a per-as-built-floor
    breakdown, distinguishing "an entire unauthorised extra floor" from
    "an existing floor's footprint was built out past its sanctioned
    line" (a setback/terrace encroachment) by what fraction of that
    floor's own footprint area turned out unauthorised.
    """
    all_floors = as_built_floors + sanctioned_floors
    all_xy = np.vstack([f.footprint_xy for f in all_floors])
    xmin, ymin = all_xy.min(axis=0) - cell_size
    xmax, ymax = all_xy.max(axis=0) + cell_size
    zmin, zmax = min(f.z_min for f in all_floors), max(f.z_max for f in all_floors)
    shared_bounds = (xmin, xmax, ymin, ymax, zmin, zmax)

    occ_built, grid_info = voxelize_floors(as_built_floors, cell_size, shared_bounds)
    occ_sanctioned, _ = voxelize_floors(sanctioned_floors, cell_size, shared_bounds)

    unauthorised = occ_built & ~occ_sanctioned
    voxel_volume = cell_size ** 3
    total_unauthorised_cum = float(unauthorised.sum() * voxel_volume)

    nz = occ_built.shape[0]
    per_floor = []
    for floor in as_built_floors:
        z_lo = max(0, int(np.floor((floor.z_min - grid_info["zmin"]) / cell_size)))
        z_hi = min(nz, int(np.ceil((floor.z_max - grid_info["zmin"]) / cell_size)))
        floor_slice = unauthorised[z_lo:z_hi, :, :]
        floor_unauthorised_cum = float(floor_slice.sum() * voxel_volume)
        footprint_area = polygon_area(floor.footprint_xy)
        unauthorised_footprint_area = float(floor_slice.any(axis=0).sum() * cell_size ** 2)

        if floor_unauthorised_cum <= 1e-6:
            deviation_type = None
        elif unauthorised_footprint_area >= 0.9 * footprint_area:
            deviation_type = "UNAUTHORISED_ADDITIONAL_FLOOR"
        else:
            deviation_type = "SETBACK_OR_FOOTPRINT_ENCROACHMENT"

        per_floor.append({
            "floor": floor.name,
            "unauthorised_volume_cum": round(floor_unauthorised_cum, 3),
            "unauthorised_footprint_area_sqm": round(unauthorised_footprint_area, 2),
            "deviation_type": deviation_type,
        })

    return {
        "total_unauthorised_volume_cum": round(total_unauthorised_cum, 3),
        "voxel_cell_size_m": cell_size,
        "per_floor": per_floor,
    }


# ============================================================================
# 5. END-TO-END DEMO
# ============================================================================

def _demo_relief_displacement() -> None:
    print("\n--- 1. Footprint extraction: relief-displacement correction ---")
    # Independent synthetic example (see generate_synthetic_dataset's
    # docstring for why this is kept separate from the tiered building).
    true_footprint = np.array([[-7.5, -5.5], [7.5, -5.5], [7.5, 5.5], [-7.5, 5.5]])
    nadir = np.array([18.0, 6.0])       # drone flight line was offset from the building
    flying_height_m = 120.0
    object_height_m = 22.4              # true as-built apex above the same datum

    displaced = _apply_relief_displacement(true_footprint, nadir, object_height_m, flying_height_m)
    recovered = correct_relief_displacement(displaced, nadir, object_height_m, flying_height_m)

    max_error_m = float(np.max(np.abs(recovered - true_footprint)))
    print(f"  flying height H={flying_height_m} m, object height h={object_height_m} m, "
          f"nadir offset from building centroid={np.linalg.norm(nadir):.2f} m")
    print(f"  max corner displacement in the raw (uncorrected) image: "
          f"{np.max(np.linalg.norm(displaced - true_footprint, axis=1)):.3f} m")
    print(f"  max round-trip error after correction: {max_error_m:.6f} m "
          f"({'PASS' if max_error_m < 1e-6 else 'FAIL'})")


def _demo_fusion_engine(dataset: Dict) -> Dict:
    print("\n--- 2. LiDAR & CAD fusion engine ---")
    pc_df = dataset["point_cloud"]
    constants = dataset["constants"]
    print(f"  point cloud: {len(pc_df):,} points "
          f"({int((pc_df['classification'] == 2).sum()):,} ground / "
          f"{int((pc_df['classification'] == 6).sum()):,} building, as captured)")

    classification = classify_ground_points(pc_df, cell_size=1.0, ground_tolerance_m=0.3)
    reclassified_ground = int((classification == 2).sum())
    print(f"  re-derived ground classification: {reclassified_ground:,} points -> ground")

    dsm, dem, ndsm, grid_info = compute_dsm_dem(pc_df, classification, cell_size=0.5)
    print(f"  DSM/DEM grid: {grid_info['nx']} x {grid_info['ny']} cells "
          f"@ {grid_info['cell_size']} m")

    full_fp = constants["full_footprint"]
    plinth_apex = extract_plinth_and_apex(pc_df, classification, full_fp, buffer_m=1.0)
    true_apex = (constants["sanctioned_upper_floors"] + 2) * constants["floor_height"]  # +1 unauthorised floor
    print(f"  plinth elevation: {plinth_apex['plinth_elevation_m']} m "
          f"(true: 0.000 m)")
    print(f"  apex elevation:   {plinth_apex['apex_elevation_m']} m "
          f"(true: {true_apex:.3f} m)")

    floor_check = validate_floor_count(
        ndsm_apex_height_m=plinth_apex["apex_height_above_plinth_m"],
        floor_height_estimate_m=constants["floor_height"],
        cad_declared_floor_count=constants["sanctioned_upper_floors"] + 1,  # G+5 = 6 declared levels
        cad_declared_floor_height_m=constants["floor_height"],
    )
    print(f"  floor-count reconciliation: naive nDSM/height = "
          f"{floor_check['ndsm_implied_floor_count_naive']}, CAD declares "
          f"{floor_check['cad_declared_floor_count']}, discrepancy "
          f"{floor_check['height_discrepancy_m']} m -> resolved "
          f"{floor_check['resolved_floor_count']} levels, flag: {floor_check['review_flag']}")

    print("\n  ICP alignment self-test (recovering a known synthetic transform):")
    rng = np.random.default_rng(7)
    target_pts = rng.uniform(-5, 5, size=(200, 3))
    true_angle = np.radians(12.0)
    true_R = np.array([
        [np.cos(true_angle), -np.sin(true_angle), 0],
        [np.sin(true_angle), np.cos(true_angle), 0],
        [0, 0, 1],
    ])
    true_t = np.array([0.6, -0.3, 0.1])
    source_pts = (np.linalg.inv(true_R) @ (target_pts - true_t).T).T
    source_pts += rng.normal(0, 0.01, source_pts.shape)  # small sensor noise

    R_est, t_est, aligned, rmse_history = icp_align(source_pts, target_pts)
    rotation_error_deg = float(np.degrees(
        np.arccos(np.clip((np.trace(true_R.T @ R_est) - 1) / 2, -1.0, 1.0))
    ))
    translation_error_m = float(np.linalg.norm(true_t - t_est))
    print(f"    converged in {len(rmse_history)} iterations, final RMSE={rmse_history[-1]:.4f} m")
    print(f"    rotation error vs. known ground truth: {rotation_error_deg:.3f} deg")
    print(f"    translation error vs. known ground truth: {translation_error_m:.3f} m")

    print("\n  sensor-to-cadastre precedence resolution (spec 1.3):")
    candidates = {
        "LIDAR_POINT_CLOUD": {"boundary": full_fp, "positional_uncertainty_cm": 18.0},
        "SANCTIONED_CAD": {"boundary": constants["setback_footprint"]},
        "LEGACY_2D_GIS": {"boundary": full_fp * 1.02},
    }
    resolved = resolve_boundary_precedence(candidates)
    print(f"    candidates offered: {list(candidates.keys())}")
    print(f"    resolved source: {resolved['resolved_source']} "
          f"(confidence={resolved['source_confidence_score']}, "
          f"uncertainty={resolved['positional_uncertainty_cm']} cm)")
    print(f"    superseded: {resolved['superseded_sources']}")

    return {"classification": classification, "plinth_apex": plinth_apex, "floor_check": floor_check}


def _demo_deviation_detector(dataset: Dict) -> None:
    print("\n--- 3. Unauthorised construction / deviation detector ---")
    constants = dataset["constants"]
    ring_area = polygon_area(constants["full_footprint"]) - polygon_area(constants["setback_footprint"])
    expected_ring_cum = ring_area * constants["floor_height"]
    expected_extra_floor_cum = polygon_area(constants["setback_footprint"]) * constants["floor_height"]
    expected_total = expected_ring_cum + expected_extra_floor_cum
    print(f"  analytic ground truth: setback-encroachment ring = {expected_ring_cum:.2f} m3, "
          f"unauthorised extra floor = {expected_extra_floor_cum:.2f} m3, "
          f"total = {expected_total:.2f} m3")

    result = detect_deviations(dataset["as_built_floors"], dataset["sanctioned_floors"], cell_size=0.25)
    print(f"  voxel-computed total unauthorised volume: "
          f"{result['total_unauthorised_volume_cum']} m3 "
          f"(voxel cell = {result['voxel_cell_size_m']} m)")
    error_pct = 100 * abs(result["total_unauthorised_volume_cum"] - expected_total) / expected_total
    print(f"  error vs. analytic ground truth: {error_pct:.2f}% "
          f"({'within expected voxel-resolution error' if error_pct < 5 else 'CHECK THIS'})")
    print("  per-floor breakdown:")
    for row in result["per_floor"]:
        if row["deviation_type"] is None:
            continue
        print(f"    {row['floor']}: {row['unauthorised_volume_cum']} m3 unauthorised "
              f"({row['unauthorised_footprint_area_sqm']} sqm of footprint) "
              f"-> {row['deviation_type']}")


if __name__ == "__main__":
    print("=" * 78)
    print("Strata Cadastre — Phase 2 fusion engine, end-to-end demo on a")
    print("synthetic dataset (see generate_synthetic_dataset docstring for")
    print("every assumption about its shape and units).")
    print("=" * 78)

    dataset = generate_synthetic_dataset()
    print(f"\nSanctioned plan: {len(dataset['sanctioned_floors'])} levels "
          f"(G+{dataset['constants']['sanctioned_upper_floors']})")
    print(f"As-built survey: {len(dataset['as_built_floors'])} levels "
          f"(includes 1 unauthorised additional floor)")

    _demo_relief_displacement()
    _demo_fusion_engine(dataset)
    _demo_deviation_detector(dataset)

    print("\n" + "=" * 78)
    print("Phase 2 demo complete.")
    print("=" * 78)
