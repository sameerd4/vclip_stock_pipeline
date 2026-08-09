from __future__ import annotations

from vclip_pipeline.reconcile import ReconcileService
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.export_ingest import ExportIngestService


def test_export_ingest_persists_matching_files_without_packaging(pipeline_run):
    ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=pipeline_run["output"],
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    candidates = pipeline_run["repository"].candidates_for_run(
        pipeline_run["result"].stockify_run_id,
        accepted_only=True,
        approved_only=True,
    )
    exports = pipeline_run["tmp_path"] / "exports"
    exports.mkdir()
    target = candidates[0]
    exported = exports / f"{target['expected_export_basename']}.mp4"
    exported.write_bytes(b"not-real-video")

    workflow = WorkflowCatalog(pipeline_run["database"])
    report = ExportIngestService(
        pipeline_run["repository"],
        workflow,
    ).run(
        exports_directory=exports,
        run_id=pipeline_run["result"].stockify_run_id,
        calculate_checksums=True,
        inspect_media=False,
        allow_unmatched=False,
        allow_missing=True,
        allow_duration_mismatch=False,
        allow_unreconciled=False,
        dry_run=False,
        report_path=None,
    )
    assert report.exports_matched == 1
    assert report.exports_persisted == 1
    stored = pipeline_run["repository"].export_for_candidate(
        pipeline_run["result"].stockify_run_id,
        target["stock_clip_id"],
    )
    assert stored is not None
    assert stored["exported_path"] == str(exported.resolve())
    with pipeline_run["database"].connect() as connection:
        media = connection.execute(
            "SELECT * FROM export_media_metadata WHERE export_id=?",
            (stored["id"],),
        ).fetchone()
    assert media is not None
