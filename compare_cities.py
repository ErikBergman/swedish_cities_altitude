#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.table import Table
except ModuleNotFoundError as error:
    raise SystemExit(
        "Error: rich is not installed in this environment. Run `python -m pip install -r requirements.txt`."
    ) from error

from check_lund_access import (
    DEFAULT_CACHE_BUDGET_MB,
    fetch_intersecting_tiles,
    find_covering_collections,
    format_mb,
    load_tatort,
    plan_batches,
    require_credentials,
    verify_asset_access,
)
from download_lund_batch import cache_usage_bytes, download_batch_tiles
from summarize_lund_altitude import MetricsAccumulator, finalize_metrics, format_coord, update_metrics_from_tiles


DEFAULT_CITIES = [
    ("Lund", "Lund"),
    ("Malmö", "Malmö"),
    ("Helsingborg", "Helsingborg"),
    ("Kristianstad", "Kristianstad"),
]
DEFAULT_DOWNLOAD_RATE_MBPS = 16.0
DEFAULT_PROCESSING_SECONDS_PER_TILE = 0.5


@dataclass
class CityPlan:
    tatort: str
    kommun: str
    collections: list[str]
    tiles: list
    batches: list[list]
    total_bytes: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare hilliness metrics for a small set of Swedish tatorter."
    )
    parser.add_argument(
        "--cache-budget-mb",
        type=int,
        default=DEFAULT_CACHE_BUDGET_MB,
        help="Maximum temporary cache size used when planning download batches.",
    )
    parser.add_argument(
        "--work-root",
        help="Optional parent directory for per-city temporary caches.",
    )
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help="Keep downloaded cache directories after processing.",
    )
    parser.add_argument(
        "--download-rate-mbps",
        type=float,
        default=DEFAULT_DOWNLOAD_RATE_MBPS,
        help="Initial download speed estimate in megabits per second for ETA calculations.",
    )
    parser.add_argument(
        "--processing-seconds-per-tile",
        type=float,
        default=DEFAULT_PROCESSING_SECONDS_PER_TILE,
        help="Initial processing time estimate per tile for ETA calculations.",
    )
    return parser.parse_args()


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_text.lower().replace(" ", "_")


def city_cache_dir(work_root: Path | None, tatort: str, kommun: str) -> Path:
    if work_root is None:
        prefix = f"swedish_cities_altitude_{slugify(tatort)}_{slugify(kommun)}_"
        return Path(tempfile.mkdtemp(prefix=prefix))

    path = work_root / f"{slugify(tatort)}_{slugify(kommun)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def estimate_download_seconds(total_bytes: int, download_rate_mbps: float) -> float:
    bytes_per_second = max(download_rate_mbps, 0.1) * 1_000_000 / 8
    return total_bytes / bytes_per_second


def estimate_processing_seconds(tile_count: int, seconds_per_tile: float) -> float:
    return tile_count * max(seconds_per_tile, 0.01)


def format_seconds(seconds: float) -> str:
    total_seconds = max(int(round(seconds)), 0)
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def remaining_download_seconds(
    total_bytes: int,
    downloaded_bytes: int,
    default_download_rate_mbps: float,
    observed_download_seconds: float,
) -> float:
    bytes_remaining = max(total_bytes - downloaded_bytes, 0)
    if bytes_remaining == 0:
        return 0.0

    if downloaded_bytes > 0 and observed_download_seconds > 0:
        bytes_per_second = downloaded_bytes / observed_download_seconds
    else:
        bytes_per_second = max(default_download_rate_mbps, 0.1) * 1_000_000 / 8
    return bytes_remaining / max(bytes_per_second, 1.0)


def remaining_processing_seconds(
    total_tiles: int,
    processed_tiles: int,
    default_seconds_per_tile: float,
    observed_processing_seconds: float,
) -> float:
    tiles_remaining = max(total_tiles - processed_tiles, 0)
    if tiles_remaining == 0:
        return 0.0

    if processed_tiles > 0 and observed_processing_seconds > 0:
        seconds_per_tile = observed_processing_seconds / processed_tiles
    else:
        seconds_per_tile = max(default_seconds_per_tile, 0.01)
    return tiles_remaining * max(seconds_per_tile, 0.01)


def build_city_plans(cache_budget_mb: int) -> list[CityPlan]:
    plans: list[CityPlan] = []
    cache_budget_bytes = cache_budget_mb * 1024 * 1024
    for tatort_name, kommun_name in DEFAULT_CITIES:
        tatort = load_tatort(tatort_name, kommun_name)
        collections = find_covering_collections(tatort)
        tiles = fetch_intersecting_tiles(tatort, collections)
        batches = plan_batches(tiles, cache_budget_bytes)
        plans.append(
            CityPlan(
                tatort=tatort_name,
                kommun=kommun_name,
                collections=collections,
                tiles=tiles,
                batches=batches,
                total_bytes=sum(tile.size_bytes for tile in tiles),
            )
        )
    return plans


def main() -> int:
    args = parse_args()
    console = Console()
    username, password = require_credentials()
    work_root = Path(args.work_root) if args.work_root else None
    if work_root is not None:
        work_root.mkdir(parents=True, exist_ok=True)

    plans = build_city_plans(args.cache_budget_mb)
    preflight = Table(title="City Processing Estimates")
    preflight.add_column("Tatort")
    preflight.add_column("Kommun")
    preflight.add_column("Tiles", justify="right")
    preflight.add_column("Batches", justify="right")
    preflight.add_column("Raw Size", justify="right")
    preflight.add_column("Est. Time", justify="right")
    for plan in plans:
        estimate_seconds = estimate_download_seconds(plan.total_bytes, args.download_rate_mbps) + estimate_processing_seconds(
            len(plan.tiles), args.processing_seconds_per_tile
        )
        preflight.add_row(
            plan.tatort,
            plan.kommun,
            str(len(plan.tiles)),
            str(len(plan.batches)),
            format_mb(plan.total_bytes),
            format_seconds(estimate_seconds),
        )
    console.print(preflight)
    console.print()

    results: list[dict[str, float | int | str]] = []
    current_download_rate_mbps = args.download_rate_mbps
    current_processing_seconds_per_tile = args.processing_seconds_per_tile

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        overall_task = progress.add_task("Comparing cities", total=len(plans))

        for plan in plans:
            estimated_city_seconds = estimate_download_seconds(plan.total_bytes, current_download_rate_mbps) + estimate_processing_seconds(
                len(plan.tiles), current_processing_seconds_per_tile
            )
            city_task = progress.add_task(
                f"{plan.tatort}: estimated {format_seconds(estimated_city_seconds)}",
                total=max(estimated_city_seconds, 0.01),
            )
            verify_asset_access(plan.tiles[0], username, password)

            cache_dir = city_cache_dir(work_root, plan.tatort, plan.kommun)
            metrics = MetricsAccumulator()
            downloaded_bytes = 0
            processing_tile_count = 0
            download_seconds = 0.0
            processing_seconds = 0.0
            city_started_at = time.perf_counter()

            def refresh_city_task(phase: str) -> None:
                elapsed_seconds = time.perf_counter() - city_started_at
                remaining_seconds = remaining_download_seconds(
                    plan.total_bytes,
                    downloaded_bytes,
                    current_download_rate_mbps,
                    download_seconds,
                ) + remaining_processing_seconds(
                    len(plan.tiles),
                    processing_tile_count,
                    current_processing_seconds_per_tile,
                    processing_seconds,
                )
                total_seconds = max(elapsed_seconds + remaining_seconds, elapsed_seconds + 0.01)
                progress.update(
                    city_task,
                    completed=elapsed_seconds,
                    total=total_seconds,
                    description=(
                        f"{plan.tatort}: {phase} | "
                        f"{downloaded_bytes / 1024 / 1024:.1f}/{plan.total_bytes / 1024 / 1024:.1f} MB | "
                        f"{processing_tile_count}/{len(plan.tiles)} tiles"
                    ),
                )

            refresh_city_task(f"starting, est. {format_seconds(estimated_city_seconds)}")

            for index, batch in enumerate(plan.batches, start=1):
                refresh_city_task(f"downloading batch {index}/{len(plan.batches)}")
                batch_bytes = sum(tile.size_bytes for tile in batch)
                current_cache_bytes = cache_usage_bytes(cache_dir)
                if current_cache_bytes + batch_bytes > args.cache_budget_mb * 1024 * 1024:
                    raise RuntimeError(
                        f"{plan.tatort}: batch {index} would exceed cache budget. "
                        f"Current cache {format_mb(current_cache_bytes)}, batch {format_mb(batch_bytes)}."
                    )

                def on_download(chunk_size: int) -> None:
                    nonlocal downloaded_bytes
                    downloaded_bytes += chunk_size
                    refresh_city_task(f"downloading batch {index}/{len(plan.batches)}")

                download_start = time.perf_counter()
                download_batch_tiles(batch, cache_dir, username, password, quiet=True, progress_callback=on_download)
                download_seconds += time.perf_counter() - download_start
                refresh_city_task(f"processing batch {index}/{len(plan.batches)}")

                batch_paths = [cache_dir / Path(tile.href).name for tile in batch]

                def on_processed_tile() -> None:
                    nonlocal processing_tile_count
                    processing_tile_count += 1
                    refresh_city_task(f"processing batch {index}/{len(plan.batches)}")

                processing_start = time.perf_counter()
                metrics = update_metrics_from_tiles(
                    batch_paths,
                    plan.tatort,
                    plan.kommun,
                    metrics,
                    tile_callback=on_processed_tile,
                )
                processing_seconds += time.perf_counter() - processing_start

                if not args.keep_cache:
                    for path in batch_paths:
                        if path.exists():
                            path.unlink()

            summary = finalize_metrics(plan.tatort, plan.kommun, metrics)
            summary["collections"] = ", ".join(plan.collections)
            summary["batches"] = len(plan.batches)
            summary["total_raw_size_mb"] = plan.total_bytes / 1024 / 1024
            results.append(summary)

            if not args.keep_cache and cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)

            refresh_city_task("completed")
            progress.update(city_task, completed=progress.tasks[city_task].total)
            progress.advance(overall_task)

            if downloaded_bytes > 0 and download_seconds > 0:
                current_download_rate_mbps = (downloaded_bytes * 8 / 1_000_000) / download_seconds
            if processing_tile_count > 0 and processing_seconds > 0:
                current_processing_seconds_per_tile = processing_seconds / processing_tile_count

    results.sort(key=lambda row: float(row["hilliness_score"]), reverse=True)

    table = Table(title="City Hilliness Comparison")
    table.add_column("Rank", justify="right")
    table.add_column("Tatort")
    table.add_column("Kommun")
    table.add_column("Tiles", justify="right")
    table.add_column("Batches", justify="right")
    table.add_column("Min m", justify="right")
    table.add_column("Min Coord")
    table.add_column("Max m", justify="right")
    table.add_column("Max Coord")
    table.add_column("Norm Relief", justify="right")
    table.add_column("RMS Slope", justify="right")
    table.add_column("Score", justify="right")

    for index, row in enumerate(results, start=1):
        table.add_row(
            str(index),
            str(row["tatort"]),
            str(row["kommun"]),
            str(row["tiles_used"]),
            str(row["batches"]),
            f"{float(row['min_altitude_m']):.2f}",
            format_coord(row["min_coord_3006"]),
            f"{float(row['max_altitude_m']):.2f}",
            format_coord(row["max_coord_3006"]),
            f"{float(row['normalized_relief']):.2f}",
            f"{float(row['rms_slope_deg']):.2f}",
            f"{float(row['hilliness_score']):.2f}",
        )

    console.print()
    console.print(table)
    console.print()
    for row in results:
        console.print(
            f"{row['tatort']}: {row['collections']} | "
            f"{int(row['tiles_scanned'])} tiles scanned | "
            f"{float(row['total_raw_size_mb']):.2f} MB raw"
        )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
