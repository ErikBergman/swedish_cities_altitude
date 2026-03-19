#!/usr/bin/env python3

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.mask import mask

from check_lund_access import DEFAULT_KOMMUN, DEFAULT_TATORT, load_tatort


SWEREF99TM_TO_WGS84 = Transformer.from_crs(3006, 4326, always_xy=True)


@dataclass
class MetricsAccumulator:
    elevation_chunks: list[np.ndarray] = field(default_factory=list)
    global_min: float = math.inf
    global_max: float = -math.inf
    min_coord_3006: tuple[float, float] | None = None
    max_coord_3006: tuple[float, float] | None = None
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


def extreme_value_coords(band, nodata, transform) -> tuple[float, tuple[float, float], float, tuple[float, float]] | None:
    values = np.ma.filled(band, np.nan).astype("float64", copy=False)
    valid_mask = np.isfinite(values)
    if nodata is not None and not math.isnan(nodata):
        valid_mask &= ~np.isclose(values, nodata)

    if not valid_mask.any():
        return None

    min_candidates = np.where(valid_mask, values, np.inf)
    max_candidates = np.where(valid_mask, values, -np.inf)

    min_index = np.unravel_index(np.argmin(min_candidates), values.shape)
    max_index = np.unravel_index(np.argmax(max_candidates), values.shape)

    min_value = float(values[min_index])
    max_value = float(values[max_index])
    min_x, min_y = rasterio.transform.xy(transform, min_index[0], min_index[1], offset="center")
    max_x, max_y = rasterio.transform.xy(transform, max_index[0], max_index[1], offset="center")
    return min_value, (float(min_x), float(min_y)), max_value, (float(max_x), float(max_y))


def format_coord(coord: tuple[float, float] | None) -> str:
    if coord is None:
        return "N/A"
    lon, lat = SWEREF99TM_TO_WGS84.transform(coord[0], coord[1])
    return f"({lat:.6f}, {lon:.6f})"


def update_metrics_from_tiles(
    tif_paths: list[Path],
    tatort_name: str,
    kommun_name: str,
    accumulator: MetricsAccumulator | None = None,
    tile_callback: Callable[[], None] | None = None,
) -> MetricsAccumulator:
    tatort = load_tatort(tatort_name, kommun_name)
    geometry = [tatort.geometry.iloc[0].__geo_interface__]
    metrics = accumulator or MetricsAccumulator()

    for tif_path in tif_paths:
        metrics.tiles_scanned += 1
        with rasterio.open(tif_path) as dataset:
            try:
                clipped, clipped_transform = mask(dataset, geometry, crop=True, filled=False)
            except ValueError:
                continue

            values = valid_values(clipped[0], dataset.nodata)
            if values.size == 0:
                continue

            metrics.elevation_chunks.append(values.astype("float32", copy=False))
            local_extremes = extreme_value_coords(clipped[0], dataset.nodata, clipped_transform)
            if local_extremes is not None:
                local_min, local_min_coord, local_max, local_max_coord = local_extremes
                if local_min < metrics.global_min:
                    metrics.global_min = local_min
                    metrics.min_coord_3006 = local_min_coord
                if local_max > metrics.global_max:
                    metrics.global_max = local_max
                    metrics.max_coord_3006 = local_max_coord

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
            if tile_callback is not None:
                tile_callback()

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
        "min_coord_3006": metrics.min_coord_3006,
        "max_coord_3006": metrics.max_coord_3006,
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
    print(f"Min coordinate (lat, lon): {format_coord(summary['min_coord_3006'])}")
    print(f"Max altitude: {summary['max_altitude_m']:.2f} m")
    print(f"Max coordinate (lat, lon): {format_coord(summary['max_coord_3006'])}")
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
