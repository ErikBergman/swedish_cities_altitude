#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError

import geopandas as gpd
from shapely.geometry import box


API_ROOT = "https://api.lantmateriet.se/stac-hojd/v1"
GPKG_PATH = "Tatorter_2023.gpkg"
GPKG_LAYER = "Tatorter_2023"
TARGET_TATORT = "Lund"
TARGET_KOMMUN = "Lund"
SIZE_LIMIT_MB = 50


@dataclass
class TileInfo:
    item_id: str
    collection_id: str
    href: str
    size_bytes: int
    bbox_3006: tuple[float, float, float, float]


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url) as response:
        return json.load(response)


def load_lund_tatort() -> gpd.GeoDataFrame:
    cities = gpd.read_file(GPKG_PATH, layer=GPKG_LAYER).to_crs(3006)
    lund = cities.loc[
        (cities["tatort"] == TARGET_TATORT) & (cities["kommunnamn"] == TARGET_KOMMUN)
    ].copy()
    if len(lund) != 1:
        raise ValueError(
            f"Expected exactly one tatort for {TARGET_TATORT}/{TARGET_KOMMUN}, found {len(lund)}."
        )
    return lund


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

    return tiles


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
    request = urllib.request.Request(
        sample_tile.href,
        headers={"Range": "bytes=0-0"},
        method="GET",
    )
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


def main() -> int:
    lund = load_lund_tatort()
    collections = find_covering_collections(lund)
    tiles = fetch_intersecting_tiles(lund, collections)
    total_bytes = sum(tile.size_bytes for tile in tiles)
    sample_tile = tiles[0]

    username, password = require_credentials()
    status_code, content_length = verify_asset_access(sample_tile, username, password)

    print(f"Tatort: {TARGET_TATORT} ({TARGET_KOMMUN})")
    print(f"Tatortskod: {lund.iloc[0]['tatortskod']}")
    print(f"Collections: {', '.join(collections)}")
    print(f"Intersecting tiles: {len(tiles)}")
    print(f"Estimated dataset size: {format_mb(total_bytes)}")
    print(f"Sample tile: {sample_tile.item_id}")
    print(f"Sample tile URL: {sample_tile.href}")
    print(f"Authenticated test status: {status_code}")
    if content_length:
        print(f"Authenticated response content-length: {content_length}")

    if total_bytes > SIZE_LIMIT_MB * 1024 * 1024:
        print(
            f"Download blocked: estimated size exceeds {SIZE_LIMIT_MB} MB. "
            "Approval is required before downloading DEM tiles."
        )
        return 2

    print("Dataset is within the automatic download limit.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
