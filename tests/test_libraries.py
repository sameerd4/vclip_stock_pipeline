from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import xml.etree.ElementTree as ET

from vclip_pipeline.cli import main
from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify import StockifyService
from vclip_pipeline.stockify.libraries import (
    discover_xml_library_names,
    find_fcpbundles,
    format_libraries_report,
    resolve_source_library,
)


def test_resolve_source_library_from_xml_location(tmp_path: Path):
    bundle = tmp_path / "February 2026.fcpbundle"
    bundle.mkdir()
    root = ET.Element("fcpxml", {"version": "1.12"})
    ET.SubElement(
        root,
        "library",
        {"location": bundle.resolve().as_uri() + "/"},
    )
    resolved = resolve_source_library(
        requested_path=tmp_path / "export.fcpxml",
        input_path=tmp_path / "export.fcpxml",
        source_root=root,
    )
    assert resolved == ("February 2026.fcpbundle", str(bundle.resolve()))


def test_resolve_source_library_from_bundle_path(tmp_path: Path):
    bundle = tmp_path / "December 2023.fcpbundle"
    info = bundle / "Info.fcpxml"
    bundle.mkdir()
    info.write_text("<fcpxml/>", encoding="utf-8")
    resolved = resolve_source_library(
        requested_path=bundle,
        input_path=info,
        source_root=None,
    )
    assert resolved == ("December 2023.fcpbundle", str(bundle.resolve()))


def test_mark_library_processed_is_idempotent(tmp_path: Path):
    database = Database(tmp_path / "libs.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at
            ) VALUES
            ('STOCKIFY_A', 'a.xml', 'aaa', 'out.xml', 'r.json', '0.1.0', 'complete', '{}', 't'),
            ('STOCKIFY_B', 'b.xml', 'bbb', 'out.xml', 'r.json', '0.1.0', 'complete', '{}', 't')
            """
        )
    path = str((tmp_path / "Olympia Day.fcpbundle").resolve())
    repository.mark_library_processed(
        library_name="Olympia Day.fcpbundle",
        library_path=path,
        stockify_run_id="STOCKIFY_A",
    )
    repository.mark_library_processed(
        library_name="Olympia Day.fcpbundle",
        library_path=path,
        stockify_run_id="STOCKIFY_B",
    )
    rows = repository.processed_libraries()
    assert len(rows) == 1
    assert rows[0]["first_stockify_run_id"] == "STOCKIFY_A"
    assert rows[0]["last_stockify_run_id"] == "STOCKIFY_B"
    assert rows[0]["first_processed_at"] <= rows[0]["last_processed_at"]


def test_stockify_records_processed_library(pipeline_run):
    # Attach a library location to the already-processed fixture source and re-run.
    tmp_path = pipeline_run["tmp_path"]
    bundle = tmp_path / "South Lake Union.fcpbundle"
    bundle.mkdir()
    source = Path(pipeline_run["source"])
    tree = ET.parse(source)
    root = tree.getroot()
    library = root.find("library")
    assert library is not None
    library.set("location", bundle.resolve().as_uri() + "/")
    located = tmp_path / "located-source.fcpxml"
    tree.write(located, encoding="utf-8", xml_declaration=True)

    options = replace(
        pipeline_run["options"],
        input_path=located,
        requested_path=located,
        output_path=tmp_path / "review-libs.fcpxml",
        report_path=tmp_path / "review-libs-report.json",
        manifest_path=tmp_path / "review-libs-manifest.json",
    )
    result = StockifyService(
        pipeline_run["repository"], pipeline_run["resolver"]
    ).run(options)
    rows = pipeline_run["repository"].processed_libraries()
    assert len(rows) == 1
    assert rows[0]["library_name"] == "South Lake Union.fcpbundle"
    assert rows[0]["last_stockify_run_id"] == result.stockify_run_id


def test_libraries_cli_scan_marks_remaining(tmp_path: Path, capsys):
    database_path = tmp_path / "vclip.sqlite3"
    database = Database(database_path)
    database.migrate()
    repository = CatalogRepository(database)
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at
            ) VALUES ('STOCKIFY_X', 'x.xml', 'x', 'o.xml', 'r.json', '0.1.0', 'complete', '{}', 't')
            """
        )
    drive = tmp_path / "Volumes" / "Archive"
    drive.mkdir(parents=True)
    done = drive / "February 2026.fcpbundle"
    todo = drive / "Berkeley.fcpbundle"
    done.mkdir()
    todo.mkdir()
    repository.mark_library_processed(
        library_name=done.name,
        library_path=str(done.resolve()),
        stockify_run_id="STOCKIFY_X",
    )

    code = main(
        [
            "libraries",
            "--db",
            str(database_path),
            "--scan",
            str(drive),
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "✓ February 2026.fcpbundle" in output
    assert "○ Berkeley.fcpbundle" in output
    assert "Remaining: 1" in output


def test_find_and_format_helpers(tmp_path: Path):
    root = tmp_path / "drive"
    (root / "Santa Cruz.fcpbundle").mkdir(parents=True)
    (root / "nested" / "December 2023.fcpbundle").mkdir(parents=True)
    found = find_fcpbundles(root)
    assert [path.name for path in found] == [
        "December 2023.fcpbundle",
        "Santa Cruz.fcpbundle",
    ]
    lines = format_libraries_report(
        processed=[
            {
                "library_name": "December 2023.fcpbundle",
                "library_path": str(found[0]),
            }
        ],
        scanned=found,
    )
    assert lines == [
        "✓ December 2023.fcpbundle",
        "○ Santa Cruz.fcpbundle",
    ]


def test_libraries_cli_reports_xml_found_missing(tmp_path: Path, capsys):
    database_path = tmp_path / "vclip.sqlite3"
    database = Database(database_path)
    database.migrate()
    repository = CatalogRepository(database)
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at
            ) VALUES ('STOCKIFY_X', 'x.xml', 'x', 'o.xml', 'r.json', '0.1.0', 'complete', '{}', 't')
            """
        )

    drive = tmp_path / "Volumes"
    xml_dir = tmp_path / "work"
    drive.mkdir()
    xml_dir.mkdir()
    feb = drive / "February 2026.fcpbundle"
    client = drive / "Client Work.fcpbundle"
    april = drive / "April 2026.fcpbundle"
    for bundle in (feb, client, april):
        bundle.mkdir()
    repository.mark_library_processed(
        library_name=feb.name,
        library_path=str(feb.resolve()),
        stockify_run_id="STOCKIFY_X",
    )

    # Primary match: library location inside FCPXML.
    feb_xml = xml_dir / "export-feb.fcpxml"
    root = ET.Element("fcpxml", {"version": "1.12"})
    ET.SubElement(root, "library", {"location": feb.resolve().as_uri() + "/"})
    ET.ElementTree(root).write(feb_xml, encoding="utf-8", xml_declaration=True)
    # Fallback match: normalized filename.
    april_bundle = xml_dir / "April 2026.fcpxmld"
    april_bundle.mkdir()
    (april_bundle / "Info.fcpxml").write_text(
        '<?xml version="1.0"?><fcpxml version="1.12"><library/></fcpxml>',
        encoding="utf-8",
    )

    names = discover_xml_library_names(xml_dir)
    assert "February 2026.fcpbundle" in names
    assert "April 2026.fcpbundle" in names

    code = main(
        [
            "libraries",
            "--db",
            str(database_path),
            "--scan",
            str(drive),
            "--xml-dir",
            str(xml_dir),
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "✓ February 2026.fcpbundle" in output
    assert "XML found" in output
    assert "○ Client Work.fcpbundle" in output
    assert "XML missing" in output
    assert "○ April 2026.fcpbundle" in output or "✓ April 2026.fcpbundle" in output
    assert "Libraries: 3" in output
    assert "XML found: 2" in output
    assert "XML missing: 1" in output
    # XML presence must not imply processed.
    assert "○ April 2026.fcpbundle" in output
