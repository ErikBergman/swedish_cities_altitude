#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

try:
    from rich.console import Console
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
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
from summarize_lund_altitude import MetricsAccumulator, finalize_metrics, update_metrics_from_tiles


DEFAULT_CITIES = [
    ("Lund", "Lund"),
    ("Malmö", "Malmö"),
    ("Helsingborg", "Helsingborg"),
    ("Kristianstad", "Kristianstad"),
]


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


def main() -> int:
    args = parse_args()
    console = Console()
    username, password = require_credentials()
    work_root = Path(args.work_root) if args.work_root else None
    if work_root is not None:
        work_root.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, float | int | str]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Comparing cities", total=len(DEFAULT_CITIES))

        for tatort_name, kommun_name in DEFAULT_CITIES:
            progress.update(task, description=f"Preparing {tatort_name}")
            tatort = load_tatort(tatort_name, kommun_name)
            collections = find_covering_collections(tatort)
            tiles = fetch_intersecting_tiles(tatort, collections)
            batches = plan_batches(tiles, args.cache_budget_mb * 1024 * 1024)
            verify_asset_access(tiles[0], username, password)

            cache_dir = city_cache_dir(work_root, tatort_name, kommun_name)
            metrics = MetricsAccumulator()

            for index, batch in enumerate(batches, start=1):
                progress.update(task, description=f"{tatort_name}: batch {index}/{len(batches)}")
                batch_bytes = sum(tile.size_bytes for tile in batch)
                current_cache_bytes = cache_usage_bytes(cache_dir)
                if current_cache_bytes + batch_bytes > args.cache_budget_mb * 1024 * 1024:
                    raise RuntimeError(
                        f"{tatort_name}: batch {index} would exceed cache budget. "
                        f"Current cache {format_mb(current_cache_bytes)}, batch {format_mb(batch_bytes)}."
                    )

                download_batch_tiles(batch, cache_dir, username, password, quiet=True)
                batch_paths = [cache_dir / Path(tile.href).name for tile in batch]
                metrics = update_metrics_from_tiles(batch_paths, tatort_name, kommun_name, metrics)

                if not args.keep_cache:
                    for path in batch_paths:
                        if path.exists():
                            path.unlink()

            summary = finalize_metrics(tatort_name, kommun_name, metrics)
            summary["collections"] = ", ".join(collections)
            summary["batches"] = len(batches)
            summary["total_raw_size_mb"] = sum(tile.size_bytes for tile in tiles) / 1024 / 1024
            results.append(summary)

            if not args.keep_cache and cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)

            progress.advance(task)

    results.sort(key=lambda row: float(row["hilliness_score"]), reverse=True)

    table = Table(title="City Hilliness Comparison")
    table.add_column("Rank", justify="right")
    table.add_column("Tatort")
    table.add_column("Kommun")
    table.add_column("Tiles", justify="right")
    table.add_column("Batches", justify="right")
    table.add_column("Min m", justify="right")
    table.add_column("Max m", justify="right")
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
            f"{float(row['max_altitude_m']):.2f}",
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
