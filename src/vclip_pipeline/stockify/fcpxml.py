"""Read, inspect, validate, repair, and write Final Cut Pro XML structures."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import unquote, urlparse

from .constants import (
    DTD_FORBIDDEN_ATTRS,
    RESOURCE_REF_TAGS,
    STILL_IMAGE_EXTENSIONS,
    SUPPORTED_STOCK_SEGMENT_TAGS,
)
from .core import format_time, local_name, parse_time
from .models import ResourcesReport, RunReport, StockifyError, ValidationReport


# XML traversal and metadata helpers

# Add or update VClip metadata on a cleaned clip.
def add_vclip_metadata(clip: ET.Element, values: dict[str, str]) -> None:
    metadata = first_direct_child(clip, "metadata")
    if metadata is None:
        metadata = ET.SubElement(clip, "metadata")

    existing = {
        md.get("key"): md
        for md in direct_children_by_name(metadata, "md")
        if md.get("key")
    }
    for key, value in values.items():
        md = existing.get(key)
        if md is None:
            md = ET.SubElement(metadata, "md", {"key": key})
        md.set("value", value)


# Read metadata embedded by Stockify from a clip.
def read_vclip_metadata(clip: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    metadata = first_direct_child(clip, "metadata")
    if metadata is None:
        return values
    for md in direct_children_by_name(metadata, "md"):
        key = md.get("key")
        value = md.get("value")
        if key and value is not None:
            values[key] = value
    return values


INGEST_DATE_KEY = "com.apple.proapps.mio.ingestDate"
_INGEST_DATE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})(?:\s+([+-]\d{2}):?(\d{2}))?$"
)


# Read Final Cut's asset ingest timestamp for SRT disambiguation only.
def asset_ingest_datetime(asset: ET.Element | None) -> datetime | None:
    if asset is None:
        return None
    metadata = first_direct_child(asset, "metadata")
    if metadata is None:
        return None
    raw: str | None = None
    for md in direct_children_by_name(metadata, "md"):
        if md.get("key") == INGEST_DATE_KEY:
            raw = md.get("value")
            break
    if not raw:
        return None
    match = _INGEST_DATE_RE.match(raw.strip())
    if not match:
        return None
    stamp = f"{match.group(1)} {match.group(2)}"
    try:
        parsed = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    # Keep timezone-aware when Final Cut provides an offset; callers may drop it.
    if match.group(3) is not None and match.group(4) is not None:
        offset = match.group(3) + match.group(4)
        try:
            aware = datetime.strptime(
                f"{stamp} {offset}",
                "%Y-%m-%d %H:%M:%S %z",
            )
            return aware
        except ValueError:
            return parsed
    return parsed


# Attribute keys that Final Cut freely rewrites on import/export.
_UNSTABLE_TREATMENT_ATTRS = frozenset({"ref", "uid", "id"})


# Hash only the visual treatment carried by a timeline clip.
def video_treatment_signature(clip: ET.Element) -> str:
    """Return a stable hash of review-relevant video treatment.

    Compares semantic effect state rather than raw XML serialization. Generated
    resource refs/IDs, child ordering, and whitespace-only formatting differences
    from Final Cut round-trips are ignored. Custom LUT identity and parameters
    (name/key/value and effectConfig payloads) remain part of the fingerprint.
    """

    def canonical_node(node: ET.Element) -> dict[str, object]:
        tag = local_name(node.tag)
        attrs = sorted(
            (key, value)
            for key, value in node.attrib.items()
            if key not in _UNSTABLE_TREATMENT_ATTRS
        )
        raw_text = (node.text or "").strip()
        if tag == "data":
            # effectConfig and similar blobs are base64; drop transport whitespace.
            text = re.sub(r"\s+", "", raw_text)
        else:
            text = re.sub(r"\s+", " ", raw_text)
        children = [canonical_node(child) for child in list(node)]
        children.sort(
            key=lambda child: json.dumps(child, sort_keys=True, ensure_ascii=False)
        )
        return {
            "tag": tag,
            "attributes": attrs,
            "text": text,
            "children": children,
        }

    treatment_nodes: list[dict[str, object]] = []
    for child in list(clip):
        tag = local_name(child.tag)
        if tag == "filter-video" or tag.startswith("adjust-") or tag in {
            "video-animation",
            "conform-rate",
        }:
            treatment_nodes.append(canonical_node(child))
    treatment_nodes.sort(
        key=lambda node: json.dumps(node, sort_keys=True, ensure_ascii=False)
    )
    serialized = json.dumps(
        treatment_nodes,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


# Read useful dimensions and frame-rate data from a format resource.
def format_metadata(resource_index: dict[str, ET.Element], format_id: str) -> dict[str, str | int | float | None]:
    resource = resource_index.get(format_id)
    if resource is None or local_name(resource.tag) != "format":
        return {
            "format_id": format_id,
            "width": None,
            "height": None,
            "frame_duration": None,
            "timecode_fps": 30,
        }

    frame_duration = resource.get("frameDuration")
    fps = 30
    if frame_duration:
        try:
            duration = parse_time(frame_duration)
            if duration > 0:
                fps = max(1, int(round(1 / float(duration))))
        except ValueError:
            fps = 30

    def int_or_none(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    return {
        "format_id": format_id,
        "format_name": resource.get("name"),
        "width": int_or_none(resource.get("width")),
        "height": int_or_none(resource.get("height")),
        "frame_duration": frame_duration,
        "timecode_fps": fps,
    }


# Convert a timeline offset into CommandPost-style timecode text.
def format_project_timecode(offset: Fraction, fps: int) -> str:
    frames = int(round(float(offset) * fps))
    frames_per_hour = fps * 60 * 60
    frames_per_minute = fps * 60
    hours, frames = divmod(frames, frames_per_hour)
    minutes, frames = divmod(frames, frames_per_minute)
    seconds, frame = divmod(frames, fps)
    return f"{hours:02d}{minutes:02d}{seconds:02d}{frame:02d}"


# Yield only direct children with a matching local tag name.
def direct_children_by_name(element: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in list(element):
        if local_name(child.tag) == name:
            yield child


# Return the first direct child with a matching tag.
def first_direct_child(element: ET.Element, name: str) -> ET.Element | None:
    return next(direct_children_by_name(element, name), None)


# Yield descendants whose local tag is in the requested set.
def descendants_by_name(element: ET.Element, names: set[str]) -> Iterator[ET.Element]:
    for node in element.iter():
        if local_name(node.tag) in names:
            yield node


# Return matching direct children as a list.
def direct_children_with_name(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if local_name(child.tag) == name]


# Resource and asset helpers

# Index top-level resources by their FCPXML ID.
def build_resource_index(resources: ET.Element) -> dict[str, ET.Element]:
    index: dict[str, ET.Element] = {}
    for child in list(resources):
        resource_id = child.get("id")
        if resource_id:
            index[resource_id] = child
    return index


# Count how many times each resource ID appears.
def resource_id_counts(resources: ET.Element) -> Counter[str]:
    return Counter(
        child.get("id")
        for child in list(resources)
        if child.get("id")
    )


# List resource IDs that appear more than once.
def duplicate_resource_ids(resources: ET.Element) -> list[str]:
    return sorted(
        resource_id
        for resource_id, count in resource_id_counts(resources).items()
        if count > 1
    )


# Return the set of declared resource IDs.
def resource_ids(resources: ET.Element) -> set[str]:
    return {
        child.get("id", "")
        for child in list(resources)
        if child.get("id")
    }


# Check whether an asset has the required media-rep child.
def asset_has_media_rep(asset: ET.Element | None) -> bool:
    return bool(asset is not None and first_direct_child(asset, "media-rep") is not None)


# Count direct media-rep children on an asset.
def asset_media_rep_count(asset: ET.Element) -> int:
    return sum(1 for child in list(asset) if local_name(child.tag) == "media-rep")


# Report invalid or unexpected direct asset children.
def asset_child_order_errors(asset: ET.Element) -> list[str]:
    names = [local_name(child.tag) for child in list(asset)]
    errors: list[str] = []
    if "media-rep" not in names:
        errors.append("required media-rep missing")
    metadata_seen = False
    media_rep_seen = False
    for name in names:
        if name == "media-rep":
            media_rep_seen = True
            if metadata_seen:
                errors.append("media-rep appears after metadata")
        elif name == "metadata":
            if not media_rep_seen:
                errors.append("metadata appears before media-rep")
            if metadata_seen:
                errors.append("multiple metadata children")
            metadata_seen = True
        else:
            errors.append(f"unexpected direct asset child {name!r}")
    return errors


# Collect file extensions advertised by an asset.
def asset_source_extensions(asset: ET.Element) -> set[str]:
    values = [asset.get("name", "")]
    for media_rep in direct_children_by_name(asset, "media-rep"):
        values.append(media_rep.get("src", ""))

    extensions: set[str] = set()
    for value in values:
        if not value:
            continue
        parsed = urlparse(value)
        path = unquote(parsed.path or value)
        suffix = Path(path).suffix.lower()
        if suffix:
            extensions.add(suffix)
    return extensions


# Convert a local file URL or path string into a Path.
def file_url_to_path(value: str | None) -> Path | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    raw_path = parsed.path if parsed.scheme == "file" else value
    if not raw_path:
        return None
    return Path(unquote(raw_path))


# Return every local media path referenced by an asset.
def asset_media_paths(asset: ET.Element) -> list[Path]:
    paths: list[Path] = []
    for media_rep in direct_children_by_name(asset, "media-rep"):
        path = file_url_to_path(media_rep.get("src"))
        if path is not None:
            paths.append(path)
    return paths


# Return the first referenced media file that exists.
def existing_asset_media_path(asset: ET.Element | None) -> Path | None:
    if asset is None:
        return None
    for path in asset_media_paths(asset):
        if path.is_file():
            return path
    return None


# Resolve a clip ref to its asset resource.
def referenced_asset(
    clip: ET.Element,
    resource_index: dict[str, ET.Element],
) -> ET.Element | None:
    ref = clip.get("ref")
    resource = resource_index.get(ref or "")
    if resource is not None and local_name(resource.tag) == "asset":
        return resource
    return None


# Check whether an asset declares usable video.
def asset_has_video(asset: ET.Element | None) -> bool:
    if asset is None:
        return False
    return asset.get("hasVideo", "0") == "1" and asset.get("videoSources") not in {
        None,
        "0",
    }


# Detect still-image assets by extension.
def asset_is_still_image(asset: ET.Element | None) -> bool:
    if asset is None:
        return False
    return bool(asset_source_extensions(asset) & STILL_IMAGE_EXTENSIONS)


# Build the fields used to match malformed assets to valid copies.
def replacement_key(asset: ET.Element) -> tuple[str | None, str | None, str | None, str | None]:
    return (
        asset.get("name"),
        asset.get("duration"),
        asset.get("format"),
        asset.get("customLUTOverride"),
    )


# Find safe one-to-one replacements for malformed assets.
def build_malformed_asset_replacement_map(
    assets: Iterable[ET.Element],
) -> dict[str, str]:
    valid_by_key: dict[tuple[str | None, str | None, str | None, str | None], list[str]] = {}
    malformed_assets: list[ET.Element] = []
    for asset in assets:
        resource_id = asset.get("id")
        if not resource_id:
            continue
        if asset_has_media_rep(asset):
            valid_by_key.setdefault(replacement_key(asset), []).append(resource_id)
        else:
            malformed_assets.append(asset)

    replacements: dict[str, str] = {}
    for asset in malformed_assets:
        resource_id = asset.get("id")
        candidates = valid_by_key.get(replacement_key(asset), [])
        if resource_id and len(candidates) == 1:
            replacements[resource_id] = candidates[0]
    return replacements


# Check whether an element contains a descendant with a given tag.
def has_descendant_named(element: ET.Element, name: str) -> bool:
    return any(
        node is not element and local_name(node.tag) == name
        for node in element.iter()
    )


# List the video effects applied directly to a clip.
def video_effect_names(
    clip: ET.Element,
    resource_index: dict[str, ET.Element] | None = None,
) -> list[str]:
    names: list[str] = []
    for child in list(clip):
        if local_name(child.tag) == "filter-video":
            ref = child.get("ref")
            resource_name = None
            if resource_index is not None and ref:
                resource = resource_index.get(ref)
                if resource is not None:
                    resource_name = resource.get("name")
            names.append(
                child.get("name")
                or resource_name
                or ref
                or "Unnamed Video Effect"
            )
    return names


# Detect whether an effect list contains a LUT effect.
def has_custom_lut_effect(effect_names: Iterable[str]) -> bool:
    return any("lut" in effect_name.lower() for effect_name in effect_names)


# Detect a Custom LUT filter-video node by name or effect resource.
def is_custom_lut_filter(
    filter_video: ET.Element,
    resource_index: dict[str, ET.Element] | None = None,
) -> bool:
    name = filter_video.get("name") or ""
    if "lut" in name.lower():
        return True
    ref = filter_video.get("ref")
    if resource_index is not None and ref:
        resource = resource_index.get(ref)
        resource_name = resource.get("name") if resource is not None else None
        if resource_name and "lut" in resource_name.lower():
            return True
    return False


# Read camera conversion LUT metadata from an asset.
def asset_conversion_lut(asset: ET.Element | None) -> str | None:
    if asset is None:
        return None
    value = asset.get("customLUTOverride")
    if value:
        return value
    # Defensive fallback for metadata-based exports.
    for md in descendants_by_name(asset, {"md"}):
        key = (md.get("key") or "").lower()
        value = md.get("value")
        if value and ("lut" in key or "logconversion" in key):
            return f"{key}={value}"
    return None


# Collect every resource ID referenced by generated XML.
def collect_resource_refs(root: ET.Element) -> set[str]:
    refs: set[str] = set()
    for node in root.iter():
        tag = local_name(node.tag)
        ref = node.get("ref")
        if tag in RESOURCE_REF_TAGS and ref:
            refs.add(ref)
        if tag == "sequence" and node.get("format"):
            refs.add(node.get("format", ""))
    return refs


# Drop <effect> resources that nothing in the generated XML still references.
def prune_unreferenced_effect_resources(root: ET.Element) -> list[str]:
    resources = first_direct_child(root, "resources")
    if resources is None:
        return []
    referenced = collect_resource_refs(root)
    removed: list[str] = []
    for child in list(resources):
        if local_name(child.tag) != "effect":
            continue
        resource_id = child.get("id")
        if resource_id and resource_id not in referenced:
            resources.remove(child)
            removed.append(resource_id)
    return removed


# Copy the source resources without mutating the input tree.
def clone_resources(source_root: ET.Element) -> ET.Element:
    source_resources = first_direct_child(source_root, "resources")
    if source_resources is None:
        raise StockifyError("Input FCPXML does not contain <resources>.")
    return copy.deepcopy(source_resources)


# Return the output resources element or fail clearly.
def output_resources(root: ET.Element) -> ET.Element:
    resources = first_direct_child(root, "resources")
    if resources is None:
        raise StockifyError("FCPXML does not contain <resources>.")
    return resources


# Compare source and output resource tables.
def resource_report(
    source_resources: ET.Element,
    generated_resources: ET.Element,
) -> ResourcesReport:
    source_resource_ids = resource_ids(source_resources)
    generated_resource_ids = resource_ids(generated_resources)
    source_assets = [
        child for child in list(source_resources)
        if local_name(child.tag) == "asset"
    ]
    generated_assets = [
        child for child in list(generated_resources)
        if local_name(child.tag) == "asset"
    ]
    return ResourcesReport(
        source_count=len(list(source_resources)),
        output_count=len(list(generated_resources)),
        source_assets=len(source_assets),
        output_assets=len(generated_assets),
        assets_with_conversion_lut=sum(
            1 for asset in source_assets if asset_conversion_lut(asset)
        ),
        source_assets_missing_media_rep=sum(
            1 for asset in source_assets if not asset_has_media_rep(asset)
        ),
        output_assets_missing_media_rep=sum(
            1 for asset in generated_assets if not asset_has_media_rep(asset)
        ),
        missing_resource_ids=sorted(source_resource_ids - generated_resource_ids),
        duplicate_resource_ids=duplicate_resource_ids(generated_resources),
    )


# Remove malformed assets that no output project uses.
def drop_unreferenced_malformed_assets(
    resources: ET.Element,
    root: ET.Element,
    report: RunReport,
) -> None:
    referenced_ids = collect_resource_refs(root)
    for asset in list(resources):
        if local_name(asset.tag) != "asset" or asset_has_media_rep(asset):
            continue
        resource_id = asset.get("id")
        if resource_id and resource_id in referenced_ids:
            continue
        resources.remove(asset)
        if resource_id:
            report.resources.dropped_malformed_asset_ids.append(resource_id)


# Parsing, validation, and diagnostics

# Run conservative structural checks before Final Cut import.
def validate_fcpxml(root: ET.Element) -> ValidationReport:
    report = ValidationReport()

    if local_name(root.tag) != "fcpxml":
        report.errors.append("Root element is not <fcpxml>.")
        return report

    resources_nodes = direct_children_with_name(root, "resources")
    library_nodes = direct_children_with_name(root, "library")

    if len(resources_nodes) != 1:
        report.errors.append(
            f"Expected exactly one direct <resources> element, found {len(resources_nodes)}."
        )
    if len(library_nodes) != 1:
        report.errors.append(
            f"Expected exactly one direct <library> element, found {len(library_nodes)}."
        )
    if not resources_nodes:
        return report

    resources = resources_nodes[0]
    resource_index = build_resource_index(resources)

    for resource_id in duplicate_resource_ids(resources):
        report.errors.append(f"Duplicate resource id {resource_id}.")

    for asset in direct_children_by_name(resources, "asset"):
        resource_id = asset.get("id", "<missing id>")
        for error in asset_child_order_errors(asset):
            report.errors.append(f"Asset {resource_id}: {error}.")

    for node in root.iter():
        tag = local_name(node.tag)
        for attr in node.attrib:
            if (tag, attr) in DTD_FORBIDDEN_ATTRS:
                report.errors.append(
                    f"No DTD declaration for attribute {attr} of element {tag}."
                )
        ref = node.get("ref")
        if tag in RESOURCE_REF_TAGS and ref and ref not in resource_index:
            name = node.get("name") or node.get("id") or tag
            report.errors.append(
                f"{tag} {name!r} references unknown resource {ref}."
            )
        if tag == "sequence":
            fmt = node.get("format")
            if not fmt:
                report.errors.append("A sequence is missing its format reference.")
            elif fmt not in resource_index:
                report.errors.append(
                    f"A sequence references unknown format resource {fmt}."
                )
            try:
                if parse_time(node.get("duration")) <= 0:
                    report.errors.append("A sequence has non-positive duration.")
            except ValueError as exc:
                report.errors.append(str(exc))

    for project in descendants_by_name(root, {"project"}):
        project_name = project.get("name", "<unnamed>")
        sequence = first_direct_child(project, "sequence")
        if sequence is None:
            report.errors.append(f"Project {project_name!r} has no sequence.")
            continue
        spine = first_direct_child(sequence, "spine")
        if spine is None:
            report.errors.append(f"Project {project_name!r} has no primary spine.")
            continue
        primary_segments = [
            child for child in list(spine)
            if local_name(child.tag) in SUPPORTED_STOCK_SEGMENT_TAGS
        ]
        is_individual_stock_project = (
            " - Clip " in project_name
            or " — Clip " in project_name
        )
        if is_individual_stock_project and len(primary_segments) != 1:
            report.errors.append(
                f"Stock project {project_name!r} has {len(primary_segments)} "
                "primary stock segments; expected exactly one."
            )
        for segment in primary_segments:
            try:
                if parse_time(segment.get("duration")) <= 0:
                    report.errors.append(
                        f"Segment in project {project_name!r} has non-positive duration."
                    )
            except ValueError as exc:
                report.errors.append(str(exc))

    report.passed = not report.errors
    return report


# Parse an input file and verify its required top-level structure.
def parse_source(path: Path) -> ET.ElementTree:
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise StockifyError(f"Input is not valid XML: {exc}") from exc
    root = tree.getroot()
    if local_name(root.tag) != "fcpxml":
        raise StockifyError("Input root element is not <fcpxml>.")
    if first_direct_child(root, "resources") is None:
        raise StockifyError("Input FCPXML does not contain <resources>.")
    if first_direct_child(root, "library") is None and not any(
        local_name(node.tag) == "event" for node in root.iter()
    ):
        raise StockifyError("Input FCPXML contains neither a <library> nor an <event>.")
    return tree


# Resolve a file or FCPXML bundle to the actual XML file.
def resolve_input_fcpxml(path: Path) -> tuple[Path, list[str]]:
    messages: list[str] = []
    if path.is_dir():
        info_path = path / "Info.fcpxml"
        if not info_path.is_file():
            raise StockifyError(f"FCPXML bundle lacks Info.fcpxml: {path}")
        messages.extend([
            "Resolved FCPXML bundle:",
            f"  {path}",
            "Using:",
            f"  {info_path}",
        ])
        return info_path, messages
    if path.is_file():
        return path, messages
    raise StockifyError(f"Input path is neither a file nor a directory: {path}")


# Print a focused report about malformed asset resources.
def print_asset_diagnostics(path: Path) -> int:
    tree = parse_source(path)
    root = tree.getroot()
    resources = output_resources(root)
    assets = [
        child for child in list(resources)
        if local_name(child.tag) == "asset"
    ]
    missing = [asset for asset in assets if not asset_has_media_rep(asset)]
    with_metadata = [
        asset for asset in assets
        if first_direct_child(asset, "metadata") is not None
    ]
    with_lut = [asset for asset in assets if asset_conversion_lut(asset)]
    multiple_media_rep = [
        asset for asset in assets
        if asset_media_rep_count(asset) > 1
    ]

    print("Asset diagnostics")
    print("-----------------")
    print(f"Total assets:                  {len(assets)}")
    print(f"Assets with media-rep:         {len(assets) - len(missing)}")
    print(f"Assets missing media-rep:      {len(missing)}")
    print(f"Assets with metadata:          {len(with_metadata)}")
    print(f"Assets with conversion LUT:    {len(with_lut)}")
    print(f"Assets with multiple media-rep: {len(multiple_media_rep)}")
    if missing:
        print()
    for asset in missing:
        resource_id = asset.get("id", "<missing id>")
        print(f"Asset {resource_id}:")
        print(f"  name: {asset.get('name')}")
        children = ", ".join(local_name(child.tag) for child in list(asset)) or "<none>"
        print(f"  children: {children}")
        print("  error: required media-rep missing")
    return 1 if missing else 0


# Print validation results for an existing FCPXML file.
def print_validation_report(path: Path) -> int:
    tree = parse_source(path)
    validation = validate_fcpxml(tree.getroot())
    print("FCPXML validation")
    print("-----------------")
    print(f"Input:  {path}")
    print(f"Passed: {str(validation.passed).lower()}")
    if validation.errors:
        print()
        print("Errors:")
        for error in validation.errors:
            print(f"  - {error}")
    if validation.warnings:
        print()
        print("Warnings:")
        for warning in validation.warnings:
            print(f"  - {warning}")
    return 0 if validation.passed else 1


# Project creation and XML output

# Create a project and lay its clips sequentially on a clean spine.
def make_project(
    *,
    project_name: str,
    project_uid: str,
    sequence_format: str,
    sequence_tc_format: str,
    sequence_audio_layout: str,
    sequence_audio_rate: str,
    clips: list[ET.Element],
) -> ET.Element:
    project = ET.Element(
        "project",
        {
            "name": project_name,
            "uid": project_uid,
        },
    )

    total_duration = sum(
        (parse_time(clip.get("duration")) for clip in clips),
        start=Fraction(0),
    )

    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": sequence_format,
            "duration": format_time(total_duration),
            "tcStart": "0s",
            "tcFormat": sequence_tc_format,
            "audioLayout": sequence_audio_layout,
            "audioRate": sequence_audio_rate,
        },
    )
    spine = ET.SubElement(sequence, "spine")

    offset = Fraction(0)
    for clip in clips:
        clip.set("offset", format_time(offset))
        spine.append(clip)
        offset += parse_time(clip.get("duration"))

    return project


# Read the sequence settings needed for a new project.
def sequence_settings(sequence: ET.Element) -> tuple[str, str, str, str]:
    fmt = sequence.get("format")
    if not fmt:
        raise StockifyError("Source sequence is missing its format reference.")
    return (
        fmt,
        sequence.get("tcFormat", "NDF"),
        sequence.get("audioLayout", "stereo"),
        sequence.get("audioRate", "48k"),
    )


# Return source events from library or event-only XML.
def iter_source_events(root: ET.Element) -> Iterable[ET.Element]:
    library = next(
        (node for node in root.iter() if local_name(node.tag) == "library"),
        None,
    )
    if library is None:
        # Event-only XML is also accepted.
        return [
            node for node in root.iter()
            if local_name(node.tag) == "event"
        ]
    return [
        child for child in list(library)
        if local_name(child.tag) == "event"
    ]


# Pretty-print XML, including a fallback for Python 3.8.
def indent_xml(element: ET.Element) -> None:
    try:
        ET.indent(element, space="    ")
    except AttributeError:
        # Python 3.8 fallback.
        def _indent(node: ET.Element, level: int = 0) -> None:
            whitespace = "\n" + "    " * level
            child_whitespace = "\n" + "    " * (level + 1)
            if len(node):
                if not node.text or not node.text.strip():
                    node.text = child_whitespace
                for child in node:
                    _indent(child, level + 1)
                if not node[-1].tail or not node[-1].tail.strip():
                    node[-1].tail = whitespace
            if level and (not node.tail or not node.tail.strip()):
                node.tail = whitespace
        _indent(element)


# Write formatted XML with the Final Cut doctype.
def write_fcpxml(root: ET.Element, output_path: Path) -> None:
    indent_xml(root)
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    doctype = b'<!DOCTYPE fcpxml>\n'
    if xml_bytes.startswith(b"<?xml"):
        declaration_end = xml_bytes.find(b"?>") + 2
        xml_bytes = (
            xml_bytes[:declaration_end]
            + b"\n"
            + doctype
            + xml_bytes[declaration_end:].lstrip(b"\n")
        )
    else:
        xml_bytes = doctype + xml_bytes
    output_path.write_bytes(xml_bytes)
