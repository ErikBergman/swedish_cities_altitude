#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from urllib.error import HTTPError

import geopandas as gpd
from shapely.geometry import box


API_ROOT = "https://api.lantmateriet.se/stac-hojd/v1"
GPKG_PATH = "Tatorter_2023.gpkg"
GPKG_LAYER = "Tatorter_2023"
DEFAULT_TATORT = "Lund"
DEFAULT_KOMMUN = "Lund"
DEFAULT_CACHE_BUDGET_MB = 1024


@dataclass
class TileInfo:
    item_id: str
    collection_id: str
    href: str
    size_bytes: int
    bbox_3006: tuple[float, float, float, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check authenticated access to Lantmateriet DEM tiles and plan a "
            "temporary-cache batch strategy for one tatort."
        )
    )
    parser.add_argument("--tatort", default=DEFAULT_TATORT, help="Tatort name to inspect.")
    parser.add_argument("--kommun", default=DEFAULT_KOMMUN, help="Kommun name to disambiguate the tatort.")
    parser.add_argument(
        "--cache-budget-mb",
        type=int,
        default=DEFAULT_CACHE_BUDGET_MB,
        help="Maximum temporary cache size to assume when planning download batches.",
    )
    return parser.parse_args()


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url) as response:
        return json.load(response)


@lru_cache(maxsize=1)
def _load_all_tatorter_cached() -> gpd.GeoDataFrame:
    return gpd.read_file(GPKG_PATH, layer=GPKG_LAYER).to_crs(3006)


def load_all_tatorter() -> gpd.GeoDataFrame:
    return _load_all_tatorter_cached().copy()


def load_tatort(tatort_name: str, kommun_name: str) -> gpd.GeoDataFrame:
    cities = _load_all_tatorter_cached()
    tatort = cities.loc[
        (cities["tatort"] == tatort_name) & (cities["kommunnamn"] == kommun_name)
    ].copy()
    if len(tatort) != 1:
        raise ValueError(
            f"Expected exactly one tatort for {tatort_name}/{kommun_name}, found {len(tatort)}."
        )
    return tatort


def find_covering_collections(tatort: gpd.GeoDataFrame) -> list[str]:
    collections = fetch_json(f"{API_ROOT}/collections")["collections"]
    tatort_bounds = box(*tatort.total_bounds)
    collection_ids: list[str] = []

    for collection in collections:
        bbox_4326 = collection["extent"]["spatial"]["bbox"][0]
        bbox_3006 = gpd.GeoSeries([box(*bbox_4326)], crs=4326).to_crs(3006).iloc[0]
        if bbox_3006.intersects(tatort_bounds):
            collection_ids.append(collection["id"])

    if not collection_ids:
        raise ValueError("No STAC collections intersect the target tatort.")

    return collection_ids


def fetch_intersecting_tiles(tatort: gpd.GeoDataFrame, collection_ids: list[str]) -> list[TileInfo]:
    geometry = tatort.geometry.iloc[0]
    tiles: list[TileInfo] = []

    for collection_id in collection_ids:
        url = f"{API_ROOT}/collections/{collection_id}/items?limit=1000"
        while url:
            page = fetch_json(url)
            for item in page.get("features", []):
                asset = item["assets"]["data"]
                bbox = asset.get("proj:bbox") or item["properties"]["proj:bbox"]
                tile_geometry = box(*bbox)
                if not geometry.intersects(tile_geometry):
                    continue

                tiles.append(
                    TileInfo(
                        item_id=item["id"],
                        collection_id=collection_id,
                        href=asset["href"],
                        size_bytes=int(asset["file:size"]),
                        bbox_3006=tuple(bbox),
                    )
                )
            url = next((link["href"] for link in page.get("links", []) if link.get("rel") == "next"), None)

    if not tiles:
        raise ValueError("No DEM tiles intersect the target tatort.")

    return sorted(tiles, key=lambda tile: (tile.bbox_3006[1], tile.bbox_3006[0]))


def format_mb(size_bytes: int) -> str:
    return f"{size_bytes / 1024 / 1024:.2f} MB"


def require_credentials() -> tuple[str, str]:
    username = os.getenv("LANTMATERIET_USERNAME")
    password = os.getenv("LANTMATERIET_PASSWORD")
    if not username or not password:
        missing = []
        if not username:
            missing.append("LANTMATERIET_USERNAME")
        if not password:
            missing.append("LANTMATERIET_PASSWORD")
        names = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variable(s): {names}")
    return username, password


def verify_asset_access(sample_tile: TileInfo, username: str, password: str) -> tuple[int, int]:
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, sample_tile.href, username, password)
    auth_handler = urllib.request.HTTPBasicAuthHandler(password_manager)
    opener = urllib.request.build_opener(auth_handler)
    request = urllib.request.Request(sample_tile.href, headers={"Range": "bytes=0-0"}, method="GET")

    try:
        with opener.open(request, timeout=30) as response:
            status_code = response.status
            content_length = int(response.headers.get("Content-Length", "0"))
    except HTTPError as error:
        raise RuntimeError(
            f"Authenticated test request failed for {sample_tile.href} with status {error.code}."
        ) from error

    if status_code not in (200, 206):
        raise RuntimeError(
            f"Authenticated test request failed for {sample_tile.href} with status {status_code}."
        )

    return status_code, content_length


def plan_batches(tiles: list[TileInfo], cache_budget_bytes: int) -> list[list[TileInfo]]:
    if cache_budget_bytes <= 0:
        raise ValueError("--cache-budget-mb must be greater than 0.")

    if any(tile.size_bytes > cache_budget_bytes for tile in tiles):
        largest = max(tile.size_bytes for tile in tiles)
        raise RuntimeError(
            "At least one DEM tile is larger than the cache budget. "
            f"Largest tile: {format_mb(largest)}."
        )

    batches: list[list[TileInfo]] = []
    current_batch: list[TileInfo] = []
    current_size = 0

    for tile in tiles:
        if current_size + tile.size_bytes > cache_budget_bytes and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_size = 0

        current_batch.append(tile)
        current_size += tile.size_bytes

    if current_batch:
        batches.append(current_batch)

    return batches


def describe_batch(batch: list[TileInfo]) -> str:
    size_bytes = sum(tile.size_bytes for tile in batch)
    first_id = batch[0].item_id
    last_id = batch[-1].item_id
    return f"{len(batch)} tiles, {format_mb(size_bytes)}, ids {first_id} -> {last_id}"


def main() -> int:
    args = parse_args()
    tatort = load_tatort(args.tatort, args.kommun)
    collections = find_covering_collections(tatort)
    tiles = fetch_intersecting_tiles(tatort, collections)

    total_bytes = sum(tile.size_bytes for tile in tiles)
    cache_budget_bytes = args.cache_budget_mb * 1024 * 1024
    batches = plan_batches(tiles, cache_budget_bytes)

    username, password = require_credentials()
    sample_tile = tiles[0]
    status_code, content_length = verify_asset_access(sample_tile, username, password)

    print(f"Tatort: {args.tatort} ({args.kommun})")
    print(f"Tatortskod: {tatort.iloc[0]['tatortskod']}")
    print(f"Collections: {', '.join(collections)}")
    print(f"Intersecting tiles: {len(tiles)}")
    print(f"Total raw size: {format_mb(total_bytes)}")
    print(f"Temporary cache budget: {args.cache_budget_mb} MB")
    print(f"Planned batches: {len(batches)}")
    for index, batch in enumerate(batches, start=1):
        print(f"  Batch {index}: {describe_batch(batch)}")

    print(f"Sample tile URL: {sample_tile.href}")
    print(f"Authenticated test status: {status_code}")
    if content_length:
        print(f"Authenticated response content-length: {content_length}")

    if len(batches) == 1:
        print("This tatort fits within the temporary cache budget as a single working set.")
    else:
        print(
            "This tatort does not need to be stored permanently. "
            "It can be processed in multiple temporary batches."
        )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
