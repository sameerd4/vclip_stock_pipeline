"""Infer source location from nearby DJI JPG/JPEG EXIF GPS (same-shoot correlation).

This is never treated as direct source GPS. Provenance must always record
evidence_sources including ``jpg_exif_same_shoot``.
"""

from __future__ import annotations

import os
import re
import struct
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .metadata import haversine_meters, is_usable_gps, parse_dji_filename_datetime
from .sidecars import normalized_stem

EVIDENCE_SOURCE = "jpg_exif_same_shoot"

# DJI_YYYYMMDDHHMMSS_NNNN[_suffix].ext
DJI_FILE_IDENTITY_RE = re.compile(
    r"(?P<stem>DJI_(?P<date>\d{8})(?P<time>\d{6})_(?P<seq>\d{3,5})"
    r"(?:_(?P<suffix>[A-Za-z0-9]+))?)",
    re.I,
)

JPG_EXTENSIONS = {".jpg", ".jpeg"}

# Inside .fcpbundle packages, skip heavy internals but still reach Original Media
# (same visibility the sequence-neighbor sibling scan already has).
_FCP_BUNDLE_SKIP_DIRNAMES = frozenset(
    {
        "Render Files",
        "Transcoded Media",
        "Proxy Media",
        "Analysis Files",
        "Shared Items",
        "Internal Data",
    }
)

# Association thresholds.
HIGH_SEQ_DELTA = 2
HIGH_TIME_DELTA_S = 120
MEDIUM_SEQ_DELTA = 15
MEDIUM_TIME_DELTA_S = 15 * 60
LOW_TIME_DELTA_S = 2 * 60 * 60
BRACKET_COHERENT_METERS = 250.0
SAME_DAY_CLUSTER_METERS = 500.0


@dataclass
class DjiFileIdentity:
    stem: str
    filename: str
    capture_datetime: datetime
    sequence: int
    suffix: str | None = None
    date: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stem": self.stem,
            "filename": self.filename,
            "capture_datetime": self.capture_datetime.isoformat(sep=" "),
            "sequence": self.sequence,
            "suffix": self.suffix,
            "date": self.date,
        }


@dataclass
class JpgExifPhoto:
    path: Path
    identity: DjiFileIdentity
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    camera_model: str | None = None
    exif_datetime: datetime | None = None
    # Other filesystem copies of the same DJI still (archive vs FCP Original Media).
    alternate_paths: list[Path] = field(default_factory=list)

    @property
    def has_gps(self) -> bool:
        return is_usable_gps(self.latitude, self.longitude)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "identity": self.identity.as_dict(),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude": self.altitude,
            "camera_model": self.camera_model,
            "exif_datetime": (
                self.exif_datetime.isoformat(sep=" ") if self.exif_datetime else None
            ),
            "has_gps": self.has_gps,
            "alternate_paths": [str(path) for path in self.alternate_paths],
        }


@dataclass
class JpgEvidenceLink:
    path: str
    latitude: float | None
    longitude: float | None
    altitude: float | None
    camera_model: str | None
    capture_datetime: str | None
    sequence: int | None
    sequence_delta: int | None
    time_delta_seconds: float | None
    role: str
    duplicate_paths: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JpgExifInference:
    source_basename: str
    source_stem: str
    source_identity: DjiFileIdentity | None
    latitude: float
    longitude: float
    confidence: str
    review_required: bool
    association_reason: str
    evidence_photos: list[JpgEvidenceLink] = field(default_factory=list)
    camera_models: list[str] = field(default_factory=list)
    evidence_source: str = EVIDENCE_SOURCE
    sample_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_basename": self.source_basename,
            "source_stem": self.source_stem,
            "source_identity": (self.source_identity.as_dict() if self.source_identity else None),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "confidence": self.confidence,
            "review_required": self.review_required,
            "association_reason": self.association_reason,
            "evidence_source": self.evidence_source,
            "evidence_photos": [item.as_dict() for item in self.evidence_photos],
            "camera_models": list(self.camera_models),
            "sample_count": self.sample_count,
            "note": (
                "Coordinates are inferred from same-shoot JPG EXIF GPS; not direct source GPS."
            ),
        }

    def as_source_observation(self) -> dict[str, Any]:
        """Shape compatible with review-location-recover source_observations."""
        return {
            "source_basename": self.source_basename,
            "stem": self.source_stem,
            "lat": float(self.latitude),
            "lon": float(self.longitude),
            "sample_count": int(self.sample_count or len(self.evidence_photos) or 1),
            "srt_paths": [],
            "evidence_source": EVIDENCE_SOURCE,
            "resolution_confidence": self.confidence,
            "review_required": self.review_required,
            "jpg_exif_same_shoot": self.as_dict(),
        }


def parse_dji_file_identity(value: str | None) -> DjiFileIdentity | None:
    if not value:
        return None
    name = Path(unquote_name(value)).name
    match = DJI_FILE_IDENTITY_RE.search(name)
    if not match:
        return None
    try:
        captured = datetime.strptime(match.group("date") + match.group("time"), "%Y%m%d%H%M%S")
        sequence = int(match.group("seq"))
    except ValueError:
        return None
    return DjiFileIdentity(
        stem=match.group("stem"),
        filename=name,
        capture_datetime=captured,
        sequence=sequence,
        suffix=match.group("suffix"),
        date=f"{match.group('date')[:4]}-{match.group('date')[4:6]}-{match.group('date')[6:8]}",
    )


def unquote_name(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)


def index_jpg_photos(
    media_roots: Iterable[Path],
    *,
    needed_dates: set[str] | None = None,
) -> dict[str, list[JpgExifPhoto]]:
    """Index JPG/JPEG stills by capture date (YYYY-MM-DD).

    Walks raw media roots and also enters ``.fcpbundle`` packages to index
    DJI stills under event ``Original Media`` folders — the same filesystem
    locations the sequence-neighbor scanner already sees via sibling dirs.
    """
    by_date: dict[str, list[JpgExifPhoto]] = {}
    for root in media_roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = prune_media_walk_dirnames(dirpath, dirnames)
            for filename in filenames:
                if filename.startswith("._"):
                    continue
                suffix = Path(filename).suffix.lower()
                if suffix not in JPG_EXTENSIONS:
                    continue
                identity = parse_dji_file_identity(filename)
                if identity is None:
                    continue
                if needed_dates is not None and identity.date not in needed_dates:
                    continue
                path = Path(dirpath) / filename
                lat, lon, alt, model, exif_dt = read_jpeg_exif_gps(path)
                photo = JpgExifPhoto(
                    path=path,
                    identity=identity,
                    latitude=lat,
                    longitude=lon,
                    altitude=alt,
                    camera_model=model,
                    exif_datetime=exif_dt,
                )
                by_date.setdefault(identity.date or "", []).append(photo)
    for _date, photos in by_date.items():
        photos.sort(key=lambda item: (item.identity.sequence, str(item.path)))
    return by_date


def logical_photo_key(identity: DjiFileIdentity) -> str:
    """Identity for one DJI still, independent of archive vs FCP-copy path."""
    return identity.stem.casefold()


def dedupe_jpg_photos(photos: Iterable[JpgExifPhoto]) -> list[JpgExifPhoto]:
    """Keep one GPS observation per DJI still identity.

    Duplicate physical copies (raw archive + FCP Original Media) must not
    inflate sample counts or same-day coherence.
    """
    groups: dict[str, list[JpgExifPhoto]] = {}
    for photo in photos:
        groups.setdefault(logical_photo_key(photo.identity), []).append(photo)
    result: list[JpgExifPhoto] = []
    for items in groups.values():
        ranked = sorted(
            items,
            key=lambda photo: (
                0 if photo.has_gps else 1,
                _fcpbundle_rank(photo.path),
                str(photo.path),
            ),
        )
        keeper = ranked[0]
        extras = [
            path
            for item in ranked[1:]
            for path in (item.path, *item.alternate_paths)
            if path != keeper.path
        ]
        if extras:
            keeper = JpgExifPhoto(
                path=keeper.path,
                identity=keeper.identity,
                latitude=keeper.latitude,
                longitude=keeper.longitude,
                altitude=keeper.altitude,
                camera_model=keeper.camera_model,
                exif_datetime=keeper.exif_datetime,
                alternate_paths=list(dict.fromkeys(extras)),
            )
        result.append(keeper)
    result.sort(key=lambda item: (item.identity.sequence, str(item.path)))
    return result


def dedupe_jpg_index(
    jpg_index: dict[str, list[JpgExifPhoto]],
) -> dict[str, list[JpgExifPhoto]]:
    return {date: dedupe_jpg_photos(photos) for date, photos in jpg_index.items()}


def _fcpbundle_rank(path: Path) -> int:
    """Prefer a raw-archive copy over the same still inside an FCP bundle."""
    return 1 if any(part.endswith(".fcpbundle") for part in path.parts) else 0


def capture_dates_for_dji_sources(
    sources: Iterable[tuple[str | None, str | None]],
) -> set[str]:
    """Collect YYYY-MM-DD dates from DJI video basenames/paths."""
    dates: set[str] = set()
    for basename, media_path in sources:
        identity = parse_dji_file_identity(basename)
        if identity is None and media_path:
            identity = parse_dji_file_identity(Path(media_path).name)
        if identity is not None and identity.date:
            dates.add(identity.date)
    return dates


def prune_media_walk_dirnames(dirpath: str | Path, dirnames: list[str]) -> list[str]:
    """Prune ``os.walk`` children while still entering FCP Original Media trees."""
    path = Path(dirpath)
    in_bundle = any(part.endswith(".fcpbundle") for part in path.parts)
    kept: list[str] = []
    for name in dirnames:
        if name.startswith("."):
            continue
        # Enter .fcpbundle packages (do not treat them as opaque leaves).
        if name.endswith(".fcpbundle"):
            kept.append(name)
            continue
        if in_bundle and name in _FCP_BUNDLE_SKIP_DIRNAMES:
            continue
        kept.append(name)
    return kept


def infer_jpg_exif_same_shoot(
    source_basename: str,
    *,
    jpg_index: dict[str, list[JpgExifPhoto]],
    media_path: str | None = None,
) -> JpgExifInference | None:
    """Correlate a DJI video source to nearby same-shoot JPG EXIF GPS."""
    identity = parse_dji_file_identity(source_basename)
    if identity is None and media_path:
        identity = parse_dji_file_identity(Path(media_path).name)
    if identity is None:
        # Fall back to filename datetime without sequence — cannot correlate strongly.
        captured = parse_dji_filename_datetime(source_basename, media_path)
        if captured is None:
            return None
        identity = DjiFileIdentity(
            stem=normalized_stem(source_basename) or Path(source_basename).stem,
            filename=Path(source_basename).name,
            capture_datetime=captured,
            sequence=-1,
            date=captured.date().isoformat(),
        )

    candidates = [
        photo
        for photo in dedupe_jpg_photos(jpg_index.get(identity.date or "", []))
        if photo.has_gps
    ]
    if not candidates:
        return None

    links = [_evidence_link(identity, photo, role="candidate") for photo in candidates]
    bracket = _bracketing_pair(identity, candidates)
    if bracket is not None:
        before, after = bracket
        spread = haversine_meters(
            float(before.latitude),
            float(before.longitude),
            float(after.latitude),
            float(after.longitude),
        )
        if spread <= BRACKET_COHERENT_METERS:
            lat = (float(before.latitude) + float(after.latitude)) / 2.0
            lon = (float(before.longitude) + float(after.longitude)) / 2.0
            evidence = [
                _evidence_link(identity, before, role="bracket_before"),
                _evidence_link(identity, after, role="bracket_after"),
            ]
            return JpgExifInference(
                source_basename=source_basename,
                source_stem=normalized_stem(source_basename) or identity.stem.lower(),
                source_identity=identity,
                latitude=lat,
                longitude=lon,
                confidence="high",
                review_required=False,
                association_reason=(
                    f"bracketing_photos_geographically_coherent|spread_m={spread:.1f}"
                ),
                evidence_photos=evidence,
                camera_models=_camera_models(evidence),
                sample_count=2,
            )

    ranked = sorted(
        candidates,
        key=lambda photo: (
            abs(photo.identity.sequence - identity.sequence) if identity.sequence >= 0 else 10_000,
            abs((photo.identity.capture_datetime - identity.capture_datetime).total_seconds()),
            str(photo.path),
        ),
    )
    nearest = ranked[0]
    seq_delta = (
        abs(nearest.identity.sequence - identity.sequence) if identity.sequence >= 0 else None
    )
    time_delta = abs(
        (nearest.identity.capture_datetime - identity.capture_datetime).total_seconds()
    )
    coherent = _coherent_same_day(candidates, nearest)

    if seq_delta is not None and seq_delta <= HIGH_SEQ_DELTA and time_delta <= HIGH_TIME_DELTA_S:
        confidence = "high"
        review_required = False
        reason = f"adjacent_sequence_short_time|seq_delta={seq_delta}|time_delta_s={time_delta:.1f}"
    elif (
        seq_delta is not None
        and seq_delta <= MEDIUM_SEQ_DELTA
        and time_delta <= MEDIUM_TIME_DELTA_S
        and coherent
    ):
        confidence = "medium"
        review_required = True
        reason = (
            f"near_sequence_same_day_coherent|seq_delta={seq_delta}|time_delta_s={time_delta:.1f}"
        )
    elif time_delta <= LOW_TIME_DELTA_S and coherent:
        confidence = "low"
        review_required = True
        reason = f"same_day_weak_association|time_delta_s={time_delta:.1f}"
    else:
        return None

    # Preserve every candidate evidence photo in provenance (not only nearest).
    evidence = links
    for item in evidence:
        if item.path == str(nearest.path):
            item.role = "primary"
    return JpgExifInference(
        source_basename=source_basename,
        source_stem=normalized_stem(source_basename) or identity.stem.lower(),
        source_identity=identity,
        latitude=float(nearest.latitude),
        longitude=float(nearest.longitude),
        confidence=confidence,
        review_required=review_required,
        association_reason=reason,
        evidence_photos=evidence,
        camera_models=_camera_models(evidence),
        sample_count=len([item for item in evidence if item.latitude is not None]),
    )


def _bracketing_pair(
    identity: DjiFileIdentity,
    photos: list[JpgExifPhoto],
) -> tuple[JpgExifPhoto, JpgExifPhoto] | None:
    if identity.sequence < 0:
        return None
    before = [
        photo
        for photo in photos
        if photo.identity.sequence < identity.sequence
        and abs((photo.identity.capture_datetime - identity.capture_datetime).total_seconds())
        <= MEDIUM_TIME_DELTA_S
    ]
    after = [
        photo
        for photo in photos
        if photo.identity.sequence > identity.sequence
        and abs((photo.identity.capture_datetime - identity.capture_datetime).total_seconds())
        <= MEDIUM_TIME_DELTA_S
    ]
    if not before or not after:
        return None
    nearest_before = max(before, key=lambda photo: photo.identity.sequence)
    nearest_after = min(after, key=lambda photo: photo.identity.sequence)
    return nearest_before, nearest_after


def _coherent_same_day(photos: list[JpgExifPhoto], anchor: JpgExifPhoto) -> bool:
    if not anchor.has_gps:
        return False
    for photo in photos:
        if photo.path == anchor.path or not photo.has_gps:
            continue
        if (
            haversine_meters(
                float(anchor.latitude),
                float(anchor.longitude),
                float(photo.latitude),
                float(photo.longitude),
            )
            <= SAME_DAY_CLUSTER_METERS
        ):
            return True
    # Single GPS photo can still be medium/low only via time/seq thresholds above.
    return len(photos) == 1


def _evidence_link(
    source: DjiFileIdentity,
    photo: JpgExifPhoto,
    *,
    role: str,
) -> JpgEvidenceLink:
    seq_delta = photo.identity.sequence - source.sequence if source.sequence >= 0 else None
    time_delta = (photo.identity.capture_datetime - source.capture_datetime).total_seconds()
    return JpgEvidenceLink(
        path=str(photo.path),
        latitude=photo.latitude,
        longitude=photo.longitude,
        altitude=photo.altitude,
        camera_model=photo.camera_model,
        capture_datetime=photo.identity.capture_datetime.isoformat(sep=" "),
        sequence=photo.identity.sequence,
        sequence_delta=seq_delta,
        time_delta_seconds=time_delta,
        role=role,
        duplicate_paths=[str(path) for path in photo.alternate_paths],
    )


def _camera_models(evidence: list[JpgEvidenceLink]) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    for item in evidence:
        model = (item.camera_model or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def enumerate_nearby_jpg_evidence(
    source_basename: str,
    *,
    jpg_index: dict[str, list[JpgExifPhoto]],
    media_path: str | None = None,
    max_time_delta_s: float = 6 * 60 * 60,
    max_results: int = 25,
) -> dict[str, Any]:
    """List same-day JPG neighbors without applying inference confidence gates.

    Used by unresolved evidence dossiers. Does not invent a source location.
    """
    identity = parse_dji_file_identity(source_basename)
    if identity is None and media_path:
        identity = parse_dji_file_identity(Path(media_path).name)
    if identity is None:
        captured = parse_dji_filename_datetime(source_basename, media_path)
        if captured is None:
            return {
                "source_basename": source_basename,
                "capture_date": None,
                "accepted_inference": False,
                "rejection_reason": "no_dji_identity_or_capture_time",
                "gps_photos": [],
                "non_gps_photos": [],
            }
        identity = DjiFileIdentity(
            stem=normalized_stem(source_basename) or Path(source_basename).stem,
            filename=Path(source_basename).name,
            capture_datetime=captured,
            sequence=-1,
            date=captured.date().isoformat(),
        )

    accepted = infer_jpg_exif_same_shoot(
        source_basename,
        jpg_index=jpg_index,
        media_path=media_path,
    )
    day_photos = list(jpg_index.get(identity.date or "", []))
    gps_ranked: list[dict[str, Any]] = []
    non_gps: list[dict[str, Any]] = []
    for photo in day_photos:
        time_delta = abs(
            (photo.identity.capture_datetime - identity.capture_datetime).total_seconds()
        )
        if time_delta > max_time_delta_s:
            continue
        seq_delta = (
            photo.identity.sequence - identity.sequence
            if identity.sequence >= 0 and photo.identity.sequence >= 0
            else None
        )
        payload = {
            "path": str(photo.path),
            "sequence": photo.identity.sequence,
            "sequence_delta": seq_delta,
            "time_delta_seconds": time_delta,
            "has_gps": photo.has_gps,
            "latitude": photo.latitude,
            "longitude": photo.longitude,
            "camera_model": photo.camera_model,
            "capture_datetime": photo.identity.capture_datetime.isoformat(sep=" "),
            "below_inference_threshold": accepted is None,
        }
        if photo.has_gps:
            gps_ranked.append(payload)
        else:
            non_gps.append(payload)

    gps_ranked.sort(
        key=lambda item: (
            abs(item["sequence_delta"]) if item["sequence_delta"] is not None else 10_000,
            abs(float(item["time_delta_seconds"])),
            item["path"],
        )
    )
    non_gps.sort(
        key=lambda item: (
            abs(item["sequence_delta"]) if item["sequence_delta"] is not None else 10_000,
            abs(float(item["time_delta_seconds"])),
            item["path"],
        )
    )
    rejection = None
    if accepted is None:
        if not day_photos:
            rejection = "no_same_day_jpg_index"
        elif not gps_ranked:
            rejection = "same_day_jpgs_lack_gps_or_outside_time_window"
        else:
            rejection = "nearest_same_day_gps_jpg_below_confidence_threshold"

    return {
        "source_basename": source_basename,
        "capture_date": identity.date,
        "source_sequence": identity.sequence if identity.sequence >= 0 else None,
        "accepted_inference": accepted is not None,
        "accepted_confidence": accepted.confidence if accepted else None,
        "rejection_reason": rejection,
        "gps_photos": gps_ranked[:max_results],
        "non_gps_photos": non_gps[:max_results],
        "same_day_jpg_count": len(day_photos),
        "same_day_gps_jpg_count": sum(1 for item in day_photos if item.has_gps),
    }


def find_dji_sequence_neighbors(
    *,
    source_basename: str,
    media_path: str | None,
    media_roots: Iterable[Path],
    sequence_window: int = 25,
    max_results: int = 40,
) -> list[dict[str, Any]]:
    """Find nearby DJI sequence siblings on disk (video/still), dossier-only."""
    identity = parse_dji_file_identity(source_basename)
    if identity is None and media_path:
        identity = parse_dji_file_identity(Path(media_path).name)
    if identity is None or identity.sequence < 0 or not identity.date:
        return []

    neighbors: list[dict[str, Any]] = []
    seen: set[str] = set()

    def consider(directory: Path, filename: str) -> None:
        other = parse_dji_file_identity(filename)
        if other is None or other.date != identity.date:
            return
        seq_delta = other.sequence - identity.sequence
        if abs(seq_delta) == 0 or abs(seq_delta) > sequence_window:
            return
        path = str(directory / filename)
        if path in seen:
            return
        seen.add(path)
        suffix = Path(filename).suffix.lower()
        neighbors.append(
            {
                "path": path,
                "filename": filename,
                "sequence": other.sequence,
                "sequence_delta": seq_delta,
                "kind": (
                    "still"
                    if suffix in JPG_EXTENSIONS
                    else ("video" if suffix in {".mp4", ".mov", ".mts", ".m4v"} else "other")
                ),
                "capture_datetime": other.capture_datetime.isoformat(sep=" "),
            }
        )

    # Prefer the source's sibling directory (cheap + most relevant).
    if media_path:
        parent = Path(media_path).expanduser()
        try:
            parent = parent.resolve().parent
        except OSError:
            parent = Path(media_path).parent
        if parent.is_dir():
            try:
                for filename in os.listdir(parent):
                    consider(parent, filename)
            except OSError:
                pass

    # If the sibling dir was empty/missing, scan media roots for same-date DJI files.
    if not neighbors:
        date_compact = identity.date.replace("-", "")
        for root in media_roots:
            root_path = Path(root)
            if not root_path.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root_path):
                dirnames[:] = prune_media_walk_dirnames(dirpath, dirnames)
                for filename in filenames:
                    if filename.startswith("._"):
                        continue
                    if date_compact not in filename:
                        continue
                    consider(Path(dirpath), filename)
                if len(neighbors) >= max_results * 2:
                    break
            if len(neighbors) >= max_results:
                break

    neighbors.sort(key=lambda item: (abs(int(item["sequence_delta"])), item["path"]))
    return neighbors[:max_results]


def read_jpeg_exif_gps(
    path: Path,
) -> tuple[float | None, float | None, float | None, str | None, datetime | None]:
    """Return (lat, lon, alt, camera_model, exif_datetime) from JPEG EXIF."""
    try:
        data = path.read_bytes()
    except OSError:
        return None, None, None, None, None
    return parse_jpeg_exif_gps_bytes(data)


def parse_jpeg_exif_gps_bytes(
    data: bytes,
) -> tuple[float | None, float | None, float | None, str | None, datetime | None]:
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None, None, None, None, None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            break
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD9, 0xDA}:  # EOI / SOS
            break
        if offset + 2 > len(data):
            break
        size = struct.unpack(">H", data[offset : offset + 2])[0]
        if size < 2 or offset + size > len(data):
            break
        segment = data[offset + 2 : offset + size]
        offset += size
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            return _parse_exif_tiff(segment[6:])
    return None, None, None, None, None


def _parse_exif_tiff(
    tiff: bytes,
) -> tuple[float | None, float | None, float | None, str | None, datetime | None]:
    if len(tiff) < 8:
        return None, None, None, None, None
    endian = tiff[0:2]
    if endian == b"II":
        endian_fmt = "<"
    elif endian == b"MM":
        endian_fmt = ">"
    else:
        return None, None, None, None, None
    try:
        magic = struct.unpack(endian_fmt + "H", tiff[2:4])[0]
        ifd0 = struct.unpack(endian_fmt + "I", tiff[4:8])[0]
    except struct.error:
        return None, None, None, None, None
    if magic != 42:
        return None, None, None, None, None

    entries = _read_ifd_entries(tiff, ifd0, endian_fmt)
    model = _exif_string(tiff, entries.get(0x0110), endian_fmt)
    dt_raw = _exif_string(tiff, entries.get(0x9003) or entries.get(0x0132), endian_fmt)
    exif_dt = _parse_exif_datetime(dt_raw)

    gps_ptr = entries.get(0x8825)
    if not gps_ptr:
        return None, None, None, model, exif_dt
    gps_offset = _exif_long(gps_ptr, endian_fmt)
    if gps_offset is None:
        return None, None, None, model, exif_dt
    gps_entries = _read_ifd_entries(tiff, gps_offset, endian_fmt)
    lat = _gps_coordinate(
        tiff,
        gps_entries.get(2),
        gps_entries.get(1),
        endian_fmt,
    )
    lon = _gps_coordinate(
        tiff,
        gps_entries.get(4),
        gps_entries.get(3),
        endian_fmt,
    )
    alt = _gps_altitude(tiff, gps_entries.get(6), gps_entries.get(5), endian_fmt)
    if not is_usable_gps(lat, lon):
        lat, lon = None, None
    return lat, lon, alt, model, exif_dt


def _read_ifd_entries(tiff: bytes, offset: int, endian_fmt: str) -> dict[int, bytes]:
    if offset < 0 or offset + 2 > len(tiff):
        return {}
    try:
        count = struct.unpack(endian_fmt + "H", tiff[offset : offset + 2])[0]
    except struct.error:
        return {}
    entries: dict[int, bytes] = {}
    cursor = offset + 2
    for _ in range(count):
        if cursor + 12 > len(tiff):
            break
        tag = struct.unpack(endian_fmt + "H", tiff[cursor : cursor + 2])[0]
        entries[tag] = tiff[cursor : cursor + 12]
        cursor += 12
    return entries


def _exif_long(entry: bytes | None, endian_fmt: str) -> int | None:
    if entry is None or len(entry) < 12:
        return None
    typ, count = struct.unpack(endian_fmt + "HH", entry[2:6])
    value = entry[8:12]
    if typ == 4 and count == 1:
        return struct.unpack(endian_fmt + "I", value)[0]
    if typ == 3 and count == 1:
        return struct.unpack(endian_fmt + "H", value[:2])[0]
    return struct.unpack(endian_fmt + "I", value)[0]


def _exif_string(tiff: bytes, entry: bytes | None, endian_fmt: str) -> str | None:
    if entry is None or len(entry) < 12:
        return None
    typ, count = struct.unpack(endian_fmt + "HH", entry[2:6])
    if typ != 2 or count < 1:
        return None
    if count <= 4:
        raw = entry[8 : 8 + count]
    else:
        offset = struct.unpack(endian_fmt + "I", entry[8:12])[0]
        raw = tiff[offset : offset + count]
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip() or None


def _gps_coordinate(
    tiff: bytes,
    value_entry: bytes | None,
    ref_entry: bytes | None,
    endian_fmt: str,
) -> float | None:
    if value_entry is None or ref_entry is None:
        return None
    typ, count = struct.unpack(endian_fmt + "HH", value_entry[2:6])
    if typ != 5 or count != 3:
        return None
    offset = struct.unpack(endian_fmt + "I", value_entry[8:12])[0]
    try:
        nums = struct.unpack(endian_fmt + "IIIIII", tiff[offset : offset + 24])
    except struct.error:
        return None
    parts = []
    for index in range(0, 6, 2):
        denom = nums[index + 1]
        if denom == 0:
            return None
        parts.append(nums[index] / denom)
    degrees = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    ref = _exif_string(tiff, ref_entry, endian_fmt) or ""
    if ref.upper() in {"S", "W"}:
        degrees = -degrees
    return degrees


def _gps_altitude(
    tiff: bytes,
    value_entry: bytes | None,
    ref_entry: bytes | None,
    endian_fmt: str,
) -> float | None:
    if value_entry is None:
        return None
    typ, count = struct.unpack(endian_fmt + "HH", value_entry[2:6])
    if typ != 5 or count != 1:
        return None
    offset = struct.unpack(endian_fmt + "I", value_entry[8:12])[0]
    try:
        num, den = struct.unpack(endian_fmt + "II", tiff[offset : offset + 8])
    except struct.error:
        return None
    if den == 0:
        return None
    alt = num / den
    if ref_entry is not None:
        ref = ref_entry[8]
        if ref == 1:
            alt = -alt
    return alt


def _parse_exif_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
