"""Catalog persistence. Services deal in domain records, not SQL strings."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from typing import Any

from ..errors import VClipError
from ..util import json_dumps, json_loads, stable_id, utc_now
from .connection import Database
from .records import (
    CandidateRecord,
    GeneratedOccurrenceRecord,
    ProjectFamilyRecord,
    ShootSessionRecord,
    SourceEventRecord,
    SourceMediaRecord,
    SourceProjectRecord,
    StockifySnapshot,
)


def _insert(connection: sqlite3.Connection, table: str, values: Mapping[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def _upsert(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, Any],
    conflict_columns: Iterable[str],
    update_columns: Iterable[str],
) -> None:
    columns = list(values)
    placeholders = ", ".join("?" for _ in columns)
    conflict = ", ".join(conflict_columns)
    update = ", ".join(f"{column}=excluded.{column}" for column in update_columns)
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET {update}",
        tuple(values[column] for column in columns),
    )


class CatalogRepository:
    """Read and write the lifecycle state shared by all three commands."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # Stockify lifecycle

    def start_stockify_run(
        self,
        *,
        run_id: str,
        source_xml_path: str,
        source_xml_sha256: str,
        source_fcpxml_version: str | None,
        output_xml_path: str,
        report_path: str,
        manifest_path: str | None,
        pipeline_version: str,
        options: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            _insert(
                connection,
                "stockify_runs",
                {
                    "id": run_id,
                    "source_xml_path": source_xml_path,
                    "source_xml_sha256": source_xml_sha256,
                    "source_fcpxml_version": source_fcpxml_version,
                    "output_xml_path": output_xml_path,
                    "report_path": report_path,
                    "manifest_path": manifest_path,
                    "pipeline_version": pipeline_version,
                    "status": "running",
                    "options_json": json_dumps(options),
                    "started_at": now,
                    "completed_at": None,
                    "error_text": None,
                },
            )

    def complete_stockify_run(self, run_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE stockify_runs SET status='complete', completed_at=?, error_text=NULL WHERE id=?",
                (utc_now(), run_id),
            )

    def mark_library_processed(
        self,
        *,
        library_name: str,
        library_path: str,
        stockify_run_id: str,
    ) -> None:
        """Record that Stockify successfully processed a Final Cut library.

        Re-running the same library path is idempotent: first-run provenance is
        preserved while last-run identity/timestamp advance.
        """
        now = utc_now()
        library_id = stable_id("LIBRARY", library_path)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM processed_libraries WHERE library_path=?",
                (library_path,),
            ).fetchone()
            if existing is None:
                _insert(
                    connection,
                    "processed_libraries",
                    {
                        "id": library_id,
                        "library_name": library_name,
                        "library_path": library_path,
                        "first_stockify_run_id": stockify_run_id,
                        "last_stockify_run_id": stockify_run_id,
                        "first_processed_at": now,
                        "last_processed_at": now,
                    },
                )
                return
            connection.execute(
                """
                UPDATE processed_libraries
                SET library_name=?,
                    last_stockify_run_id=?,
                    last_processed_at=?
                WHERE library_path=?
                """,
                (library_name, stockify_run_id, now, library_path),
            )

    def processed_libraries(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM processed_libraries
                ORDER BY lower(library_name), library_path
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def libraries_for_stockify_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return processed FCP libraries associated with a Stockify run."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM processed_libraries
                WHERE first_stockify_run_id=? OR last_stockify_run_id=?
                ORDER BY lower(library_name), library_path
                """,
                (run_id, run_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def source_media_for_stems(
        self,
        run_id: str,
        stems: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        """Load source_media rows keyed by normalized_stem for a run."""
        normalized = sorted({str(stem).casefold() for stem in stems if stem})
        if not normalized:
            return {}
        placeholders = ", ".join("?" for _ in normalized)
        query = f"""
            SELECT *
            FROM source_media
            WHERE run_id=?
              AND lower(normalized_stem) IN ({placeholders})
        """
        with self.database.connect() as connection:
            rows = connection.execute(query, [run_id, *normalized]).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["location"] = json_loads(item.pop("location_json"), {})
            stem = str(item.get("normalized_stem") or "").casefold()
            if stem:
                result[stem] = item
        return result

    def fail_stockify_run(self, run_id: str, error_text: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE stockify_runs SET status='failed', completed_at=?, error_text=? WHERE id=?",
                (utc_now(), error_text, run_id),
            )

    def persist_stockify_snapshot(self, snapshot: StockifySnapshot) -> None:
        """Persist one complete analysis atomically."""
        now = utc_now()
        with self.database.transaction() as connection:
            for record in snapshot.events:
                self._insert_event(connection, record)
            for record in snapshot.sessions:
                self._insert_session(connection, record, now)
            for record in snapshot.families:
                self._insert_family(connection, record, now)
            for record in snapshot.projects:
                self._insert_project(connection, record, now)
            for record in snapshot.media:
                self._insert_media(connection, record, now)
            for record in snapshot.candidates:
                self._insert_candidate(connection, record, now)
            for record in snapshot.occurrences:
                self._insert_occurrence(connection, record)

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, record: SourceEventRecord) -> None:
        _insert(connection, "source_events", asdict(record))

    @staticmethod
    def _insert_session(
        connection: sqlite3.Connection,
        record: ShootSessionRecord,
        now: str,
    ) -> None:
        values = asdict(record)
        values["location_json"] = json_dumps(values.pop("location"))
        values["capture_json"] = json_dumps(values.pop("capture"))
        values["created_at"] = now
        values["updated_at"] = now
        _insert(connection, "shoot_sessions", values)

    @staticmethod
    def _insert_family(
        connection: sqlite3.Connection,
        record: ProjectFamilyRecord,
        now: str,
    ) -> None:
        values = asdict(record)
        values["similarity_json"] = json_dumps(values.pop("similarity"))
        values["created_at"] = now
        _insert(connection, "source_project_families", values)

    @staticmethod
    def _insert_project(
        connection: sqlite3.Connection,
        record: SourceProjectRecord,
        now: str,
    ) -> None:
        values = asdict(record)
        values["created_at"] = now
        values["updated_at"] = now
        _insert(connection, "source_projects", values)

    @staticmethod
    def _insert_media(
        connection: sqlite3.Connection,
        record: SourceMediaRecord,
        now: str,
    ) -> None:
        values = asdict(record)
        values["srt_match_ambiguous"] = int(values["srt_match_ambiguous"])
        for key in ("srt_has_position", "srt_has_altitude", "srt_has_orientation"):
            value = values[key]
            values[key] = None if value is None else int(value)
        values["location_json"] = json_dumps(values.pop("location"))
        values["created_at"] = now
        values["updated_at"] = now
        _insert(connection, "source_media", values)

    @staticmethod
    def _insert_candidate(
        connection: sqlite3.Connection,
        record: CandidateRecord,
        now: str,
    ) -> None:
        values = asdict(record)
        for source_key, target_key in (
            ("srt_reasons", "srt_reasons_json"),
            ("visual_reasons", "visual_reasons_json"),
            ("visual_metrics", "visual_metrics_json"),
            ("location", "location_json"),
            ("capture_time", "capture_time_json"),
            ("time_of_day", "time_of_day_json"),
            ("weather", "weather_json"),
            ("creative_effects", "creative_effects_json"),
        ):
            values[target_key] = json_dumps(values.pop(source_key))
        values.update(
            {
                "final_start": None,
                "final_duration": None,
                "final_duration_seconds": None,
                "review_status": (
                    "pending" if record.eligibility_status == "accepted" else "not_applicable"
                ),
                "manually_modified": 0,
                "manual_change_json": "{}",
                "final_effect_signature": None,
                "export_status": (
                    "pending" if record.eligibility_status == "accepted" else "not_applicable"
                ),
                "created_at": now,
                "updated_at": now,
            }
        )
        _insert(connection, "stock_candidates", values)

    @staticmethod
    def _insert_occurrence(
        connection: sqlite3.Connection,
        record: GeneratedOccurrenceRecord,
    ) -> None:
        _insert(connection, "generated_occurrences", asdict(record))

    # Common reads

    def get_stockify_run(self, run_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stockify_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise VClipError(f"Unknown Stockify run: {run_id}")
        result = dict(row)
        result["options"] = json_loads(result.pop("options_json"), {})
        return result

    def latest_stockify_run(self, *, completed_only: bool = True) -> dict[str, Any]:
        runs = self.list_stockify_runs(completed_only=completed_only)
        if not runs:
            raise VClipError("The database does not contain a Stockify run.")
        return runs[0]

    def list_stockify_runs(self, *, completed_only: bool = True) -> list[dict[str, Any]]:
        """Return Stockify runs newest-first."""
        where = "WHERE status='complete'" if completed_only else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT id FROM stockify_runs {where} "
                "ORDER BY COALESCE(completed_at, started_at) DESC, id DESC"
            ).fetchall()
        return [self.get_stockify_run(str(row["id"])) for row in rows]

    def latest_reconciled_stockify_run(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT s.*
                FROM stockify_runs s
                JOIN reconcile_runs r ON r.stockify_run_id=s.id
                WHERE r.status IN ('complete','complete_with_conflicts')
                ORDER BY r.completed_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            raise VClipError("The database does not contain a reconciled Stockify run.")
        return self.get_stockify_run(str(row["id"]))

    def candidates_for_run(
        self,
        run_id: str,
        *,
        accepted_only: bool = False,
        approved_only: bool = False,
    ) -> list[dict[str, Any]]:
        conditions = ["c.run_id=?"]
        parameters: list[Any] = [run_id]
        if accepted_only:
            conditions.append("c.eligibility_status='accepted'")
        if approved_only:
            conditions.append("c.review_status='approved'")
        query = f"""
            SELECT
                c.*,
                p.source_name AS source_project_name,
                p.source_event_id,
                p.source_index AS source_project_index,
                e.source_name AS source_event_name,
                s.capture_date AS session_capture_date,
                s.captured_at_local AS session_captured_at_local,
                s.timezone AS session_timezone,
                s.center_lat AS session_center_lat,
                s.center_lon AS session_center_lon,
                s.country AS session_country,
                s.state AS session_state,
                s.city AS session_city,
                s.neighborhood AS session_neighborhood,
                s.poi AS session_poi,
                s.public_label AS session_public_label,
                s.time_of_day AS session_time_of_day,
                s.generated_event_name AS session_event_name,
                s.generated_base_label AS session_base_label,
                m.original_filename AS source_filename,
                m.media_path AS source_media_path,
                m.srt_path AS source_srt_path,
                m.normalized_stem AS source_normalized_stem,
                m.srt_match_method AS source_srt_match_method,
                m.srt_match_ambiguous AS source_srt_match_ambiguous,
                m.srt_has_position AS source_srt_has_position,
                m.fps AS source_fps
            FROM stock_candidates c
            JOIN source_projects p ON p.id=c.source_project_id
            JOIN source_events e ON e.id=p.source_event_id
            LEFT JOIN shoot_sessions s ON s.id=c.session_id
            LEFT JOIN source_media m ON m.id=c.source_media_id
            WHERE {' AND '.join(conditions)}
            ORDER BY e.source_index, p.source_index, c.source_segment_index
        """
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._decode_candidate_row(dict(row)) for row in rows]

    def candidate_by_id(self, run_id: str, stock_clip_id: str) -> dict[str, Any]:
        rows = [
            row
            for row in self.candidates_for_run(run_id)
            if row["stock_clip_id"] == stock_clip_id
        ]
        if not rows:
            raise VClipError(f"Unknown stock candidate {stock_clip_id} in run {run_id}.")
        return rows[0]

    def candidates_by_ids(self, stock_clip_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Load accepted candidates by stock_clip_id across runs."""
        ids = sorted({str(value) for value in stock_clip_ids if value})
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        query = f"""
            SELECT
                c.*,
                p.source_name AS source_project_name,
                p.source_event_id,
                p.source_index AS source_project_index,
                e.source_name AS source_event_name,
                s.capture_date AS session_capture_date,
                s.captured_at_local AS session_captured_at_local,
                s.timezone AS session_timezone,
                s.country AS session_country,
                s.state AS session_state,
                s.city AS session_city,
                s.neighborhood AS session_neighborhood,
                s.public_label AS session_public_label,
                m.original_filename AS source_filename,
                m.media_path AS source_media_path,
                m.normalized_stem AS source_normalized_stem,
                m.fps AS source_fps
            FROM stock_candidates c
            JOIN source_projects p ON p.id=c.source_project_id
            JOIN source_events e ON e.id=p.source_event_id
            LEFT JOIN shoot_sessions s ON s.id=c.session_id
            LEFT JOIN source_media m ON m.id=c.source_media_id
            WHERE c.eligibility_status='accepted'
              AND c.stock_clip_id IN ({placeholders})
        """
        with self.database.connect() as connection:
            rows = connection.execute(query, ids).fetchall()
        return {
            str(row["stock_clip_id"]): self._decode_candidate_row(dict(row))
            for row in rows
        }

    def candidates_by_run_and_ids(
        self,
        pairs: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Load accepted candidates keyed by (run_id, stock_clip_id)."""
        unique_pairs = sorted(
            {
                (str(run_id), str(clip_id))
                for run_id, clip_id in pairs
                if run_id and clip_id
            }
        )
        if not unique_pairs:
            return {}
        placeholders = ", ".join("(?, ?)" for _ in unique_pairs)
        parameters: list[Any] = []
        for run_id, clip_id in unique_pairs:
            parameters.extend([run_id, clip_id])
        query = f"""
            SELECT
                c.*,
                p.source_name AS source_project_name,
                p.source_event_id,
                p.source_index AS source_project_index,
                e.source_name AS source_event_name,
                s.capture_date AS session_capture_date,
                s.captured_at_local AS session_captured_at_local,
                s.timezone AS session_timezone,
                s.country AS session_country,
                s.state AS session_state,
                s.city AS session_city,
                s.neighborhood AS session_neighborhood,
                s.public_label AS session_public_label,
                m.original_filename AS source_filename,
                m.media_path AS source_media_path,
                m.normalized_stem AS source_normalized_stem,
                m.fps AS source_fps
            FROM stock_candidates c
            JOIN source_projects p ON p.id=c.source_project_id
            JOIN source_events e ON e.id=p.source_event_id
            LEFT JOIN shoot_sessions s ON s.id=c.session_id
            LEFT JOIN source_media m ON m.id=c.source_media_id
            WHERE c.eligibility_status='accepted'
              AND (c.run_id, c.stock_clip_id) IN ({placeholders})
        """
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return {
            (str(row["run_id"]), str(row["stock_clip_id"])): self._decode_candidate_row(
                dict(row)
            )
            for row in rows
        }

    @staticmethod
    def _decode_candidate_row(row: dict[str, Any]) -> dict[str, Any]:
        for source_key, target_key, fallback in (
            ("srt_reasons_json", "srt_reasons", []),
            ("visual_reasons_json", "visual_reasons", []),
            ("visual_metrics_json", "visual_metrics", {}),
            ("location_json", "location", {}),
            ("capture_time_json", "capture_time", {}),
            ("time_of_day_json", "time_of_day", {}),
            ("weather_json", "weather", {}),
            ("creative_effects_json", "creative_effects", []),
            ("manual_change_json", "manual_change", {}),
        ):
            row[target_key] = json_loads(row.get(source_key), fallback)
        row["manually_modified"] = bool(row.get("manually_modified"))
        if "source_srt_match_ambiguous" in row:
            value = row.get("source_srt_match_ambiguous")
            row["source_srt_match_ambiguous"] = (
                None if value is None else bool(value)
            )
        if "source_srt_has_position" in row:
            value = row.get("source_srt_has_position")
            row["source_srt_has_position"] = None if value is None else bool(value)
        return row

    def projects_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*, e.source_name AS source_event_name,
                       s.generated_event_name AS session_event_name,
                       s.generated_base_label AS session_base_label
                FROM source_projects p
                JOIN source_events e ON e.id=p.source_event_id
                LEFT JOIN shoot_sessions s ON s.id=p.session_id
                WHERE p.run_id=?
                ORDER BY e.source_index, p.source_index
                """,
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["timeline_signature"] = json_loads(
                item.pop("timeline_signature_json", None),
                None,
            )
            result.append(item)
        return result

    def project_families_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM source_project_families
                WHERE run_id=?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["similarity"] = json_loads(item.pop("similarity_json"), {})
            result.append(item)
        return result

    def sessions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM shoot_sessions WHERE run_id=? ORDER BY generated_event_name",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["location"] = json_loads(item.pop("location_json"), {})
            item["capture"] = json_loads(item.pop("capture_json"), {})
            result.append(item)
        return result

    def apply_location_recovery(
        self,
        *,
        run_id: str,
        session_id: str,
        location: dict[str, Any],
        generated_event_name: str,
        generated_base_label: str,
        candidate_updates: list[dict[str, Any]],
        project_updates: list[dict[str, Any]],
    ) -> None:
        """Persist session-level location consensus and regenerated names."""
        now = utc_now()
        capture_patch = {
            key: location.get(key)
            for key in ("timezone",)
            if location.get(key) is not None
        }
        with self.database.transaction() as connection:
            session_row = connection.execute(
                "SELECT capture_json FROM shoot_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session_row is None:
                raise VClipError(f"Unknown shoot session: {session_id}")
            capture = json_loads(session_row["capture_json"], {})
            capture.update(capture_patch)
            if location.get("timezone"):
                capture["timezone"] = location["timezone"]
            connection.execute(
                """
                UPDATE shoot_sessions
                SET center_lat=?,
                    center_lon=?,
                    gps_radius_meters=?,
                    country=?,
                    state=?,
                    city=?,
                    neighborhood=?,
                    poi=?,
                    public_label=?,
                    location_confidence=?,
                    timezone=COALESCE(?, timezone),
                    generated_event_name=?,
                    generated_base_label=?,
                    location_json=?,
                    capture_json=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    location.get("center_lat"),
                    location.get("center_lon"),
                    location.get("radius_meters"),
                    location.get("country"),
                    location.get("state"),
                    location.get("city"),
                    location.get("neighborhood"),
                    location.get("poi"),
                    location.get("public_label"),
                    location.get("confidence"),
                    location.get("timezone"),
                    generated_event_name,
                    generated_base_label,
                    json_dumps(location),
                    json_dumps(capture),
                    now,
                    session_id,
                ),
            )
            for project in project_updates:
                connection.execute(
                    """
                    UPDATE source_projects
                    SET generated_event_name=?,
                        generated_project_label=?,
                        generated_compilation_name=?,
                        updated_at=?
                    WHERE id=? AND run_id=?
                    """,
                    (
                        project["generated_event_name"],
                        project["generated_project_label"],
                        project.get("generated_compilation_name"),
                        now,
                        project["source_project_id"],
                        run_id,
                    ),
                )
            for candidate in candidate_updates:
                connection.execute(
                    """
                    UPDATE stock_candidates
                    SET location_json=?,
                        generated_event_name=?,
                        generated_project_label=?,
                        generated_clip_project_name=?,
                        generated_compilation_name=?,
                        expected_export_basename=?,
                        updated_at=?
                    WHERE run_id=? AND stock_clip_id=?
                    """,
                    (
                        json_dumps(candidate["location"]),
                        candidate["generated_event_name"],
                        candidate["generated_project_label"],
                        candidate.get("generated_clip_project_name"),
                        candidate.get("generated_compilation_name"),
                        candidate.get("expected_export_basename"),
                        now,
                        run_id,
                        candidate["stock_clip_id"],
                    ),
                )
                clip_name = candidate.get("generated_clip_project_name")
                compilation_name = candidate.get("generated_compilation_name")
                if clip_name:
                    connection.execute(
                        """
                        UPDATE generated_occurrences
                        SET generated_event_name=?, generated_project_name=?
                        WHERE run_id=? AND stock_clip_id=? AND representation='individual'
                        """,
                        (
                            candidate["generated_event_name"],
                            clip_name,
                            run_id,
                            candidate["stock_clip_id"],
                        ),
                    )
                if compilation_name:
                    connection.execute(
                        """
                        UPDATE generated_occurrences
                        SET generated_event_name=?, generated_project_name=?
                        WHERE run_id=? AND stock_clip_id=? AND representation='compilation'
                        """,
                        (
                            candidate["generated_event_name"],
                            compilation_name,
                            run_id,
                            candidate["stock_clip_id"],
                        ),
                    )

    def update_session_event_name(self, session_id: str, generated_event_name: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE shoot_sessions SET generated_event_name=?, updated_at=? WHERE id=?",
                (generated_event_name, utc_now(), session_id),
            )

    def generated_occurrences(self, run_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM generated_occurrences WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # Reconcile lifecycle

    def start_reconcile_run(
        self,
        *,
        reconcile_id: str,
        stockify_run_id: str,
        reviewed_xml_path: str,
        reviewed_xml_sha256: str,
        authority: str,
        scope: str,
        report_path: str | None,
    ) -> None:
        with self.database.transaction() as connection:
            _insert(
                connection,
                "reconcile_runs",
                {
                    "id": reconcile_id,
                    "stockify_run_id": stockify_run_id,
                    "reviewed_xml_path": reviewed_xml_path,
                    "reviewed_xml_sha256": reviewed_xml_sha256,
                    "authority": authority,
                    "scope": scope,
                    "status": "running",
                    "started_at": utc_now(),
                    "completed_at": None,
                    "approved_count": 0,
                    "rejected_count": 0,
                    "modified_count": 0,
                    "conflict_count": 0,
                    "report_path": report_path,
                    "error_text": None,
                },
            )

    def apply_reconciliation(
        self,
        *,
        reconcile_id: str,
        stockify_run_id: str,
        decisions: list[dict[str, Any]],
        occurrences: list[dict[str, Any]],
        status: str,
    ) -> None:
        now = utc_now()
        counts = {
            "approved": sum(d["review_status"] == "approved" for d in decisions),
            "rejected": sum(d["review_status"] == "rejected" for d in decisions),
            "modified": sum(
                d["review_status"] == "approved" and bool(d.get("manually_modified"))
                for d in decisions
            ),
            "conflict": sum(d["review_status"] == "conflict" for d in decisions),
        }
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM review_occurrences WHERE reconcile_run_id=?",
                (reconcile_id,),
            )
            for occurrence in occurrences:
                _insert(
                    connection,
                    "review_occurrences",
                    {"reconcile_run_id": reconcile_id, **occurrence},
                )
            for decision in decisions:
                connection.execute(
                    """
                    UPDATE stock_candidates
                    SET review_status=?, final_start=?, final_duration=?,
                        final_duration_seconds=?, manually_modified=?,
                        manual_change_json=?, final_effect_signature=?,
                        final_compilation_timeline_offset=?, final_project_timecode=?,
                        updated_at=?
                    WHERE run_id=? AND stock_clip_id=?
                    """,
                    (
                        decision["review_status"],
                        decision.get("final_start"),
                        decision.get("final_duration"),
                        decision.get("final_duration_seconds"),
                        int(bool(decision.get("manually_modified"))),
                        json_dumps(decision.get("manual_change", {})),
                        decision.get("final_effect_signature"),
                        decision.get("final_compilation_timeline_offset"),
                        decision.get("final_project_timecode"),
                        now,
                        stockify_run_id,
                        decision["stock_clip_id"],
                    ),
                )
            connection.execute(
                """
                UPDATE reconcile_runs
                SET status=?, completed_at=?, approved_count=?, rejected_count=?,
                    modified_count=?, conflict_count=?, error_text=NULL
                WHERE id=?
                """,
                (
                    status,
                    now,
                    counts["approved"],
                    counts["rejected"],
                    counts["modified"],
                    counts["conflict"],
                    reconcile_id,
                ),
            )

    def fail_reconcile_run(self, reconcile_id: str, error_text: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE reconcile_runs SET status='failed', completed_at=?, error_text=? WHERE id=?",
                (utc_now(), error_text, reconcile_id),
            )

    # Geocode and weather caches

    def get_geocode_cache(self, cache_key: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM geocode_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["response"] = json_loads(item.pop("response_json"), {})
        return item

    def put_geocode_cache(
        self,
        *,
        cache_key: str,
        latitude: float,
        longitude: float,
        provider: str,
        response: dict[str, Any],
    ) -> None:
        values = {
            "cache_key": cache_key,
            "latitude": latitude,
            "longitude": longitude,
            "provider": provider,
            "response_json": json_dumps(response),
            "fetched_at": utc_now(),
        }
        with self.database.transaction() as connection:
            _upsert(
                connection,
                "geocode_cache",
                values,
                ("cache_key",),
                ("latitude", "longitude", "provider", "response_json", "fetched_at"),
            )

    def weather_for_session(self, session_id: str, provider: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM weather_observations WHERE session_id=? AND provider=?",
                (session_id, provider),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["raw"] = json_loads(item.pop("raw_json"), {})
        return item

    def upsert_weather(self, values: dict[str, Any]) -> None:
        serialized = dict(values)
        serialized["raw_json"] = json_dumps(serialized.pop("raw", {}))
        with self.database.transaction() as connection:
            _upsert(
                connection,
                "weather_observations",
                serialized,
                ("session_id", "provider"),
                tuple(
                    column
                    for column in serialized
                    if column not in {"id", "session_id", "provider"}
                ),
            )
            connection.execute(
                "UPDATE shoot_sessions SET weather_status=?, updated_at=? WHERE id=?",
                (values["status"], utc_now(), values["session_id"]),
            )

    def astronomy_for_session(self, session_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM astronomy_observations WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["concept_signals"] = json_loads(item.pop("concept_signals_json"), {})
        item["visual_analysis"] = json_loads(item.pop("visual_analysis_json"), {})
        item["raw"] = json_loads(item.pop("raw_json"), {})
        return item

    def upsert_astronomy(self, values: dict[str, Any]) -> None:
        serialized = dict(values)
        serialized["concept_signals_json"] = json_dumps(
            serialized.pop("concept_signals", {})
        )
        serialized["visual_analysis_json"] = json_dumps(
            serialized.pop("visual_analysis", {})
        )
        serialized["raw_json"] = json_dumps(serialized.pop("raw", {}))
        with self.database.transaction() as connection:
            _upsert(
                connection,
                "astronomy_observations",
                serialized,
                ("session_id",),
                tuple(
                    column
                    for column in serialized
                    if column not in {"id", "session_id"}
                ),
            )
            connection.execute(
                "UPDATE shoot_sessions SET astronomy_status=?, updated_at=? WHERE id=?",
                (values["status"], utc_now(), values["session_id"]),
            )

    # Export and package persistence

    def upsert_export(self, values: dict[str, Any]) -> dict[str, Any]:
        """Insert or update an export row; return the persisted canonical row.

        Conflict key is logical ``(stockify_run_id, stock_clip_id)``. On conflict,
        ``id`` is preserved and ``exported_path`` (and other metadata) update.
        Callers must use the returned ``id`` for package_clips / media FKs.
        """
        with self.database.transaction() as connection:
            _upsert(
                connection,
                "exports",
                values,
                ("stockify_run_id", "stock_clip_id"),
                tuple(
                    column
                    for column in values
                    if column not in {"id", "stockify_run_id", "stock_clip_id"}
                ),
            )
            connection.execute(
                "UPDATE stock_candidates SET export_status='matched', updated_at=? "
                "WHERE run_id=? AND stock_clip_id=?",
                (utc_now(), values["stockify_run_id"], values["stock_clip_id"]),
            )
            row = connection.execute(
                "SELECT * FROM exports WHERE stockify_run_id=? AND stock_clip_id=?",
                (values["stockify_run_id"], values["stock_clip_id"]),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                "upsert_export failed to persist "
                f"{values.get('stockify_run_id')}/{values.get('stock_clip_id')}"
            )
        return dict(row)

    def mark_missing_exports(self, run_id: str, stock_clip_ids: Iterable[str]) -> None:
        ids = list(stock_clip_ids)
        if not ids:
            return
        with self.database.transaction() as connection:
            connection.executemany(
                "UPDATE stock_candidates SET export_status='missing', updated_at=? "
                "WHERE run_id=? AND stock_clip_id=?",
                [(utc_now(), run_id, clip_id) for clip_id in ids],
            )

    def export_for_candidate(self, run_id: str, stock_clip_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM exports WHERE stockify_run_id=? AND stock_clip_id=?",
                (run_id, stock_clip_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_package(
        self,
        *,
        package: dict[str, Any],
        clips: list[dict[str, Any]],
    ) -> None:
        now = utc_now()
        values = dict(package)
        values["metadata_json"] = json_dumps(values.pop("metadata"))
        values.setdefault("created_at", now)
        values["updated_at"] = now
        with self.database.transaction() as connection:
            _upsert(
                connection,
                "packages",
                values,
                ("stockify_run_id", "source_project_id"),
                (
                    "session_id",
                    "title",
                    "slug",
                    "output_path",
                    "clip_count",
                    "status",
                    "metadata_json",
                    "updated_at",
                ),
            )
            package_row = connection.execute(
                "SELECT id FROM packages WHERE stockify_run_id=? AND source_project_id=?",
                (package["stockify_run_id"], package["source_project_id"]),
            ).fetchone()
            assert package_row is not None
            package_id = str(package_row["id"])
            connection.execute("DELETE FROM package_clips WHERE package_id=?", (package_id,))
            for clip in clips:
                _insert(connection, "package_clips", {"package_id": package_id, **clip})

    def database_status(self) -> dict[str, int]:
        tables = (
            "stockify_runs",
            "source_projects",
            "shoot_sessions",
            "stock_candidates",
            "reconcile_runs",
            "exports",
            "packages",
            "processed_libraries",
        )
        with self.database.connect() as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }
