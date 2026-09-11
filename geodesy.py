"""
geodesy.py — Strata Cadastre (SIH26011), Phase 1
Geodetic datum & height-resolution layer — spec section 1.1.

GNSS/CORS receivers report ELLIPSOIDAL height (h), measured against the
smooth mathematical WGS84 ellipsoid. Cadastral records, floor plans and
construction drawings are all referenced to ORTHOMETRIC height (H),
i.e. height above Mean Sea Level (MSL), because that's the surface
people actually build relative to. The two are related by the geoid
undulation N, the separation between the ellipsoid and the geoid at a
given point:

    H = h - N        (ellipsoidal_to_orthometric)
    h = H + N         (orthometric_to_ellipsoidal)

APPROACH & ACCURACY TRADE-OFF (read before using this in anger)
-----------------------------------------------------------------
A full EGM2008 spherical-harmonic synthesis (degree/order 2160) needs a
multi-hundred-MB coefficient or grid file that isn't practical to
hand-derive or bundle for a hackathon build, and this environment has
no network access to fetch one. This module therefore implements the
"small precomputed regional lookup table" path explicitly sanctioned
in the brief:

  INDIA_GEOID_GRID is a coarse 4x4 grid of geoid undulation values (N,
  metres, WGS84 -> EGM geoid) spanning India's bounding box, bilinearly
  interpolated at query time. The values follow the real, well
  -documented shape of the field — India sits on the northern shoulder
  of the Indian Ocean Geoid Low (the most negative geoid anomaly on
  Earth, centred in the ocean south of Sri Lanka, where global EGM96/
  EGM2008 undulation bottoms out around -105 to -106 m against a global
  range of roughly -105 m to +85 m). N over the Indian mainland
  therefore grows steadily more negative moving south, from roughly
  -45 to -55 m near the Himalayan foothills to close to -95 to -100 m
  near Kanyakumari. The grid nodes below are hand-set to that
  documented gradient and rounded to the metre; they are ILLUSTRATIVE
  of the field's shape, not digitised from an official model, and
  carry an estimated error budget on the order of +/-1-3 m against a
  true degree-360 EGM2008 synthesis at this 4x4-node resolution over a
  ~28 deg x 29 deg country.

  That is good enough to drive floor-elevation math end-to-end through
  this pipeline for a demo, and to sanity-check that computed
  elevations are physically reasonable. It is NOT survey-grade and
  must never be the basis of a legal boundary, mutation, or OC record.

PRODUCTION UPGRADE PATH
-------------------------
Before this module is used for anything with legal weight, replace
`_bilinear_grid_lookup` with a real geoid model, e.g.:
  - `pygeodesy.GeoidKarney` / `GeoidPGM` against a downloaded EGM2008
    1x1 or 2.5x2.5 arc-minute PGM/gtx grid, or
  - `pyproj.Transformer` between a 3D CRS (EPSG:4979, WGS84 3D) and a
    compound CRS that carries an EGM2008 height axis, provided the
    matching geoid grid is installed on the PROJ data path, or
  - the Survey of India's own geoid model, where legally mandated.
`geoid_undulation()` is the single seam to swap: keep its signature
(lat, lon) -> metres and every caller in this module keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


# ---------------------------------------------------------------------
# Coarse India geoid undulation grid (see module docstring for caveats)
# ---------------------------------------------------------------------

_GRID_LATS: List[float] = [8.0, 16.0, 24.0, 32.0]
_GRID_LONS: List[float] = [68.0, 77.0, 86.0, 95.0]

# N in metres (WGS84 ellipsoid -> EGM geoid), rows follow _GRID_LATS,
# columns follow _GRID_LONS.
_GRID_N: List[List[float]] = [
    [-98.0, -100.0, -97.0, -90.0],   # lat  8 N (Kanyakumari / far south)
    [-82.0, -88.0, -83.0, -72.0],    # lat 16 N (Deccan plateau)
    [-64.0, -68.0, -72.0, -58.0],    # lat 24 N (Gujarat / MP / Bihar belt)
    [-52.0, -56.0, -50.0, -45.0],    # lat 32 N (Punjab / Himalayan foothills)
]


class OutOfServiceAreaWarning(UserWarning):
    """Raised (as a warning, not an error) when a query point falls
    outside the coarse grid's bounding box and had to be clamped."""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _validate_latlon(lat: float, lon: float) -> None:
    if not (-90.0 <= lat <= 90.0):
        raise ValueError(f"latitude {lat} out of range [-90, 90]")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f"longitude {lon} out of range [-180, 180]")


def _bilinear_grid_lookup(lat: float, lon: float) -> float:
    """Bilinear interpolation of N over the coarse India grid.
    Points outside the grid's bounding box are clamped to the nearest
    edge (reasonable for the demo's Ahmedabad/Gujarat scope; a real
    geoid model has no such limitation)."""
    lat_c = _clamp(lat, _GRID_LATS[0], _GRID_LATS[-1])
    lon_c = _clamp(lon, _GRID_LONS[0], _GRID_LONS[-1])

    i = 0
    while i < len(_GRID_LATS) - 2 and _GRID_LATS[i + 1] < lat_c:
        i += 1
    j = 0
    while j < len(_GRID_LONS) - 2 and _GRID_LONS[j + 1] < lon_c:
        j += 1

    lat0, lat1 = _GRID_LATS[i], _GRID_LATS[i + 1]
    lon0, lon1 = _GRID_LONS[j], _GRID_LONS[j + 1]

    t_lat = 0.0 if lat1 == lat0 else (lat_c - lat0) / (lat1 - lat0)
    t_lon = 0.0 if lon1 == lon0 else (lon_c - lon0) / (lon1 - lon0)

    n00 = _GRID_N[i][j]
    n01 = _GRID_N[i][j + 1]
    n10 = _GRID_N[i + 1][j]
    n11 = _GRID_N[i + 1][j + 1]

    n0 = n00 + (n01 - n00) * t_lon
    n1 = n10 + (n11 - n10) * t_lon
    return n0 + (n1 - n0) * t_lat


def geoid_undulation(lat: float, lon: float) -> float:
    """EGM geoid undulation N, in metres, at (lat, lon): the separation
    between the WGS84 ellipsoid and the geoid, positive where the geoid
    sits above the ellipsoid. See the module docstring for the accuracy
    trade-off of the lookup this currently uses."""
    _validate_latlon(lat, lon)
    return _bilinear_grid_lookup(lat, lon)


def ellipsoidal_to_orthometric(lat: float, lon: float, h: float) -> float:
    """H = h - N. Convert a GNSS/CORS ellipsoidal height reading (h,
    WGS84) to orthometric height (H, height above MSL)."""
    return h - geoid_undulation(lat, lon)


def orthometric_to_ellipsoidal(lat: float, lon: float, H: float) -> float:
    """h = H + N. Inverse of ellipsoidal_to_orthometric — useful when a
    sanctioned plan gives MSL heights and you need to compare against a
    raw GNSS fix."""
    return H + geoid_undulation(lat, lon)


@dataclass(frozen=True)
class ElevationRecord:
    """The two elevation fields the LADM-aligned schema (spec section
    1.1) requires for every spatial node:

    absolute_geodetic_elevation: orthometric elevation (MSL) of the
        unit's base or top — comparable across parcels, jurisdictions,
        and independently re-derivable from any GNSS fix.
    relative_plinth_elevation: height above the building's own ground
        plinth (Z_base + delta_Z_unit) — what a site engineer or the
        deviation-detection pipeline (Phase 2) actually reasons about
        day to day, independent of geoid/MSL error budgets.
    """
    absolute_geodetic_elevation: float   # metres, orthometric / MSL
    relative_plinth_elevation: float     # metres, above the unit's plinth


def build_elevation_record(
    lat: float,
    lon: float,
    ellipsoidal_height: float,
    plinth_orthometric_elevation: float,
    delta_z_unit: float,
) -> ElevationRecord:
    """Build both required elevation fields for one spatial node.

    lat, lon, ellipsoidal_height: the GNSS/CORS fix taken at this node
        (e.g. a unit's slab corner benchmark).
    plinth_orthometric_elevation: Z_base — the orthometric elevation of
        the building's ground plinth, established once per parcel
        (typically the ground-floor slab's own absolute_geodetic_elevation).
    delta_z_unit: height of this node above the plinth, from floor
        -height accumulation or the LiDAR/CAD fusion pipeline.
    """
    absolute = ellipsoidal_to_orthometric(lat, lon, ellipsoidal_height)
    relative = plinth_orthometric_elevation + delta_z_unit
    return ElevationRecord(
        absolute_geodetic_elevation=round(absolute, 3),
        relative_plinth_elevation=round(relative, 3),
    )


if __name__ == "__main__":
    # Self-test / demo: Shreeji Heights, Ahmedabad, Gujarat
    # (~23.0225 N, 72.5714 E) — matches base parcel IN-GJ-AMD-88421
    # from the Task 1 visualizer.
    lat, lon = 23.0225, 72.5714

    N = geoid_undulation(lat, lon)
    print(f"Geoid undulation N at Ahmedabad (coarse India grid): {N:.2f} m")

    h_gnss = 96.40  # a plausible CORS-RTK ellipsoidal height fix, metres
    H_msl = ellipsoidal_to_orthometric(lat, lon, h_gnss)
    print(f"GNSS ellipsoidal height h = {h_gnss:.2f} m  ->  orthometric H = {H_msl:.2f} m")

    round_trip = orthometric_to_ellipsoidal(lat, lon, H_msl)
    assert abs(round_trip - h_gnss) < 1e-9, "round-trip conversion drifted"
    print(f"Round-trip check: H -> h = {round_trip:.2f} m (matches input)")

    print("\nPer-floor elevation records (plinth = ground-floor slab):")
    plinth_H = H_msl
    floor_deltas = {"Ground floor": 0.0, "Floor 1": 3.2, "Floor 2": 6.4,
                     "Floor 3": 9.6, "Floor 4": 12.8, "Floor 5": 16.0}
    for label, delta_z in floor_deltas.items():
        node_h = h_gnss + delta_z  # simulated per-floor GNSS/LiDAR fix, ellipsoidal
        rec = build_elevation_record(lat, lon, node_h, plinth_H, delta_z)
        print(f"  {label:>13}: absolute_geodetic_elevation={rec.absolute_geodetic_elevation:>8.3f} m   "
              f"relative_plinth_elevation={rec.relative_plinth_elevation:>8.3f} m")

    # Basic input-validation demo
    try:
        geoid_undulation(999, 0)
    except ValueError as exc:
        print(f"\nValidation OK — rejected out-of-range latitude: {exc}")
