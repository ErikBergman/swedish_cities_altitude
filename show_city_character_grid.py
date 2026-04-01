#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.mask import mask
from rasterio.errors import RasterioIOError

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError as error:
    raise SystemExit(
        "Error: matplotlib is not installed in this environment. Run `python -m pip install -r requirements.txt`."
    ) from error

try:
    from rich.console import Console
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TaskProgressColumn, TextColumn, TimeElapsedColumn
except ModuleNotFoundError as error:
    raise SystemExit(
        "Error: rich is not installed in this environment. Run `python -m pip install -r requirements.txt`."
    ) from error

from check_lund_access import TileInfo, load_tatort, require_credentials
from download_lund_batch import download_tile, target_filename
from state_store import StateStore


DEFAULT_CSV = Path("tmp/all_tatorter_hilliness.csv")
DEFAULT_STATE_DB = Path(".state/compare_cities.sqlite")
DEFAULT_CHUNK_ROOT = Path(".state/compare_cities_chunks")
DEFAULT_CACHE_ROOT = Path(".cache/profile_views")
DEFAULT_BINS = 120
DEFAULT_TILE_SAMPLE_LIMIT = 12000
DEFAULT_MAX_CITIES = 9
PROFILE_CACHE_DIRNAME = "profiles"


@dataclass
class RankedCity:
    rank: int | None
    tatort: str
    kommun: str
    score: float | None


@dataclass
class ProfileData:
    city: RankedCity
    x: np.ndarray
    p10: np.ndarray
    p50: np.ndarray
    p90: np.ndarray
    y_min: float
    y_max: float
    width_km: float
    outline_segments: list[np.ndarray]
    view_axis_segment: np.ndarray


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show a popup 3x3 grid of silhouette-style terrain profiles for selected cities."
    )
    parser.add_argument(
        "--csv",
        default=str(DEFAULT_CSV),
        help="Ranked CSV produced by compare_cities.py.",
    )
    parser.add_argument(
        "--state-db",
        default=str(DEFAULT_STATE_DB),
        help="SQLite state DB containing stored city tile plans.",
    )
    parser.add_argument(
        "--chunk-root",
        default=str(DEFAULT_CHUNK_ROOT),
        help="Chunk root used with the SQLite state DB.",
    )
    parser.add_argument(
        "--cache-root",
        default=str(DEFAULT_CACHE_ROOT),
        help="Temporary cache directory used for DEM downloads during plotting.",
    )
    parser.add_argument(
        "--bins",
        type=int,
        default=DEFAULT_BINS,
        help="Number of horizontal bins in each projected profile.",
    )
    parser.add_argument(
        "--tile-sample-limit",
        type=int,
        default=DEFAULT_TILE_SAMPLE_LIMIT,
        help="Maximum sampled pixels per tile when constructing a silhouette profile.",
    )
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help="Keep the downloaded tile cache after plotting.",
    )
    parser.add_argument(
        "--max-cities",
        type=int,
        default=DEFAULT_MAX_CITIES,
        help="Maximum number of cached cities that can be selected in the GUI.",
    )
    return parser.parse_args()


def load_ranked_cities(csv_path: Path) -> list[RankedCity]:
    if not csv_path.exists():
        raise ValueError(f"Ranked CSV does not exist: {csv_path}")
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"Ranked CSV is empty: {csv_path}")
    cities = [
        RankedCity(
            rank=int(row["rank"]),
            tatort=row["tatort"],
            kommun=row["kommun"],
            score=float(row["hilliness_score"]),
        )
        for row in rows
    ]
    return sorted(cities, key=lambda row: row.rank)


def tile_info_list_from_state(state: StateStore, tatort: str, kommun: str) -> list[TileInfo]:
    plan = state.plan_metadata_for_city(tatort, kommun)
    if plan is None:
        raise ValueError(f"No stored tile plan found in the state DB for {tatort} ({kommun}).")
    return [
        TileInfo(
            item_id=row["item_id"],
            collection_id=row["collection_id"],
            href=row["href"],
            size_bytes=int(row["size_bytes"]),
            bbox_3006=(0.0, 0.0, 0.0, 0.0),
        )
        for row in plan["tile_rows"]
    ]


def profile_cache_dir(cache_root: Path, city: RankedCity) -> Path:
    safe_name = f"{city.tatort}_{city.kommun}".lower().replace(" ", "_")
    safe_name = (
        safe_name.replace("å", "a")
        .replace("ä", "a")
        .replace("ö", "o")
        .replace("/", "_")
    )
    path = cache_root / safe_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_cache_root(cache_root: Path) -> Path:
    path = cache_root / PROFILE_CACHE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_cache_key(city: RankedCity, bins: int, tile_sample_limit: int) -> str:
    base = f"{city.rank}_{city.tatort}_{city.kommun}_{bins}_{tile_sample_limit}"
    safe = base.lower().replace(" ", "_")
    return (
        safe.replace("å", "a")
        .replace("ä", "a")
        .replace("ö", "o")
        .replace("/", "_")
    )


def profile_cache_path(cache_root: Path, city: RankedCity, bins: int, tile_sample_limit: int) -> Path:
    return profile_cache_root(cache_root) / f"{profile_cache_key(city, bins, tile_sample_limit)}.json"


def major_axis_for_geometry(geometry) -> tuple[np.ndarray, np.ndarray]:
    rectangle = geometry.minimum_rotated_rectangle
    coords = np.asarray(rectangle.exterior.coords[:4], dtype="float64")
    best_vector = np.array([1.0, 0.0], dtype="float64")
    best_length = 0.0
    for index in range(4):
        vector = coords[(index + 1) % 4] - coords[index]
        length = float(np.hypot(vector[0], vector[1]))
        if length > best_length:
            best_length = length
            best_vector = vector
    if best_length == 0:
        best_vector = np.array([1.0, 0.0], dtype="float64")
    else:
        best_vector = best_vector / best_length
    centroid = np.array([geometry.centroid.x, geometry.centroid.y], dtype="float64")
    return centroid, best_vector


def normalize_point(point: np.ndarray, min_x: float, min_y: float, scale: float) -> np.ndarray:
    return np.array(
        [
            (point[0] - min_x) / scale,
            (point[1] - min_y) / scale,
        ],
        dtype="float64",
    )


def geometry_outline_segments(geometry, centroid: np.ndarray, axis: np.ndarray) -> tuple[list[np.ndarray], np.ndarray]:
    min_x, min_y, max_x, max_y = geometry.bounds
    scale = max(max_x - min_x, max_y - min_y, 1.0)

    def exterior_segments() -> list[np.ndarray]:
        polygons = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
        segments: list[np.ndarray] = []
        for polygon in polygons:
            coords = np.asarray(polygon.exterior.coords, dtype="float64")
            normalized = np.column_stack(
                [
                    (coords[:, 0] - min_x) / scale,
                    (coords[:, 1] - min_y) / scale,
                ]
            )
            segments.append(normalized)
        return segments

    centered_corners = np.asarray(
        [
            [min_x, min_y],
            [min_x, max_y],
            [max_x, min_y],
            [max_x, max_y],
        ],
        dtype="float64",
    ) - centroid
    projection_extent = float(np.max(np.abs(centered_corners @ axis)))
    start = centroid - axis * projection_extent
    end = centroid + axis * projection_extent

    return exterior_segments(), np.vstack(
        [
            normalize_point(start, min_x, min_y, scale),
            normalize_point(end, min_x, min_y, scale),
        ]
    )


def geometry_projection_bounds(geometry, centroid: np.ndarray, axis: np.ndarray) -> tuple[float, float]:
    boundary = geometry.boundary
    candidate_coords: list[tuple[float, float]] = []
    if hasattr(boundary, "geoms"):
        for geom in boundary.geoms:
            candidate_coords.extend(geom.coords)
    else:
        candidate_coords.extend(boundary.coords)
    points = np.asarray(candidate_coords, dtype="float64")
    centered = points - centroid
    projections = centered @ axis
    return float(projections.min()), float(projections.max())


def smooth_series(values: np.ndarray, window: int = 7) -> np.ndarray:
    if window <= 1 or values.size < 3:
        return values
    kernel = np.ones(window, dtype="float64") / window
    padded = np.pad(values, (window // 2,), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def interpolate_nans(values: np.ndarray) -> np.ndarray:
    result = values.astype("float64", copy=True)
    valid = np.isfinite(result)
    if not valid.any():
        return np.zeros_like(result)
    if valid.all():
        return result
    x = np.arange(result.size)
    result[~valid] = np.interp(x[~valid], x[valid], result[valid])
    return result


def accumulate_tile_bins(
    tif_path: Path,
    geometry_interface,
    centroid: np.ndarray,
    axis: np.ndarray,
    projection_min: float,
    projection_max: float,
    bins: int,
    bin_values: list[list[float]],
    tile_sample_limit: int,
) -> None:
    with rasterio.open(tif_path) as dataset:
        try:
            clipped, clipped_transform = mask(dataset, [geometry_interface], crop=True, filled=False)
        except ValueError:
            return

        band = clipped[0]
        values = np.ma.filled(band, np.nan).astype("float64", copy=False)
        valid_mask = np.isfinite(values)
        if dataset.nodata is not None and not math.isnan(dataset.nodata):
            valid_mask &= ~np.isclose(values, dataset.nodata)
        if not valid_mask.any():
            return

        rows, cols = np.nonzero(valid_mask)
        if rows.size > tile_sample_limit:
            step = max(int(math.ceil(rows.size / tile_sample_limit)), 1)
            rows = rows[::step]
            cols = cols[::step]

        sampled_values = values[rows, cols]
        x_coords, y_coords = rasterio.transform.xy(clipped_transform, rows, cols, offset="center")
        points = np.column_stack([x_coords, y_coords]).astype("float64", copy=False)
        projections = (points - centroid) @ axis

        scale = projection_max - projection_min
        if scale <= 0:
            indices = np.zeros_like(projections, dtype=int)
        else:
            normalized = (projections - projection_min) / scale
            indices = np.clip((normalized * bins).astype(int), 0, bins - 1)

        for index, value in zip(indices, sampled_values, strict=False):
            bin_values[int(index)].append(float(value))


def ensure_valid_tile(
    tile: TileInfo,
    destination: Path,
    username: str,
    password: str,
    progress: Progress,
    city_task: TaskID,
) -> None:
    download_tile(tile, destination, username, password, quiet=True)
    try:
        with rasterio.open(destination):
            return
    except RasterioIOError:
        progress.update(city_task, description=f"Re-downloading corrupt tile {destination.name}")
        destination.unlink(missing_ok=True)
        download_tile(tile, destination, username, password, quiet=True)
        with rasterio.open(destination):
            return


def build_profile_for_city(
    city: RankedCity,
    state: StateStore,
    cache_root: Path,
    bins: int,
    tile_sample_limit: int,
    keep_cache: bool,
    username: str,
    password: str,
    progress: Progress,
    city_task: TaskID,
) -> ProfileData:
    tatort = load_tatort(city.tatort, city.kommun)
    geometry = tatort.geometry.iloc[0]
    geometry_interface = geometry.__geo_interface__
    centroid, axis = major_axis_for_geometry(geometry)
    outline_segments, view_axis_segment = geometry_outline_segments(geometry, centroid, axis)
    projection_min, projection_max = geometry_projection_bounds(geometry, centroid, axis)
    width_km = max(projection_max - projection_min, 0.0) / 1000

    cache_dir = profile_cache_dir(cache_root, city)
    tiles = tile_info_list_from_state(state, city.tatort, city.kommun)
    progress.update(city_task, total=max(len(tiles) * 2, 1), completed=0)
    for tile in tiles:
        progress.update(city_task, description=f"{city.tatort}: downloading {target_filename(tile.href)}")
        destination = cache_dir / target_filename(tile.href)
        ensure_valid_tile(tile, destination, username, password, progress, city_task)
        progress.advance(city_task)

    bin_values: list[list[float]] = [[] for _ in range(bins)]
    try:
        for tile in tiles:
            tif_path = cache_dir / target_filename(tile.href)
            progress.update(city_task, description=f"{city.tatort}: profiling {tif_path.name}")
            accumulate_tile_bins(
                tif_path,
                geometry_interface,
                centroid,
                axis,
                projection_min,
                projection_max,
                bins,
                bin_values,
                tile_sample_limit,
            )
            progress.advance(city_task)
    finally:
        if not keep_cache and cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)

    p10 = np.array(
        [np.percentile(bucket, 10) if bucket else np.nan for bucket in bin_values],
        dtype="float64",
    )
    p50 = np.array(
        [np.percentile(bucket, 50) if bucket else np.nan for bucket in bin_values],
        dtype="float64",
    )
    p90 = np.array(
        [np.percentile(bucket, 90) if bucket else np.nan for bucket in bin_values],
        dtype="float64",
    )
    p10 = smooth_series(interpolate_nans(p10))
    p50 = smooth_series(interpolate_nans(p50))
    p90 = smooth_series(interpolate_nans(p90))

    x = np.linspace(0.0, 1.0, bins)
    return ProfileData(
        city=city,
        x=x,
        p10=p10,
        p50=p50,
        p90=p90,
        y_min=float(np.nanmin(p10)),
        y_max=float(np.nanmax(p90)),
        width_km=width_km,
        outline_segments=outline_segments,
        view_axis_segment=view_axis_segment,
    )


def add_shape_inset(axis, profile: ProfileData) -> None:
    inset = axis.inset_axes([0.72, 0.6, 0.24, 0.3])
    inset.set_facecolor((1.0, 1.0, 1.0, 0.9))
    for segment in profile.outline_segments:
        inset.plot(segment[:, 0], segment[:, 1], color="#64748b", linewidth=0.9)
    raw_start = profile.view_axis_segment[0]
    raw_end = profile.view_axis_segment[1]
    vector = raw_end - raw_start
    length = float(np.hypot(vector[0], vector[1]))
    if length == 0:
        direction = np.array([1.0, 0.0], dtype="float64")
    else:
        direction = vector / length
    midpoint = (raw_start + raw_end) / 2.0
    half_span = 0.32
    start = midpoint - direction * half_span
    end = midpoint + direction * half_span
    inset.plot(
        [start[0], end[0]],
        [start[1], end[1]],
        color="#dc2626",
        linewidth=1.8,
        zorder=5,
    )
    inset.annotate(
        "",
        xy=end,
        xytext=start,
        arrowprops={
            "arrowstyle": "-|>",
            "color": "#dc2626",
            "lw": 1.8,
            "mutation_scale": 16,
            "shrinkA": 0,
            "shrinkB": 0,
        },
        zorder=6,
        clip_on=True,
    )
    inset.set_xlim(-0.05, 1.05)
    inset.set_ylim(-0.05, 1.05)
    inset.set_aspect("equal", adjustable="box")
    inset.set_xticks([])
    inset.set_yticks([])
    inset.set_title("Shape + view", fontsize=6, pad=1)
    for spine in inset.spines.values():
        spine.set_alpha(0.35)


def save_profile_cache(path: Path, profile: ProfileData) -> None:
    payload = {
        "rank": profile.city.rank,
        "tatort": profile.city.tatort,
        "kommun": profile.city.kommun,
        "score": profile.city.score,
        "x": profile.x.tolist(),
        "p10": profile.p10.tolist(),
        "p50": profile.p50.tolist(),
        "p90": profile.p90.tolist(),
        "y_min": profile.y_min,
        "y_max": profile.y_max,
        "width_km": profile.width_km,
        "outline_segments": [segment.tolist() for segment in profile.outline_segments],
        "view_axis_segment": profile.view_axis_segment.tolist(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def load_profile_cache(path: Path) -> ProfileData:
    payload = json.loads(path.read_text(encoding="utf-8"))
    city = RankedCity(
        rank=int(payload["rank"]) if payload["rank"] is not None else None,
        tatort=payload["tatort"],
        kommun=payload["kommun"],
        score=float(payload["score"]) if payload["score"] is not None else None,
    )
    return ProfileData(
        city=city,
        x=np.asarray(payload["x"], dtype="float64"),
        p10=np.asarray(payload["p10"], dtype="float64"),
        p50=np.asarray(payload["p50"], dtype="float64"),
        p90=np.asarray(payload["p90"], dtype="float64"),
        y_min=float(payload["y_min"]),
        y_max=float(payload["y_max"]),
        width_km=float(payload["width_km"]),
        outline_segments=[
            np.asarray(segment, dtype="float64") for segment in payload["outline_segments"]
        ],
        view_axis_segment=np.asarray(payload["view_axis_segment"], dtype="float64"),
    )


def city_title(profile: ProfileData) -> str:
    if profile.city.rank is not None and profile.city.score is not None:
        return f"{profile.city.tatort}\nrank {profile.city.rank}, score {profile.city.score:.1f}"
    return f"{profile.city.tatort}\ncustom selection"


def split_panel_axes(container_axis):
    container_axis.set_xticks([])
    container_axis.set_yticks([])
    for spine in container_axis.spines.values():
        spine.set_visible(False)

    top_axis = container_axis.inset_axes([0.0, 0.14, 1.0, 0.86])
    bottom_axis = container_axis.inset_axes([0.0, 0.0, 1.0, 0.10])
    return top_axis, bottom_axis


def list_cached_profiles(cache_root: Path, bins: int) -> list[ProfileData]:
    profiles_dir = profile_cache_root(cache_root)
    candidates = sorted(profiles_dir.glob("*.json"))
    latest_by_city: dict[tuple[str, str], tuple[float, ProfileData]] = {}

    for path in candidates:
        try:
            profile = load_profile_cache(path)
        except Exception:
            continue
        if profile.x.size != bins:
            continue
        key = (profile.city.tatort, profile.city.kommun)
        current = latest_by_city.get(key)
        mtime = path.stat().st_mtime
        if current is None or mtime > current[0]:
            latest_by_city[key] = (mtime, profile)

    profiles = [item[1] for item in latest_by_city.values()]
    return sorted(
        profiles,
        key=lambda profile: (
            profile.city.rank is None,
            profile.city.rank if profile.city.rank is not None else 10**9,
            profile.city.tatort.casefold(),
            profile.city.kommun.casefold(),
        ),
    )


def select_profiles_gui(cached_profiles: list[ProfileData], max_cities: int) -> list[ProfileData]:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ModuleNotFoundError as error:
        raise SystemExit("Error: tkinter is not available in this environment.") from error

    selected: list[ProfileData] = []
    root = tk.Tk()
    root.title("Select Cities")
    root.geometry("520x420")

    tk.Label(
        root,
        text=f"Select up to {max_cities} cached cities to include in the graph:",
        anchor="w",
    ).pack(fill="x", padx=12, pady=(12, 6))

    frame = tk.Frame(root)
    frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    scrollbar = tk.Scrollbar(frame)
    scrollbar.pack(side="right", fill="y")

    listbox = tk.Listbox(
        frame,
        selectmode=tk.EXTENDED,
        yscrollcommand=scrollbar.set,
        exportselection=False,
    )
    listbox.pack(side="left", fill="both", expand=True)
    scrollbar.config(command=listbox.yview)

    for index, profile in enumerate(cached_profiles):
        if profile.city.rank is not None and profile.city.score is not None:
            label = (
                f"{profile.city.tatort} ({profile.city.kommun}) | "
                f"rank {profile.city.rank} | score {profile.city.score:.1f}"
            )
        else:
            label = f"{profile.city.tatort} ({profile.city.kommun}) | cached"
        listbox.insert(tk.END, label)
        if index < min(3, len(cached_profiles)):
            listbox.selection_set(index)

    def on_ok() -> None:
        indexes = listbox.curselection()
        if not indexes:
            messagebox.showwarning("No selection", "Select at least one city.")
            return
        if len(indexes) > max_cities:
            messagebox.showwarning(
                "Too many cities",
                f"Select at most {max_cities} cities.",
            )
            return
        selected.extend(cached_profiles[index] for index in indexes)
        root.destroy()

    def on_cancel() -> None:
        root.destroy()

    button_row = tk.Frame(root)
    button_row.pack(fill="x", padx=12, pady=(0, 12))
    tk.Button(button_row, text="Cancel", command=on_cancel).pack(side="right")
    tk.Button(button_row, text="Open graph", command=on_ok).pack(side="right", padx=(0, 8))

    root.mainloop()
    if not selected:
        raise SystemExit("No cities selected.")
    return selected


def main() -> int:
    args = parse_args()
    console = Console()
    cache_root = Path(args.cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    cached_profiles = list_cached_profiles(cache_root, args.bins)
    if not cached_profiles:
        raise RuntimeError(
            "No cached city profiles found. Run the viewer once with cached profiling enabled before using the selector."
        )

    selected_profiles = select_profiles_gui(cached_profiles, args.max_cities)
    selected_profiles = selected_profiles[: args.max_cities]
    global_min = min(profile.y_min for profile in selected_profiles)
    global_max = max(profile.y_max for profile in selected_profiles)
    max_half_span_km = max(profile.width_km for profile in selected_profiles) / 2.0

    if not math.isfinite(global_min) or not math.isfinite(global_max):
        raise RuntimeError("Could not build any city profiles from the selected data.")

    console.print(f"Loaded {len(cached_profiles)} cached city profiles.")
    console.print(f"Selected {len(selected_profiles)} cities:")
    for profile in selected_profiles:
        if profile.city.rank is not None:
            console.print(f"  rank {profile.city.rank}: {profile.city.tatort} ({profile.city.kommun})")
        else:
            console.print(f"  cached: {profile.city.tatort} ({profile.city.kommun})")

    console.print("Opening plot window...")
    columns = min(3, len(selected_profiles))
    rows = math.ceil(len(selected_profiles) / columns)
    figure, axes = plt.subplots(rows, columns, figsize=(4.8 * columns, 3.5 * rows), sharex=True, sharey=True)
    axes_array = np.atleast_1d(axes).reshape(rows, columns)
    figure.suptitle("Tatort Altitude Character Profiles", fontsize=16, y=0.992)
    figure.text(
        0.5,
        0.964,
        "Each panel compresses one city's terrain into a side-view style shape, so you can compare whether a city is broadly flat, valley-like, or split into several higher and lower parts.",
        ha="center",
        va="top",
        fontsize=11,
        color="#0f172a",
        wrap=True,
    )
    figure.text(
        0.5,
        0.924,
        "Technical summary: DEM pixels inside each tatort polygon are projected onto the city's major axis, grouped into bins along that span, and summarized by percentiles per bin.",
        ha="center",
        va="top",
        fontsize=8.5,
        color="#475569",
        wrap=True,
    )
    figure.text(
        0.5,
        0.898,
        "Dark blue line: median projected elevation profile. Light blue band: 10th-90th percentile elevation range across each slice.",
        ha="center",
        va="top",
        fontsize=10,
        color="#0f4c5c",
        wrap=True,
    )

    flat_axes = list(axes_array.flat)
    for index, axis in enumerate(flat_axes):
        if index >= len(selected_profiles):
            axis.axis("off")
            continue

        profile = selected_profiles[index]
        row_index = index // columns
        col_index = index % columns
        profile_axis, width_axis = split_panel_axes(axis)

        profile_axis.fill_between(profile.x, profile.p10, profile.p90, color="#b9d9eb", alpha=0.9)
        profile_axis.plot(profile.x, profile.p50, color="#0f4c5c", linewidth=2.0)
        profile_axis.set_ylim(global_min, global_max)
        profile_axis.set_xlim(0.0, 1.0)
        profile_axis.grid(alpha=0.2, linewidth=0.5)
        profile_axis.set_title(city_title(profile), fontsize=10)
        profile_axis.text(
            0.02,
            0.04,
            f"{profile.width_km:.1f} km span",
            transform=profile_axis.transAxes,
            fontsize=8,
            ha="left",
            va="bottom",
        )
        add_shape_inset(profile_axis, profile)

        width_axis.set_xlim(-max_half_span_km, max_half_span_km)
        width_axis.set_ylim(0.0, 1.0)
        width_axis.set_facecolor("#f8fafc")
        width_axis.axvline(0.0, color="#94a3b8", linewidth=0.8, alpha=0.9)
        width_axis.set_xticks([-max_half_span_km, 0.0, max_half_span_km])
        width_axis.set_xticklabels(
            [
                f"{-max_half_span_km:.1f}",
                "0",
                f"{max_half_span_km:.1f}",
            ],
            fontsize=8,
        )
        width_axis.set_yticks([])
        for spine in width_axis.spines.values():
            spine.set_alpha(0.35)

        if col_index == 0:
            profile_axis.set_ylabel("Altitude (m)")
        if row_index == rows - 1:
            width_axis.set_xlabel("Distance from center (km)")
        else:
            profile_axis.set_xticklabels([])

    plt.tight_layout(rect=(0, 0, 1, 0.80))
    plt.show()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
