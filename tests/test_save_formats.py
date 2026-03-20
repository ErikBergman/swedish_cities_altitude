from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import csv

from check_lund_access import TileInfo
from compare_cities import write_results_csv
from state_store import StateStore


def sample_result() -> dict[str, float | int | str | tuple[float, float]]:
    return {
        "tatort": "Lund",
        "kommun": "Lund",
        "tatortskod": "1281TC105",
        "collections": "mhm-61_3",
        "batches": 1,
        "total_raw_size_mb": 98.19,
        "tiles_scanned": 12,
        "tiles_used": 12,
        "min_altitude_m": 5.0,
        "max_altitude_m": 86.0,
        "min_coord_3006": (385000.0, 6175000.0),
        "max_coord_3006": (386000.0, 6176000.0),
        "altitude_range_m": 81.0,
        "relief_q05_m": 10.0,
        "relief_q95_m": 75.0,
        "normalized_relief": 12.3456,
        "rms_slope_deg": 4.321,
        "hilliness_score": 53.348,
    }


def test_write_results_csv_creates_expected_columns(tmp_path: Path) -> None:
    output_path = tmp_path / "results.csv"
    write_results_csv(output_path, [sample_result()])

    assert output_path.exists()
    with output_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    row = rows[0]
    assert row["rank"] == "1"
    assert row["tatort"] == "Lund"
    assert row["kommun"] == "Lund"
    assert row["collections"] == "mhm-61_3"
    assert row["min_altitude_m"] == "5.00"
    assert row["max_altitude_m"] == "86.00"
    assert row["hilliness_score"] == "53.3480"
    assert row["min_coord_lat_lon"].startswith("(")
    assert row["max_coord_lat_lon"].startswith("(")


def test_state_store_round_trips_plan_and_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    chunk_root = tmp_path / "chunks"
    state = StateStore(db_path, chunk_root)
    try:
        plan = SimpleNamespace(
            tatort="Lund",
            kommun="Lund",
            tatortskod="1281TC105",
            collections=["mhm-61_3"],
            tiles=[
                TileInfo(
                    item_id="617_38_5050",
                    collection_id="mhm-61_3",
                    href="https://example.com/617_38_5050.tif",
                    size_bytes=123,
                    bbox_3006=(1.0, 2.0, 3.0, 4.0),
                )
            ],
            batches=[["placeholder"]],
            total_bytes=123,
        )
        # register_city_plan only reads lengths from batches and concrete tile objects from tiles.
        plan.batches = [[plan.tiles[0]]]

        state.register_city_plan(plan, plan.tatortskod)
        state.store_final_summary(sample_result())

        plan_metadata = state.plan_metadata_for_city("Lund", "Lund")
        assert plan_metadata is not None
        assert plan_metadata["tatortskod"] == "1281TC105"
        assert plan_metadata["collections"] == ["mhm-61_3"]
        assert len(plan_metadata["tile_rows"]) == 1
        assert plan_metadata["tile_rows"][0]["href"] == "https://example.com/617_38_5050.tif"

        summary = state.final_summary_for_city("Lund", "Lund")
        assert summary is not None
        assert summary["tatort"] == "Lund"
        assert summary["kommun"] == "Lund"
        assert summary["collections"] == "mhm-61_3"
        assert summary["tiles_used"] == 12
        assert summary["min_altitude_m"] == 5.0
        assert summary["max_altitude_m"] == 86.0
    finally:
        state.close()
