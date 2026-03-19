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

    global_min = math.inf
    global_max = -math.inf
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

            global_min = min(global_min, float(values.min()))
            global_max = max(global_max, float(values.max()))
            used_tiles += 1

    if not math.isfinite(global_min) or not math.isfinite(global_max):
        raise RuntimeError("No valid DEM pixels intersected the tatort in the provided cache directory.")

    print(f"Tatort: {args.tatort} ({args.kommun})")
    print(f"Tatortskod: {tatort.iloc[0]['tatortskod']}")
    print(f"Tiles scanned: {len(tif_paths)}")
    print(f"Tiles used: {used_tiles}")
    print(f"Min altitude: {global_min:.2f} m")
    print(f"Max altitude: {global_max:.2f} m")
    print(f"Altitude range: {global_max - global_min:.2f} m")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
