#!/usr/bin/env python3

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import rasterio
from rasterio.mask import mask

from check_lund_access import DEFAULT_KOMMUN, DEFAULT_TATORT, load_tatort


@dataclass
class MetricsAccumulator:
    elevation_chunks: list[np.ndarray] = field(default_factory=list)
    global_min: float = math.inf
    global_max: float = -math.inf
    slope_sum_squares: float = 0.0
    slope_count: int = 0
    tiles_scanned: int = 0
    tiles_used: int = 0


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


def update_metrics_from_tiles(
    tif_paths: list[Path],
    tatort_name: str,
    kommun_name: str,
    accumulator: MetricsAccumulator | None = None,
) -> MetricsAccumulator:
    tatort = load_tatort(tatort_name, kommun_name)
    geometry = [tatort.geometry.iloc[0].__geo_interface__]
    metrics = accumulator or MetricsAccumulator()

    for tif_path in tif_paths:
        metrics.tiles_scanned += 1
        with rasterio.open(tif_path) as dataset:
            try:
                clipped, _ = mask(dataset, geometry, crop=True, filled=False)
            except ValueError:
                continue

            values = valid_values(clipped[0], dataset.nodata)
            if values.size == 0:
                continue

            metrics.elevation_chunks.append(values.astype("float32", copy=False))
            metrics.global_min = min(metrics.global_min, float(values.min()))
            metrics.global_max = max(metrics.global_max, float(values.max()))

            slope = slope_values(
                clipped[0],
                dataset.nodata,
                abs(dataset.transform.a),
                abs(dataset.transform.e),
            )
            if slope.size:
                metrics.slope_sum_squares += float(np.square(slope, dtype="float64").sum())
                metrics.slope_count += int(slope.size)

            metrics.tiles_used += 1

    return metrics


def finalize_metrics(
    tatort_name: str,
    kommun_name: str,
    metrics: MetricsAccumulator,
) -> dict[str, float | int | str]:
    if not math.isfinite(metrics.global_min) or not math.isfinite(metrics.global_max) or not metrics.elevation_chunks:
        raise RuntimeError("No valid DEM pixels intersected the tatort in the provided tiles.")

    tatort = load_tatort(tatort_name, kommun_name)
    elevations = np.concatenate(metrics.elevation_chunks)
    relief_q05 = float(np.percentile(elevations, 5))
    relief_q95 = float(np.percentile(elevations, 95))
    area_km2 = float(tatort.geometry.iloc[0].area) / 1_000_000
    normalized_relief = (relief_q95 - relief_q05) / math.sqrt(area_km2)
    rms_slope = math.sqrt(metrics.slope_sum_squares / metrics.slope_count) if metrics.slope_count else 0.0
    hilliness_score = normalized_relief * rms_slope

    return {
        "tatort": tatort_name,
        "kommun": kommun_name,
        "tatortskod": tatort.iloc[0]["tatortskod"],
        "tiles_scanned": metrics.tiles_scanned,
        "tiles_used": metrics.tiles_used,
        "min_altitude_m": metrics.global_min,
        "max_altitude_m": metrics.global_max,
        "altitude_range_m": metrics.global_max - metrics.global_min,
        "relief_q05_m": relief_q05,
        "relief_q95_m": relief_q95,
        "normalized_relief": normalized_relief,
        "rms_slope_deg": rms_slope,
        "hilliness_score": hilliness_score,
    }


def main() -> int:
    args = parse_args()
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_dir():
        raise ValueError(f"Cache directory does not exist: {cache_dir}")

    tif_paths = sorted(cache_dir.glob("*.tif"))
    if not tif_paths:
        raise ValueError(f"No .tif files found in cache directory: {cache_dir}")

    summary = finalize_metrics(
        args.tatort,
        args.kommun,
        update_metrics_from_tiles(tif_paths, args.tatort, args.kommun),
    )

    print(f"Tatort: {summary['tatort']} ({summary['kommun']})")
    print(f"Tatortskod: {summary['tatortskod']}")
    print(f"Tiles scanned: {summary['tiles_scanned']}")
    print(f"Tiles used: {summary['tiles_used']}")
    print(f"Min altitude: {summary['min_altitude_m']:.2f} m")
    print(f"Max altitude: {summary['max_altitude_m']:.2f} m")
    print(f"Altitude range: {summary['altitude_range_m']:.2f} m")
    print(f"Relief Q05: {summary['relief_q05_m']:.2f} m")
    print(f"Relief Q95: {summary['relief_q95_m']:.2f} m")
    print(f"Normalized relief: {summary['normalized_relief']:.2f}")
    print(f"RMS slope: {summary['rms_slope_deg']:.2f} deg")
    print(f"Hilliness score: {summary['hilliness_score']:.2f}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
