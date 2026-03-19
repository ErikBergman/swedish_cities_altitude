#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from check_lund_access import (
    DEFAULT_CACHE_BUDGET_MB,
    DEFAULT_KOMMUN,
    DEFAULT_TATORT,
    describe_batch,
    fetch_intersecting_tiles,
    find_covering_collections,
    format_mb,
    load_tatort,
    plan_batches,
    require_credentials,
    verify_asset_access,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download one planned DEM batch for a tatort into a temporary cache directory."
        )
    )
    parser.add_argument("--tatort", default=DEFAULT_TATORT, help="Tatort name to inspect.")
    parser.add_argument("--kommun", default=DEFAULT_KOMMUN, help="Kommun name to disambiguate the tatort.")
    parser.add_argument(
        "--cache-budget-mb",
        type=int,
        default=DEFAULT_CACHE_BUDGET_MB,
        help="Maximum temporary cache size used when planning batches.",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=1,
        help="1-based batch index to download.",
    )
    parser.add_argument(
        "--cache-dir",
        help=(
            "Optional cache directory. Defaults to a new temporary directory under the system temp area."
        ),
    )
    return parser.parse_args()


def build_auth_opener(username: str, password: str, url: str):
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, url, username, password)
    auth_handler = urllib.request.HTTPBasicAuthHandler(password_manager)
    return urllib.request.build_opener(auth_handler)


def default_cache_dir(tatort: str, kommun: str) -> Path:
    name = f"swedish_cities_altitude_{tatort.lower()}_{kommun.lower()}"
    return Path(tempfile.mkdtemp(prefix=f"{name}_"))


def target_filename(href: str) -> str:
    return Path(urlparse(href).path).name


def download_tile(tile, destination: Path, username: str, password: str, quiet: bool = False) -> None:
    if destination.exists() and destination.stat().st_size == tile.size_bytes:
        if not quiet:
            print(f"Skipping existing tile: {destination.name}")
        return

    opener = build_auth_opener(username, password, tile.href)
    request = urllib.request.Request(tile.href, method="GET")
    with opener.open(request, timeout=120) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)

    downloaded_size = destination.stat().st_size
    if downloaded_size != tile.size_bytes:
        raise RuntimeError(
            f"Downloaded size mismatch for {destination.name}: "
            f"expected {tile.size_bytes}, got {downloaded_size}."
        )

    if not quiet:
        print(f"Downloaded {destination.name} ({format_mb(tile.size_bytes)})")


def cache_usage_bytes(cache_dir: Path) -> int:
    return sum(path.stat().st_size for path in cache_dir.glob("*") if path.is_file())


def download_batch_tiles(selected_batch, cache_dir: Path, username: str, password: str, quiet: bool = False) -> int:
    total_batch_bytes = sum(tile.size_bytes for tile in selected_batch)
    for tile in selected_batch:
        destination = cache_dir / target_filename(tile.href)
        download_tile(tile, destination, username, password, quiet=quiet)
    return total_batch_bytes


def main() -> int:
    args = parse_args()
    tatort = load_tatort(args.tatort, args.kommun)
    collections = find_covering_collections(tatort)
    tiles = fetch_intersecting_tiles(tatort, collections)
    batches = plan_batches(tiles, args.cache_budget_mb * 1024 * 1024)

    if args.batch < 1 or args.batch > len(batches):
        raise ValueError(f"--batch must be between 1 and {len(batches)} for this tatort.")

    username, password = require_credentials()
    selected_batch = batches[args.batch - 1]
    sample_tile = selected_batch[0]
    verify_asset_access(sample_tile, username, password)

    cache_dir = Path(args.cache_dir) if args.cache_dir else default_cache_dir(args.tatort, args.kommun)
    cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"Tatort: {args.tatort} ({args.kommun})")
    print(f"Tatortskod: {tatort.iloc[0]['tatortskod']}")
    print(f"Collections: {', '.join(collections)}")
    print(f"Cache directory: {cache_dir}")
    print(f"Selected batch {args.batch}/{len(batches)}: {describe_batch(selected_batch)}")

    total_batch_bytes = sum(tile.size_bytes for tile in selected_batch)
    current_cache_bytes = cache_usage_bytes(cache_dir)
    if current_cache_bytes + total_batch_bytes > args.cache_budget_mb * 1024 * 1024:
        raise RuntimeError(
            "Selected batch would exceed the configured cache budget in the target directory. "
            f"Current files: {format_mb(current_cache_bytes)}, batch: {format_mb(total_batch_bytes)}."
        )

    download_batch_tiles(selected_batch, cache_dir, username, password)
    final_cache_bytes = cache_usage_bytes(cache_dir)
    print(f"Finished batch download. Cache usage: {format_mb(final_cache_bytes)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
