from __future__ import annotations

import json
import struct
from datetime import datetime
from pathlib import Path

from test_review_color_integrity import _seed_candidate

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify.jpg_exif_same_shoot import (
    EVIDENCE_SOURCE,
    DjiFileIdentity,
    JpgExifPhoto,
    index_jpg_photos,
    infer_jpg_exif_same_shoot,
    parse_dji_file_identity,
    parse_jpeg_exif_gps_bytes,
    prune_media_walk_dirnames,
)
from vclip_pipeline.util import json_dumps
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_location_recover import ReviewLocationRecoverService

NS = "http://www.apple.com/finalcutpro/fcpxml"


class FixedResolver:
    def __init__(self, place: dict):
        self.place = place

    def resolve(self, latitude: float, longitude: float):
        if abs(latitude - 47.61) < 0.05 and abs(longitude + 122.32) < 0.05:
            return dict(self.place)
        return None


SEATTLE = {
    "provider": "test",
    "country": "United States",
    "state": "Washington",
    "city": "Seattle",
    "neighborhood": "Capitol Hill",
    "poi": None,
    "timezone": "America/Los_Angeles",
}


def _rational(value: float) -> tuple[int, int]:
    return int(round(value * 10000)), 10000


def _build_jpeg_with_gps(
    *,
    latitude: float,
    longitude: float,
    model: str = "FC9313",
    create_date: str = "2025:11:08 21:39:54",
) -> bytes:
    """Minimal JPEG + EXIF APP1 with GPS IFD for unit tests."""
    endian = "<"
    # GPS IFD will be placed after IFD0.
    # Build GPS values first.
    lat_ref = b"N\x00"
    lon_ref = b"W\x00"
    lat_abs = abs(latitude)
    lon_abs = abs(longitude)
    lat_deg = int(lat_abs)
    lat_min = int((lat_abs - lat_deg) * 60)
    lat_sec = ((lat_abs - lat_deg) * 60 - lat_min) * 60
    lon_deg = int(lon_abs)
    lon_min = int((lon_abs - lon_deg) * 60)
    lon_sec = ((lon_abs - lon_deg) * 60 - lon_min) * 60

    def pack_rationals(values: list[float]) -> bytes:
        out = b""
        for value in values:
            num, den = _rational(value)
            out += struct.pack(endian + "II", num, den)
        return out

    # Layout inside TIFF:
    # 0: header(8)
    # 8: IFD0 count + entries + next
    # then model string, datetime string, GPS IFD, GPS rationals
    header = b"II" + struct.pack(endian + "HI", 42, 8)

    # We'll assemble with placeholders then fix offsets.
    model_bytes = model.encode("ascii") + b"\x00"
    dt_bytes = create_date.encode("ascii") + b"\x00"

    # IFD0 tags: Model(0x0110), DateTimeOriginal(0x9003), GPS IFD pointer(0x8825)
    ifd0_count = 3
    ifd0_size = 2 + ifd0_count * 12 + 4
    model_offset = 8 + ifd0_size
    dt_offset = model_offset + len(model_bytes)
    gps_ifd_offset = dt_offset + len(dt_bytes)

    def entry(tag: int, typ: int, count: int, value: bytes) -> bytes:
        return struct.pack(endian + "HHI", tag, typ, count) + value

    ifd0 = struct.pack(endian + "H", ifd0_count)
    ifd0 += entry(0x0110, 2, len(model_bytes), struct.pack(endian + "I", model_offset))
    ifd0 += entry(0x9003, 2, len(dt_bytes), struct.pack(endian + "I", dt_offset))
    ifd0 += entry(0x8825, 4, 1, struct.pack(endian + "I", gps_ifd_offset))
    ifd0 += struct.pack(endian + "I", 0)

    # GPS IFD: lat ref, lat, lon ref, lon
    gps_count = 4
    gps_ifd_size = 2 + gps_count * 12 + 4
    lat_data_offset = gps_ifd_offset + gps_ifd_size
    lon_data_offset = lat_data_offset + 24

    gps = struct.pack(endian + "H", gps_count)
    gps += entry(1, 2, 2, lat_ref.ljust(4, b"\x00"))
    gps += entry(2, 5, 3, struct.pack(endian + "I", lat_data_offset))
    gps += entry(3, 2, 2, lon_ref.ljust(4, b"\x00"))
    gps += entry(4, 5, 3, struct.pack(endian + "I", lon_data_offset))
    gps += struct.pack(endian + "I", 0)
    gps += pack_rationals([lat_deg, lat_min, lat_sec])
    gps += pack_rationals([lon_deg, lon_min, lon_sec])

    tiff = header + ifd0 + model_bytes + dt_bytes + gps
    exif = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    # Minimal SOF/SOS-less JPEG: SOI + APP1 + EOI is enough for our parser.
    return b"\xff\xd8" + app1 + b"\xff\xd9"


def _photo(
    *,
    path: str,
    seq: int,
    dt: datetime,
    lat: float,
    lon: float,
    model: str = "FC9313",
) -> JpgExifPhoto:
    identity = DjiFileIdentity(
        stem=f"DJI_{dt.strftime('%Y%m%d%H%M%S')}_{seq:04d}_D",
        filename=Path(path).name,
        capture_datetime=dt,
        sequence=seq,
        suffix="D",
        date=dt.date().isoformat(),
    )
    return JpgExifPhoto(
        path=Path(path),
        identity=identity,
        latitude=lat,
        longitude=lon,
        camera_model=model,
        exif_datetime=dt,
    )


def test_parse_dji_file_identity_and_exif_gps():
    identity = parse_dji_file_identity("DJI_20251108214016_0580_D.mp4")
    assert identity is not None
    assert identity.sequence == 580
    assert identity.date == "2025-11-08"
    assert identity.capture_datetime == datetime(2025, 11, 8, 21, 40, 16)

    blob = _build_jpeg_with_gps(latitude=47.61189, longitude=-122.3209)
    lat, lon, _alt, model, dt = parse_jpeg_exif_gps_bytes(blob)
    assert model == "FC9313"
    assert dt == datetime(2025, 11, 8, 21, 39, 54)
    assert lat is not None and abs(lat - 47.61189) < 1e-4
    assert lon is not None and abs(lon + 122.3209) < 1e-4


def test_high_confidence_adjacent_sequence():
    video = "DJI_20251108214016_0580_D.mp4"
    photos = [
        _photo(
            path="/media/DJI_20251108213954_0578_D.JPG",
            seq=578,
            dt=datetime(2025, 11, 8, 21, 39, 54),
            lat=47.61189,
            lon=-122.3209,
        ),
        _photo(
            path="/media/DJI_20251108214100_0582_D.JPG",
            seq=582,
            dt=datetime(2025, 11, 8, 21, 41, 0),
            lat=47.61195,
            lon=-122.3208,
        ),
    ]
    index = {"2025-11-08": photos}
    inference = infer_jpg_exif_same_shoot(video, jpg_index=index)
    assert inference is not None
    assert inference.confidence == "high"
    assert inference.review_required is False
    assert inference.evidence_source == EVIDENCE_SOURCE
    assert len(inference.evidence_photos) >= 2
    assert all(item.camera_model == "FC9313" for item in inference.evidence_photos)
    obs = inference.as_source_observation()
    assert obs["evidence_source"] == EVIDENCE_SOURCE
    assert "jpg_exif_same_shoot" in obs


def test_bracketing_photos_high_confidence():
    video = "DJI_20251108214016_0580_D.mp4"
    photos = [
        _photo(
            path="/media/before.JPG",
            seq=570,
            dt=datetime(2025, 11, 8, 21, 35, 0),
            lat=47.6120,
            lon=-122.3210,
        ),
        _photo(
            path="/media/after.JPG",
            seq=590,
            dt=datetime(2025, 11, 8, 21, 45, 0),
            lat=47.6121,
            lon=-122.3205,
        ),
    ]
    inference = infer_jpg_exif_same_shoot(video, jpg_index={"2025-11-08": photos})
    assert inference is not None
    assert inference.confidence == "high"
    assert "bracketing" in inference.association_reason
    roles = {item.role for item in inference.evidence_photos}
    assert roles == {"bracket_before", "bracket_after"}


def test_medium_and_low_require_review():
    video = "DJI_20251108214016_0580_D.mp4"
    medium_photos = [
        _photo(
            path="/media/near.JPG",
            seq=590,
            dt=datetime(2025, 11, 8, 21, 50, 0),
            lat=47.6119,
            lon=-122.3209,
        ),
        _photo(
            path="/media/near2.JPG",
            seq=591,
            dt=datetime(2025, 11, 8, 21, 50, 10),
            lat=47.61195,
            lon=-122.32085,
        ),
    ]
    medium = infer_jpg_exif_same_shoot(video, jpg_index={"2025-11-08": medium_photos})
    assert medium is not None
    assert medium.confidence == "medium"
    assert medium.review_required is True

    low_photos = [
        _photo(
            path="/media/far.JPG",
            seq=700,
            dt=datetime(2025, 11, 8, 22, 30, 0),
            lat=47.6119,
            lon=-122.3209,
        )
    ]
    low = infer_jpg_exif_same_shoot(video, jpg_index={"2025-11-08": low_photos})
    assert low is not None
    assert low.confidence == "low"
    assert low.review_required is True


def test_duplicate_archive_and_fcp_copies_count_once():
    video = "DJI_20251108214016_0580_D.mp4"
    dt = datetime(2025, 11, 8, 21, 39, 54)
    photos = [
        _photo(
            path="/archive/DJI_20251108213954_0578_D.JPG",
            seq=578,
            dt=dt,
            lat=47.61189,
            lon=-122.3209,
        ),
        _photo(
            path="/library.fcpbundle/Original Media/DJI_20251108213954_0578_D.JPG",
            seq=578,
            dt=dt,
            lat=47.61189,
            lon=-122.3209,
        ),
    ]
    inference = infer_jpg_exif_same_shoot(video, jpg_index={"2025-11-08": photos})
    assert inference is not None
    assert inference.confidence == "high"
    assert inference.sample_count == 1
    assert len(inference.evidence_photos) == 1
    assert inference.evidence_photos[0].path == "/archive/DJI_20251108213954_0578_D.JPG"
    assert inference.evidence_photos[0].duplicate_paths == [
        "/library.fcpbundle/Original Media/DJI_20251108213954_0578_D.JPG"
    ]


def _write_unknown_shard(
    path: Path,
    *,
    run_id: str,
    clip_id: str,
    project_name: str,
    event_name: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<?xml version='1.0' encoding='utf-8'?>
<fcp:fcpxml xmlns:fcp="{NS}" version="1.12">
  <fcp:resources>
    <fcp:format id="r1" name="FFVideoFormat3840x2160p30" frameDuration="100/3000s" width="3840" height="2160"/>
    <fcp:asset id="r2" name="media" uid="ASSET1" start="0s" duration="10s" hasVideo="1" format="r1">
      <fcp:media-rep kind="original-media" src="file:///tmp/media.mov"/>
    </fcp:asset>
  </fcp:resources>
  <fcp:library>
    <fcp:event name="{event_name}">
      <fcp:project name="{project_name}" uid="proj-1">
        <fcp:sequence format="r1" duration="10s" tcStart="0s" tcFormat="NDF">
          <fcp:spine>
            <fcp:asset-clip ref="r2" name="clip" offset="0s" duration="10s" start="0s">
              <fcp:metadata>
                <fcp:md key="com.vclip.stock_clip_id" value="{clip_id}"/>
                <fcp:md key="com.vclip.stockify_run_id" value="{run_id}"/>
              </fcp:metadata>
            </fcp:asset-clip>
          </fcp:spine>
        </fcp:sequence>
      </fcp:project>
    </fcp:event>
  </fcp:library>
</fcp:fcpxml>
""",
        encoding="utf-8",
    )
    path.with_name(f"{path.stem}-shard-manifest.json").write_text(
        json.dumps(
            {
                "stockify_run_id": run_id,
                "stock_clip_ids": [clip_id],
                "projects": [
                    {
                        "event_name": event_name,
                        "project_name": project_name,
                        "representation": "individual",
                        "stock_clip_ids": [clip_id],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_forensic_mode_is_read_only_and_preserves_provenance(tmp_path: Path):
    database = Database(tmp_path / "jpg.sqlite3")
    database.migrate()
    WorkflowCatalog(database)
    run_id = "STOCKIFY_JPG_FORENSIC"
    source = "DJI_20251108214016_0580_D.MP4"
    clip_id = "VCLIP_JPG_FORENSIC"
    media_dir = tmp_path / "media" / "DJI Mini 5 Pro"
    media_dir.mkdir(parents=True)
    (media_dir / source).write_bytes(b"fake")
    jpg_path = media_dir / "DJI_20251108213954_0578_D.JPG"
    jpg_path.write_bytes(_build_jpeg_with_gps(latitude=47.61189, longitude=-122.3209))
    _seed_candidate(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        camera_lut=None,
        media_path=str(media_dir / source),
        capture_date="2025-11-08",
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET generated_event_name=?, location_json=?
            WHERE stock_clip_id=?
            """,
            (
                "Unknown Location — 2025-11-08",
                json_dumps({"public_label": "Unknown Location", "city": None}),
                clip_id,
            ),
        )

    input_root = tmp_path / "review-shards-t9-recovery" / "market"
    _write_unknown_shard(
        input_root / f"{clip_id}.fcpxml",
        run_id=run_id,
        clip_id=clip_id,
        project_name="Unknown Location Blue Hour — Clip 01",
        event_name="Unknown Location — 2025-11-08",
    )

    report_path = tmp_path / "library-audits" / "jpg-forensic.json"
    text_path = tmp_path / "library-audits" / "jpg-forensic.txt"
    report = ReviewLocationRecoverService(
        CatalogRepository(database),
        FixedResolver(SEATTLE),
    ).run(
        input_root=input_root.parent,
        output_root=None,
        media_roots=[tmp_path / "media"],
        report_path=report_path,
        text_report_path=text_path,
        forensic_jpg_exif=True,
    )

    assert report.forensic_jpg_exif is True
    assert report.candidates_moved_or_relabelled == 0
    assert report.jpg_exif_forensic["sources_with_jpg_inference"] == 1
    focus = report.jpg_exif_forensic["focus_unknown_location_2025_11_08"]
    assert focus["sources"] == 1
    inference = focus["inferences"][0]
    assert inference["confidence"] == "high"
    assert inference["evidence_source"] == EVIDENCE_SOURCE
    assert inference["evidence_photos"]
    assert inference["evidence_photos"][0]["sequence_delta"] is not None
    assert inference["evidence_photos"][0]["time_delta_seconds"] is not None
    assert inference["evidence_photos"][0]["camera_model"] == "FC9313"
    assert report.clusters
    assert report.clusters[0]["evidence_source"] == EVIDENCE_SOURCE
    assert report.clusters[0]["gps_kind"] == "inferred_jpg_exif_same_shoot"
    # No mutation of shard tree or recoveries table.
    assert not (tmp_path / "out").exists()
    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM review_location_recoveries"
        ).fetchone()["n"]
        label = connection.execute(
            "SELECT generated_event_name FROM stock_candidates WHERE stock_clip_id=?",
            (clip_id,),
        ).fetchone()["generated_event_name"]
    assert count == 0
    assert label == "Unknown Location — 2025-11-08"
    assert "jpg_exif_same_shoot" in text_path.read_text()
    assert "2025-11-08" in text_path.read_text()


def test_index_jpg_photos_includes_fcpbundle_original_media(tmp_path: Path):
    """Same-day JPG index must see DJI stills inside FCP Original Media."""
    original = tmp_path / "May 2026 - Seattle.fcpbundle" / "Finals" / "Original Media"
    original.mkdir(parents=True)
    # Heavy FCP internals should be skipped, not required for indexing.
    (tmp_path / "May 2026 - Seattle.fcpbundle" / "Render Files").mkdir(parents=True)
    jpg_name = "DJI_20260502195525_0040_D.jpg"
    (original / jpg_name).write_bytes(_build_jpeg_with_gps(latitude=47.606, longitude=-122.332))
    # AppleDouble sidecar must not be indexed.
    (original / f"._#{jpg_name}").write_bytes(b"junk")
    (original / f"._{jpg_name}").write_bytes(b"junk")

    index = index_jpg_photos([tmp_path], needed_dates={"2026-05-02"})
    assert "2026-05-02" in index
    assert len(index["2026-05-02"]) == 1
    photo = index["2026-05-02"][0]
    assert photo.path.name == jpg_name
    assert photo.has_gps
    # Matching model unchanged: indexed photo can infer the sibling video.
    inference = infer_jpg_exif_same_shoot(
        "DJI_20260502195637_0044_D.mp4",
        jpg_index=index,
    )
    assert inference is not None
    assert inference.evidence_source == EVIDENCE_SOURCE


def test_prune_media_walk_enters_fcpbundle_skips_render_files():
    kept = prune_media_walk_dirnames(
        "/Volumes/drive",
        ["raw", "May 2026.fcpbundle", ".hidden"],
    )
    assert "May 2026.fcpbundle" in kept
    assert "raw" in kept
    assert ".hidden" not in kept
    inside = prune_media_walk_dirnames(
        "/Volumes/drive/May 2026.fcpbundle/Finals",
        ["Original Media", "Render Files", "Proxy Media"],
    )
    assert inside == ["Original Media"]
