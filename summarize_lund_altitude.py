#!/usr/bin/env python3

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.mask import mask

from check_lund_access import DEFAULT_KOMMUN, DEFAULT_TATORT, load_tatort


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute min and max altitude for a tatort from downloaded DEM tiles."
    )
    parser.add_argument("--tatort", default=DEFAULT_TATORT, help="Tatort name to inspect.")
    parser.add_argument("--kommun", default=DEFAULT_KOMMUN, help="Kommun name to disambiguate the tatort.")
    parser.add_argument(
        "--cache-dir",
        required=True,
        help="Directory containing downloaded DEM GeoTIFF tiles.",
    )
    return parser.parse_args()


def valid_values(band, nodata) -> np.ndarray:
    if np.ma.isMaskedArray(band):
        if band.count() == 0:
            return np.array([], dtype="float64")
        return band.compressed().astype("float64")

    values = np.asarray(band, dtype="float64").ravel()
    mask_valid = ~np.isnan(values)
    if nodata is not None and not math.isnan(nodata):
        mask_valid &= ~np.isclose(values, nodata)
    return values[mask_valid]


def slope_values(band, nodata, x_res: float, y_res: float) -> np.ndarray:
    values = np.ma.filled(band, np.nan).astype("float64", copy=False)
    gradient_y, gradient_x = np.gradient(values, y_res, x_res)
    slope = np.degrees(np.arctan(np.sqrt(np.square(gradient_x) + np.square(gradient_y))))
    mask_valid = np.isfinite(slope).ravel()

    source_values = values.ravel()
    if nodata is not None and not math.isnan(nodata):
        source_mask = ~np.isclose(source_values, nodata)
    else:
        source_mask = np.isfinite(source_values)

    return slope.ravel()[mask_valid & source_mask]


def main() -> int:
    args = parse_args()
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_dir():
        raise ValueError(f"Cache directory does not exist: {cache_dir}")

    tif_paths = sorted(cache_dir.glob("*.tif"))
    if not tif_paths:
        raise ValueError(f"No .tif files found in cache directory: {cache_dir}")

    tatort = load_tatort(args.tatort, args.kommun)
    geometry = [tatort.geometry.iloc[0].__geo_interface__]

    elevation_chunks: list[np.ndarray] = []
    global_min = math.inf
    global_max = -math.inf
    slope_sum_squares = 0.0
    slope_count = 0
    used_tiles = 0

    for tif_path in tif_paths:
        with rasterio.open(tif_path) as dataset:
            try:
                clipped, _ = mask(dataset, geometry, crop=True, filled=False)
            except ValueError:
                continue

            values = valid_values(clipped[0], dataset.nodata)
            if values.size == 0:
                continue

            elevation_chunks.append(values.astype("float32", copy=False))
            global_min = min(global_min, float(values.min()))
            global_max = max(global_max, float(values.max()))

            slope = slope_values(
                clipped[0],
                dataset.nodata,
                abs(dataset.transform.a),
                abs(dataset.transform.e),
            )
            if slope.size:
                slope_sum_squares += float(np.square(slope, dtype="float64").sum())
                slope_count += int(slope.size)

            used_tiles += 1

    if not math.isfinite(global_min) or not math.isfinite(global_max) or not elevation_chunks:
        raise RuntimeError("No valid DEM pixels intersected the tatort in the provided cache directory.")

    elevations = np.concatenate(elevation_chunks)
    relief_q05 = float(np.percentile(elevations, 5))
    relief_q95 = float(np.percentile(elevations, 95))
    area_km2 = float(tatort.geometry.iloc[0].area) / 1_000_000
    normalized_relief = (relief_q95 - relief_q05) / math.sqrt(area_km2)
    rms_slope = math.sqrt(slope_sum_squares / slope_count) if slope_count else 0.0
    hilliness_score = normalized_relief * rms_slope

    print(f"Tatort: {args.tatort} ({args.kommun})")
    print(f"Tatortskod: {tatort.iloc[0]['tatortskod']}")
    print(f"Tiles scanned: {len(tif_paths)}")
    print(f"Tiles used: {used_tiles}")
    print(f"Min altitude: {global_min:.2f} m")
    print(f"Max altitude: {global_max:.2f} m")
    print(f"Altitude range: {global_max - global_min:.2f} m")
    print(f"Relief Q05: {relief_q05:.2f} m")
    print(f"Relief Q95: {relief_q95:.2f} m")
    print(f"Normalized relief: {normalized_relief:.2f}")
    print(f"RMS slope: {rms_slope:.2f} deg")
    print(f"Hilliness score: {hilliness_score:.2f}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
