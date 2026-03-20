#!/usr/bin/env python3

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from summarize_lund_altitude import MetricsAccumulator, TileMetrics, apply_tile_metrics


class StateStore:
    def __init__(self, db_path: Path, chunk_root: Path) -> None:
        self.db_path = db_path
        self.chunk_root = chunk_root
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.chunk_root.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self._initialize_schema()

    def close(self) -> None:
        self.connection.close()

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS city_runs (
                tatort TEXT NOT NULL,
                kommun TEXT NOT NULL,
                tatortskod TEXT NOT NULL,
                collections_json TEXT NOT NULL,
                total_tiles INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                planned_batches INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                error_message TEXT,
                min_altitude_m REAL,
                max_altitude_m REAL,
                altitude_range_m REAL,
                min_x_3006 REAL,
                min_y_3006 REAL,
                max_x_3006 REAL,
                max_y_3006 REAL,
                relief_q05_m REAL,
                relief_q95_m REAL,
                normalized_relief REAL,
                rms_slope_deg REAL,
                hilliness_score REAL,
                tiles_scanned INTEGER,
                tiles_used INTEGER,
                PRIMARY KEY (tatort, kommun)
            );

            CREATE TABLE IF NOT EXISTS city_tiles (
                tatort TEXT NOT NULL,
                kommun TEXT NOT NULL,
                item_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                href TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                batch_index INTEGER NOT NULL,
                batch_position INTEGER NOT NULL,
                local_filename TEXT,
                download_status TEXT NOT NULL DEFAULT 'planned',
                process_status TEXT NOT NULL DEFAULT 'planned',
                elevation_chunk_path TEXT,
                local_min REAL,
                local_max REAL,
                min_x_3006 REAL,
                min_y_3006 REAL,
                max_x_3006 REAL,
                max_y_3006 REAL,
                slope_sum_squares REAL,
                slope_count INTEGER,
                PRIMARY KEY (tatort, kommun, item_id)
            );
            """
        )
        self.connection.commit()

    def register_city_plan(self, plan, tatortskod: str) -> None:
        self.connection.execute(
            """
            INSERT INTO city_runs (
                tatort, kommun, tatortskod, collections_json, total_tiles, total_bytes, planned_batches, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT status FROM city_runs WHERE tatort = ? AND kommun = ?), 'planned'))
            ON CONFLICT(tatort, kommun) DO UPDATE SET
                tatortskod = excluded.tatortskod,
                collections_json = excluded.collections_json,
                total_tiles = excluded.total_tiles,
                total_bytes = excluded.total_bytes,
                planned_batches = excluded.planned_batches
            """,
            (
                plan.tatort,
                plan.kommun,
                tatortskod,
                json.dumps(plan.collections),
                len(plan.tiles),
                plan.total_bytes,
                len(plan.batches),
                plan.tatort,
                plan.kommun,
            ),
        )
        for batch_index, batch in enumerate(plan.batches, start=1):
            for batch_position, tile in enumerate(batch, start=1):
                self.connection.execute(
                    """
                    INSERT INTO city_tiles (
                        tatort, kommun, item_id, collection_id, href, size_bytes, batch_index, batch_position
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tatort, kommun, item_id) DO UPDATE SET
                        collection_id = excluded.collection_id,
                        href = excluded.href,
                        size_bytes = excluded.size_bytes,
                        batch_index = excluded.batch_index,
                        batch_position = excluded.batch_position
                    """,
                    (
                        plan.tatort,
                        plan.kommun,
                        tile.item_id,
                        tile.collection_id,
                        tile.href,
                        tile.size_bytes,
                        batch_index,
                        batch_position,
                    ),
                )
        self.connection.commit()

    def mark_city_status(self, tatort: str, kommun: str, status: str, error_message: str | None = None) -> None:
        self.connection.execute(
            "UPDATE city_runs SET status = ?, error_message = ? WHERE tatort = ? AND kommun = ?",
            (status, error_message, tatort, kommun),
        )
        self.connection.commit()

    def store_final_summary(self, summary: dict[str, Any]) -> None:
        min_coord = summary.get("min_coord_3006")
        max_coord = summary.get("max_coord_3006")
        self.connection.execute(
            """
            UPDATE city_runs
            SET status = 'completed',
                error_message = NULL,
                min_altitude_m = ?,
                max_altitude_m = ?,
                altitude_range_m = ?,
                min_x_3006 = ?,
                min_y_3006 = ?,
                max_x_3006 = ?,
                max_y_3006 = ?,
                relief_q05_m = ?,
                relief_q95_m = ?,
                normalized_relief = ?,
                rms_slope_deg = ?,
                hilliness_score = ?,
                tiles_scanned = ?,
                tiles_used = ?
            WHERE tatort = ? AND kommun = ?
            """,
            (
                summary["min_altitude_m"],
                summary["max_altitude_m"],
                summary["altitude_range_m"],
                None if min_coord is None else min_coord[0],
                None if min_coord is None else min_coord[1],
                None if max_coord is None else max_coord[0],
                None if max_coord is None else max_coord[1],
                summary["relief_q05_m"],
                summary["relief_q95_m"],
                summary["normalized_relief"],
                summary["rms_slope_deg"],
                summary["hilliness_score"],
                summary["tiles_scanned"],
                summary["tiles_used"],
                summary["tatort"],
                summary["kommun"],
            ),
        )
        self.connection.commit()

    def final_summary_for_city(self, tatort: str, kommun: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM city_runs WHERE tatort = ? AND kommun = ? AND status = 'completed'",
            (tatort, kommun),
        ).fetchone()
        if row is None:
            return None
        collections = json.loads(row["collections_json"])
        return {
            "tatort": row["tatort"],
            "kommun": row["kommun"],
            "tatortskod": row["tatortskod"],
            "collections": ", ".join(collections),
            "batches": row["planned_batches"],
            "total_raw_size_mb": row["total_bytes"] / 1024 / 1024,
            "tiles_scanned": row["tiles_scanned"],
            "tiles_used": row["tiles_used"],
            "min_altitude_m": row["min_altitude_m"],
            "max_altitude_m": row["max_altitude_m"],
            "min_coord_3006": self._coord_from_row(row, "min"),
            "max_coord_3006": self._coord_from_row(row, "max"),
            "altitude_range_m": row["altitude_range_m"],
            "relief_q05_m": row["relief_q05_m"],
            "relief_q95_m": row["relief_q95_m"],
            "normalized_relief": row["normalized_relief"],
            "rms_slope_deg": row["rms_slope_deg"],
            "hilliness_score": row["hilliness_score"],
        }

    def plan_metadata_for_city(self, tatort: str, kommun: str) -> dict[str, Any] | None:
        run_row = self.connection.execute(
            "SELECT * FROM city_runs WHERE tatort = ? AND kommun = ?",
            (tatort, kommun),
        ).fetchone()
        if run_row is None:
            return None
        tile_rows = self.connection.execute(
            """
            SELECT *
            FROM city_tiles
            WHERE tatort = ? AND kommun = ?
            ORDER BY batch_index, batch_position
            """,
            (tatort, kommun),
        ).fetchall()
        if not tile_rows:
            return None
        return {
            "tatort": run_row["tatort"],
            "kommun": run_row["kommun"],
            "tatortskod": run_row["tatortskod"],
            "collections": json.loads(run_row["collections_json"]),
            "total_tiles": run_row["total_tiles"],
            "total_bytes": run_row["total_bytes"],
            "tile_rows": tile_rows,
        }

    def tile_row_map(self, tatort: str, kommun: str) -> dict[str, sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM city_tiles WHERE tatort = ? AND kommun = ?",
            (tatort, kommun),
        ).fetchall()
        return {row["item_id"]: row for row in rows}

    def mark_tile_downloaded(self, tatort: str, kommun: str, item_id: str, local_filename: str) -> None:
        self.connection.execute(
            """
            UPDATE city_tiles
            SET download_status = 'downloaded',
                local_filename = ?
            WHERE tatort = ? AND kommun = ? AND item_id = ?
            """,
            (local_filename, tatort, kommun, item_id),
        )
        self.connection.commit()

    def store_tile_metrics(
        self,
        tatort: str,
        kommun: str,
        item_id: str,
        tile_metrics: TileMetrics,
    ) -> None:
        chunk_path = self.chunk_root / tatort.lower().replace(" ", "_") / f"{item_id}.npy"
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(chunk_path, tile_metrics.elevation_values)
        relative_chunk_path = chunk_path.relative_to(self.chunk_root)
        self.connection.execute(
            """
            UPDATE city_tiles
            SET process_status = 'processed',
                elevation_chunk_path = ?,
                local_min = ?,
                local_max = ?,
                min_x_3006 = ?,
                min_y_3006 = ?,
                max_x_3006 = ?,
                max_y_3006 = ?,
                slope_sum_squares = ?,
                slope_count = ?
            WHERE tatort = ? AND kommun = ? AND item_id = ?
            """,
            (
                str(relative_chunk_path),
                tile_metrics.local_min,
                tile_metrics.local_max,
                tile_metrics.min_coord_3006[0],
                tile_metrics.min_coord_3006[1],
                tile_metrics.max_coord_3006[0],
                tile_metrics.max_coord_3006[1],
                tile_metrics.slope_sum_squares,
                tile_metrics.slope_count,
                tatort,
                kommun,
                item_id,
            ),
        )
        self.connection.commit()

    def mark_tile_processed_empty(self, tatort: str, kommun: str, item_id: str) -> None:
        self.connection.execute(
            """
            UPDATE city_tiles
            SET process_status = 'processed_empty'
            WHERE tatort = ? AND kommun = ? AND item_id = ?
            """,
            (tatort, kommun, item_id),
        )
        self.connection.commit()

    def resume_accumulator(self, tatort: str, kommun: str) -> MetricsAccumulator:
        metrics = MetricsAccumulator()
        empty_count = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM city_tiles
            WHERE tatort = ? AND kommun = ? AND process_status = 'processed_empty'
            """,
            (tatort, kommun),
        ).fetchone()[0]
        metrics.tiles_scanned = int(empty_count)
        rows = self.connection.execute(
            """
            SELECT *
            FROM city_tiles
            WHERE tatort = ? AND kommun = ? AND process_status = 'processed'
            ORDER BY batch_index, batch_position
            """,
            (tatort, kommun),
        ).fetchall()
        for row in rows:
            chunk_path = self.chunk_root / row["elevation_chunk_path"]
            values = np.load(chunk_path)
            tile_metrics = TileMetrics(
                elevation_values=values,
                local_min=row["local_min"],
                local_max=row["local_max"],
                min_coord_3006=(row["min_x_3006"], row["min_y_3006"]),
                max_coord_3006=(row["max_x_3006"], row["max_y_3006"]),
                slope_sum_squares=row["slope_sum_squares"] or 0.0,
                slope_count=row["slope_count"] or 0,
            )
            metrics.tiles_scanned += 1
            apply_tile_metrics(metrics, tile_metrics)
        return metrics

    @staticmethod
    def _coord_from_row(row: sqlite3.Row, prefix: str) -> tuple[float, float] | None:
        x_value = row[f"{prefix}_x_3006"]
        y_value = row[f"{prefix}_y_3006"]
        if x_value is None or y_value is None:
            return None
        return (float(x_value), float(y_value))
