"""
floorplan_extract.py — Strata Cadastre (SIH26011)
Automated boundary + floor segmentation from 2D floor plan images.

Closes two problem-statement requirements:
  - "Building floor Plans" as an integrated input source
  - "AI/ML capabilities → Floor segmentation"

WHAT IT DOES
------------
Takes a raster floor plan (scanned sanctioned plan, CAD export, or photo)
and produces:
  1. The outer wall boundary as a simplified polygon, in METRES
  2. Individual rooms/units as separate polygons (floor segmentation),
     each with its own carpet area
  3. A JSON payload ready for the 3D-ULPIN generator in main.py

METHOD (classical CV, and that is a deliberate choice)
-------------------------------------------------------
Boundary: grayscale -> Otsu binarise -> morphological cleanup ->
findContours (RETR_EXTERNAL) -> approxPolyDP simplification.

Rooms: distance transform of the interior space -> threshold to seeds ->
watershed. This is the step that handles DOORWAYS. A naive contour-
hierarchy approach treats rooms as holes inside the outer wall, which
only works if every room is fully enclosed; real plans have door gaps, so
the interior is one connected region and hierarchy returns a single room.
The distance transform peaks at room centres and dips at doorways, so
watershed places each boundary right at the doorway — where a surveyor
would put it.

Where ML genuinely earns its place in this system is point-cloud
classification (see ml_classifier.py, which is a trained model with
measured accuracy). Using a neural network here instead would be worse
engineering and slower — floor plans are high-contrast line drawings,
which is the case classical CV handles best.

SCALE CALIBRATION — DO NOT SKIP
--------------------------------
A floor plan image has no inherent units. Every output is in pixels until
you supply a scale. Provide it one of two ways:
  - metres_per_pixel, if you know the drawing scale and DPI
  - a known real-world dimension via calibrate_from_known_length()
Uncalibrated output is geometrically correct and dimensionally
meaningless, so extract_floorplan() requires the scale explicitly rather
than defaulting to 1.0 and silently producing wrong areas.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


# ============================================================================
# Geometry helpers
# ============================================================================

def polygon_area_px(polygon: np.ndarray) -> float:
    """Shoelace area in whatever units the polygon is expressed in."""
    x, y = polygon[:, 0], polygon[:, 1]
    return float(0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def pixels_to_metres(polygon_px: np.ndarray, metres_per_pixel: float,
                      origin_px: Optional[Tuple[float, float]] = None,
                      flip_y: bool = True) -> np.ndarray:
    """Convert a pixel-space polygon into local ENU metres.

    Image rows increase downward while ENU Y increases northward, so the Y
    axis is flipped by default. Leaving it unflipped mirrors every
    footprint — which still produces plausible-looking areas, making it a
    genuinely easy error to ship unnoticed.
    """
    poly = np.asarray(polygon_px, dtype=np.float64).copy()
    if origin_px is None:
        origin_px = (poly[:, 0].mean(), poly[:, 1].mean())
    poly[:, 0] = (poly[:, 0] - origin_px[0]) * metres_per_pixel
    poly[:, 1] = (poly[:, 1] - origin_px[1]) * metres_per_pixel
    if flip_y:
        poly[:, 1] = -poly[:, 1]
    return poly


def calibrate_from_known_length(pixel_length: float, real_length_m: float) -> float:
    """metres_per_pixel from one measured dimension.

    Measure any wall you know the true length of (from the plan's own
    dimension annotations, or with a tape on site), find its length in
    pixels, and pass both.
    """
    if pixel_length <= 0:
        raise ValueError("pixel_length must be positive")
    if real_length_m <= 0:
        raise ValueError("real_length_m must be positive")
    return real_length_m / pixel_length


# ============================================================================
# Core extraction
# ============================================================================

def preprocess(image: np.ndarray, invert: bool = False,
                close_kernel: int = 3) -> np.ndarray:
    """Grayscale -> binary -> morphological closing.

    Closing bridges the small gaps that scanning artefacts and door
    openings leave in wall lines. Without it, findContours leaks through
    a one-pixel doorway gap and merges every room into one blob.
    """
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image.copy()

    # Otsu picks the threshold from the histogram, so this survives
    # varying scan brightness without a hand-tuned constant.
    threshold_type = cv2.THRESH_BINARY if invert else cv2.THRESH_BINARY_INV
    _, binary = cv2.threshold(gray, 0, 255, threshold_type + cv2.THRESH_OTSU)

    if close_kernel > 0:
        kernel = np.ones((close_kernel, close_kernel), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    return binary


def extract_outer_boundary(binary: np.ndarray,
                            epsilon_ratio: float = 0.01) -> Tuple[np.ndarray, float]:
    """Largest external contour, simplified with Douglas-Peucker.

    epsilon_ratio is the simplification tolerance as a fraction of
    perimeter. 0.01 turns a noisy scanned rectangle into 4 clean corners;
    raise it for very rough scans, lower it for genuinely complex
    footprints (L-shaped or stepped buildings) that must keep their form.
    """
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError(
            "No contours found. The plan may be inverted (walls light on a dark "
            "background) — retry with invert=True."
        )

    largest = max(contours, key=cv2.contourArea)
    perimeter = cv2.arcLength(largest, True)
    approx = cv2.approxPolyDP(largest, epsilon_ratio * perimeter, True)
    polygon = approx.reshape(-1, 2).astype(np.float64)

    if len(polygon) < 3:
        raise ValueError(
            f"Boundary simplified to {len(polygon)} vertices — not a polygon. "
            "Lower epsilon_ratio."
        )
    return polygon, float(cv2.contourArea(largest))


def extract_rooms(binary: np.ndarray, min_area_px: float = 500.0,
                   epsilon_ratio: float = 0.02,
                   seed_threshold: float = 0.45) -> List[np.ndarray]:
    """Floor segmentation via distance transform + watershed.

    WHY NOT CONTOUR HIERARCHY: the obvious approach is RETR_CCOMP and
    treating child contours as rooms. That only works when every room is
    fully enclosed. Real floor plans have DOORWAYS — gaps in internal
    walls — so the interior is one connected region and hierarchy returns
    a single room. Morphological closing cannot rescue it either: a
    900 mm door at typical plan resolution is a ~90 px gap, and a kernel
    large enough to bridge it destroys the wall geometry you are trying
    to measure.

    Watershed handles this correctly. The distance transform peaks at room
    centres and dips at doorways, so thresholding it yields one seed per
    room, and watershed grows those seeds until they meet — placing the
    boundary at the doorway, which is exactly where a surveyor would put it.

    seed_threshold is the fraction of the maximum distance used to isolate
    seeds. Raise it if adjacent rooms merge; lower it if a large room
    fragments into several.
    """
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []

    # Interior space = everything inside the outer wall, minus the walls.
    largest = max(contours, key=cv2.contourArea)
    filled = np.zeros_like(binary)
    cv2.drawContours(filled, [largest], -1, 255, cv2.FILLED)
    interior = cv2.bitwise_and(filled, cv2.bitwise_not(binary))

    distance = cv2.distanceTransform(interior, cv2.DIST_L2, 5)
    if distance.max() <= 0:
        return []

    _, seeds = cv2.threshold(distance, seed_threshold * distance.max(), 255, cv2.THRESH_BINARY)
    seeds = seeds.astype(np.uint8)

    seed_count, markers = cv2.connectedComponents(seeds)
    markers = markers + 1                      # watershed reserves 1 for background
    markers[cv2.subtract(interior, seeds) == 255] = 0   # unknown region to be assigned

    markers = cv2.watershed(cv2.cvtColor(interior, cv2.COLOR_GRAY2BGR), markers)

    rooms = []
    for label in range(2, seed_count + 1):
        mask = np.where(markers == label, 255, 0).astype(np.uint8)
        room_contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not room_contours:
            continue
        contour = max(room_contours, key=cv2.contourArea)
        if cv2.contourArea(contour) < min_area_px:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, epsilon_ratio * perimeter, True)
        polygon = approx.reshape(-1, 2).astype(np.float64)
        if len(polygon) >= 3:
            rooms.append(polygon)

    rooms.sort(key=polygon_area_px, reverse=True)
    return rooms


def extract_floorplan(image_path: str | Path,
                       metres_per_pixel: float,
                       floor_index: int = 0,
                       floor_height_m: float = 3.2,
                       base_ulpin: str = "11000088421000",
                       lgd_code: str = "240124",
                       invert: bool = False,
                       min_room_area_sqm: float = 4.0) -> Dict:
    """Full extraction: image file -> boundary + rooms + 3D-ULPIN payload.

    metres_per_pixel is REQUIRED — see the scale-calibration note in the
    module docstring. There is no default, deliberately.
    """
    image_path = Path(image_path)
    image = cv2.imread(str(image_path))
    if image is None:
        raise FileNotFoundError(f"Could not read image: {image_path}")

    if metres_per_pixel <= 0:
        raise ValueError("metres_per_pixel must be positive")

    binary = preprocess(image, invert=invert)
    boundary_px, boundary_area_px = extract_outer_boundary(binary)

    # Anchor every polygon to the same origin — the boundary centroid — so
    # rooms stay correctly positioned relative to the building.
    origin_px = (boundary_px[:, 0].mean(), boundary_px[:, 1].mean())

    boundary_m = pixels_to_metres(boundary_px, metres_per_pixel, origin_px)
    boundary_area_sqm = polygon_area_px(boundary_m)

    min_area_px = min_room_area_sqm / (metres_per_pixel ** 2)
    rooms_px = extract_rooms(binary, min_area_px=min_area_px)

    z_min = floor_index * floor_height_m
    z_max = z_min + floor_height_m

    units = []
    for i, room_px in enumerate(rooms_px, start=1):
        room_m = pixels_to_metres(room_px, metres_per_pixel, origin_px)
        area_sqm = polygon_area_px(room_m)
        unit_code = f"{floor_index}{i:03d}"[-4:].zfill(4)
        units.append({
            "unit_number": f"{floor_index}{i:02d}",
            "3d_ulpin": f"{lgd_code}{base_ulpin}F{floor_index:03d}{unit_code}",
            "carpet_area_sqm": round(area_sqm, 2),
            "footprint_m": [[round(px, 3), round(py, 3)] for px, py in room_m],
            "z_min": round(z_min, 3),
            "z_max": round(z_max, 3),
            "layer_type": "Above-Ground Residential",
        })

    return {
        "source_image": image_path.name,
        "image_size_px": [int(image.shape[1]), int(image.shape[0])],
        "metres_per_pixel": metres_per_pixel,
        "base_parcel_ulpin": base_ulpin,
        "floor_index": floor_index,
        "floor_height_m": floor_height_m,
        "boundary_area_sqm": round(boundary_area_sqm, 2),
        "boundary_m": [[round(px, 3), round(py, 3)] for px, py in boundary_m],
        "unit_count": len(units),
        "total_unit_area_sqm": round(sum(u["carpet_area_sqm"] for u in units), 2),
        "units": units,
    }


def to_extruded_floors(extraction: Dict):
    """Convert an extraction into pipeline.ExtrudedFloor objects, so a
    floor plan can drive the deviation detector and topology validation
    directly."""
    from pipeline import ExtrudedFloor

    floors = []
    for unit in extraction["units"]:
        floors.append(ExtrudedFloor(
            name=unit["unit_number"],
            footprint_xy=np.array(unit["footprint_m"]),
            z_min=unit["z_min"],
            z_max=unit["z_max"],
        ))
    return floors


# ============================================================================
# Synthetic test plan generator
# ============================================================================

def generate_test_floorplan(path: str | Path, building_w_m: float = 15.0,
                             building_d_m: float = 11.0, px_per_m: float = 60.0,
                             margin_px: int = 30, wall_px: int = 8) -> Dict:
    """Draw a floor plan to test against, since no real sanctioned plan was
    available in this build environment.

    Layout: a building of the given dimensions, divided into four rooms by
    internal walls with door gaps left in them — door gaps matter, because
    they are exactly what breaks naive contour extraction.

    The canvas is sized FROM the building dimensions plus margins. Getting
    this backwards (fixing the canvas and letting the drawing overflow)
    silently clips the plan at the image edge, and the extractor then reads
    a fragment rather than the building.

    Returns the ground-truth dimensions so the extractor's output can be
    checked against known values rather than eyeballed.
    """
    path = Path(path)

    building_w_px = int(round(building_w_m * px_per_m))
    building_d_px = int(round(building_d_m * px_per_m))
    width_px = building_w_px + 2 * margin_px
    height_px = building_d_px + 2 * margin_px

    image = np.full((height_px, width_px), 255, dtype=np.uint8)

    x0, y0 = margin_px, margin_px
    x1, y1 = margin_px + building_w_px, margin_px + building_d_px
    cv2.rectangle(image, (x0, y0), (x1, y1), 0, wall_px)

    mid_x = (x0 + x1) // 2
    mid_y = (y0 + y1) // 2
    door_gap = 45

    # Vertical divider with a door gap
    cv2.line(image, (mid_x, y0), (mid_x, mid_y - door_gap), 0, wall_px)
    cv2.line(image, (mid_x, mid_y + door_gap), (mid_x, y1), 0, wall_px)
    # Horizontal divider with a door gap
    cv2.line(image, (x0, mid_y), (mid_x - door_gap, mid_y), 0, wall_px)
    cv2.line(image, (mid_x + door_gap, mid_y), (x1, mid_y), 0, wall_px)

    cv2.imwrite(str(path), image)

    return {
        "path": str(path),
        "px_per_m": px_per_m,
        "metres_per_pixel": 1.0 / px_per_m,
        "true_width_m": building_w_m,
        "true_depth_m": building_d_m,
        "true_area_sqm": building_w_m * building_d_m,
        "building_w_px": building_w_px,
        "image_size_px": [width_px, height_px],
        "expected_rooms": 4,
    }


if __name__ == "__main__":
    import tempfile

    print("=" * 76)
    print("floorplan_extract.py — boundary + floor segmentation from a plan image")
    print("=" * 76)

    with tempfile.TemporaryDirectory() as tmp:
        plan_path = Path(tmp) / "test_plan.png"
        truth = generate_test_floorplan(plan_path)

        print(f"\nGenerated test plan: {truth['true_width_m']} m x {truth['true_depth_m']} m "
              f"at {truth['px_per_m']} px/m ({truth['true_area_sqm']} m2, "
              f"{truth['expected_rooms']} rooms with door gaps)")

        print("\n--- 1. Extraction with correct calibration ---")
        result = extract_floorplan(
            plan_path,
            metres_per_pixel=truth["metres_per_pixel"],
            floor_index=4,
        )
        print(f"  image: {result['image_size_px'][0]} x {result['image_size_px'][1]} px")
        print(f"  boundary vertices: {len(result['boundary_m'])}")
        print(f"  boundary area: {result['boundary_area_sqm']} m2 "
              f"(true {truth['true_area_sqm']} m2)")

        area_error = abs(result["boundary_area_sqm"] - truth["true_area_sqm"]) / truth["true_area_sqm"]
        print(f"  area error: {area_error * 100:.2f}%")
        assert area_error < 0.05, f"boundary area off by {area_error * 100:.1f}%"
        assert len(result["boundary_m"]) == 4, \
            f"expected a 4-vertex rectangle, got {len(result['boundary_m'])}"

        print(f"\n--- 2. Floor segmentation ---")
        print(f"  rooms detected: {result['unit_count']} (expected {truth['expected_rooms']})")
        assert result["unit_count"] == truth["expected_rooms"], \
            f"expected {truth['expected_rooms']} rooms, found {result['unit_count']}"

        for unit in result["units"]:
            print(f"    unit {unit['unit_number']}: {unit['carpet_area_sqm']:>6.2f} m2  "
                  f"ULPIN {unit['3d_ulpin']}  z {unit['z_min']}-{unit['z_max']} m")

        print(f"  total carpet area: {result['total_unit_area_sqm']} m2 "
              f"(less than {truth['true_area_sqm']} m2 gross — walls are excluded, as they should be)")
        assert result["total_unit_area_sqm"] < truth["true_area_sqm"], \
            "carpet area cannot exceed gross built-up area"

        print("\n--- 3. Every generated 3D-ULPIN is valid ---")
        import re
        pattern = re.compile(r"^[0-9A-Z]{6}[0-9A-Z]{14}[BGFTUE][0-9]{3}[0-9A-Z]{4}$")
        for unit in result["units"]:
            ulpin = unit["3d_ulpin"]
            assert len(ulpin) == 28, f"{ulpin} is {len(ulpin)} chars, expected 28"
            assert pattern.match(ulpin), f"{ulpin} fails the ULPIN format check"
        print(f"  all {result['unit_count']} ULPINs are 28 chars and format-valid")

        print("\n--- 4. Handoff to pipeline.py ---")
        floors = to_extruded_floors(result)
        from pipeline import polygon_area
        print(f"  built {len(floors)} ExtrudedFloor objects")
        for floor in floors[:2]:
            print(f"    {floor.name}: {polygon_area(floor.footprint_xy):.2f} m2, "
                  f"z {floor.z_min}-{floor.z_max} m")
        assert len(floors) == result["unit_count"]

        print("\n--- 5. Scale calibration from a known dimension ---")
        derived = calibrate_from_known_length(
            pixel_length=truth["building_w_px"], real_length_m=truth["true_width_m"]
        )
        print(f"  {truth['building_w_px']} px = {truth['true_width_m']} m  ->  "
              f"{derived:.6f} m/px (true {truth['metres_per_pixel']:.6f})")
        assert abs(derived - truth["metres_per_pixel"]) < 1e-9

        print("\n--- 6. Uncalibrated input is rejected, not silently wrong ---")
        try:
            extract_floorplan(plan_path, metres_per_pixel=0)
            raise AssertionError("expected a ValueError for a zero scale")
        except ValueError as exc:
            print(f"  correctly rejected: {exc}")

        out_json = Path(tmp) / "extraction.json"
        out_json.write_text(json.dumps(result, indent=2))
        print(f"\n  JSON payload written ({out_json.stat().st_size:,} bytes)")

    print("\n" + "=" * 76)
    print("All floorplan_extract.py tests passed.")
    print("=" * 76)
