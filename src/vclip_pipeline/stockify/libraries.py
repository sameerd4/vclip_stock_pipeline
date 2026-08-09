"""Resolve and list Final Cut Pro libraries for processed-library tracking."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

from .fcpxml import first_direct_child, local_name


def resolve_source_library(
    *,
    requested_path: Path | None,
    input_path: Path | None = None,
    source_root: ET.Element | None = None,
) -> tuple[str, str] | None:
    """Return `(library_name, library_path)` when a source .fcpbundle can be determined.

    Resolution order:
    1. `<library location="file:///.../Name.fcpbundle/">` from the source XML
    2. A `.fcpbundle` ancestor of the requested input path
    3. A `.fcpbundle` ancestor of the resolved Info.fcpxml path
    """
    candidates: list[Path] = []
    if source_root is not None:
        from_xml = library_path_from_xml(source_root)
        if from_xml is not None:
            candidates.append(from_xml)
    if requested_path is not None:
        from_requested = fcpbundle_ancestor(requested_path)
        if from_requested is not None:
            candidates.append(from_requested)
    if input_path is not None:
        from_input = fcpbundle_ancestor(input_path)
        if from_input is not None:
            candidates.append(from_input)

    for candidate in candidates:
        normalized = normalize_fcpbundle_path(candidate)
        if normalized is not None:
            return normalized.name, str(normalized)
    return None


def library_path_from_xml(source_root: ET.Element) -> Path | None:
    library = first_direct_child(source_root, "library")
    if library is None:
        for node in source_root.iter():
            if local_name(node.tag) == "library":
                library = node
                break
    if library is None:
        return None
    location = library.get("location")
    if not location:
        return None
    return path_from_file_url(location)


def path_from_file_url(location: str) -> Path | None:
    parsed = urlparse(location.strip())
    if parsed.scheme and parsed.scheme != "file":
        return None
    raw_path = unquote(parsed.path or "")
    if not raw_path:
        return None
    # file:///Volumes/... → /Volumes/...
    path = Path(raw_path)
    return fcpbundle_ancestor(path) or (
        normalize_fcpbundle_path(path) if _looks_like_fcpbundle(path) else None
    )


def fcpbundle_ancestor(path: Path) -> Path | None:
    try:
        resolved = path.expanduser()
    except OSError:
        resolved = path
    current = resolved if resolved.suffix == ".fcpbundle" else resolved.parent
    for candidate in [resolved, current, *list(current.parents)]:
        if _looks_like_fcpbundle(candidate):
            return normalize_fcpbundle_path(candidate)
    return None


def normalize_fcpbundle_path(path: Path) -> Path | None:
    if not _looks_like_fcpbundle(path):
        return None
    try:
        return path.expanduser().resolve()
    except OSError:
        text = str(path.expanduser())
        while text.endswith(os.sep):
            text = text[: -len(os.sep)]
        return Path(text)


def _looks_like_fcpbundle(path: Path) -> bool:
    name = path.name.rstrip("/")
    return name.endswith(".fcpbundle")


def find_fcpbundles(root: Path) -> list[Path]:
    """Find `.fcpbundle` packages under root without descending into them."""
    root = root.expanduser().resolve()
    if _looks_like_fcpbundle(root):
        normalized = normalize_fcpbundle_path(root)
        return [normalized] if normalized is not None else []
    if not root.is_dir():
        return []

    found: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        keep: list[str] = []
        for name in dirnames:
            child = Path(dirpath) / name
            if _looks_like_fcpbundle(child):
                normalized = normalize_fcpbundle_path(child)
                if normalized is not None:
                    found.append(normalized)
            elif name.endswith(".fcpxmld"):
                # Exported XML bundles are not Final Cut libraries.
                continue
            else:
                keep.append(name)
        dirnames[:] = keep
    return sorted(found, key=lambda path: path.name.lower())


def find_fcpxml_exports(xml_dir: Path) -> list[Path]:
    """Find `.fcpxml` files and `.fcpxmld` bundles under xml_dir."""
    root = xml_dir.expanduser().resolve()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        keep: list[str] = []
        for name in dirnames:
            child = Path(dirpath) / name
            if _looks_like_fcpbundle(child):
                continue
            if name.endswith(".fcpxmld"):
                info = child / "Info.fcpxml"
                if info.is_file():
                    found.append(info)
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            if name.endswith(".fcpxml") and name != "Info.fcpxml":
                found.append(Path(dirpath) / name)
            elif name == "Info.fcpxml" and not Path(dirpath).name.endswith(".fcpxmld"):
                # Loose Info.fcpxml outside a bundle — uncommon, still count it.
                found.append(Path(dirpath) / name)
    return sorted(found)


def discover_xml_library_names(xml_dir: Path) -> set[str]:
    """Return `.fcpbundle` names that have an exported XML in xml_dir."""
    names: set[str] = set()
    for export in find_fcpxml_exports(xml_dir):
        name = library_name_for_export(export)
        if name:
            names.add(name)
    return names


def library_name_for_export(export_path: Path) -> str | None:
    """Identify the source library for an export: XML location first, else filename."""
    try:
        root = ET.parse(export_path).getroot()
    except (ET.ParseError, OSError):
        root = None
    if root is not None:
        from_xml = library_path_from_xml(root)
        if from_xml is not None:
            return from_xml.name

    if export_path.name == "Info.fcpxml":
        stem = export_path.parent.name
    else:
        stem = export_path.name
    key = normalize_library_key(stem)
    if not key:
        return None
    # Rehydrate a display-ish .fcpbundle name from the normalized key.
    return f"{stem_for_library_name(stem)}.fcpbundle"


def normalize_library_key(name: str) -> str:
    """Compare libraries/exports without package/file suffix noise."""
    text = name.strip()
    lower = text.lower()
    for suffix in (".fcpbundle", ".fcpxmld", ".fcpxml"):
        if lower.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return " ".join(text.casefold().split())


def stem_for_library_name(name: str) -> str:
    text = name.strip()
    lower = text.lower()
    for suffix in (".fcpbundle", ".fcpxmld", ".fcpxml"):
        if lower.endswith(suffix):
            return text[: -len(suffix)]
    return text


def format_libraries_report(
    *,
    processed: list[dict[str, object]],
    scanned: list[Path] | None = None,
    xml_library_names: set[str] | None = None,
) -> list[str]:
    """Render ✓ / ○ library lines, optionally with XML found/missing."""
    processed_by_path = {
        str(Path(str(row["library_path"]))): row for row in processed
    }
    processed_by_name = {str(row["library_name"]): row for row in processed}
    xml_keys = (
        {normalize_library_key(name) for name in xml_library_names}
        if xml_library_names is not None
        else None
    )

    rows: list[tuple[str, str, str | None]] = []
    if scanned is None:
        for row in sorted(processed, key=lambda item: str(item["library_name"]).lower()):
            name = str(row["library_name"])
            xml_status = None
            if xml_keys is not None:
                xml_status = (
                    "XML found"
                    if normalize_library_key(name) in xml_keys
                    else "XML missing"
                )
            rows.append(("✓", name, xml_status))
    else:
        for bundle in scanned:
            key = str(bundle)
            known = processed_by_path.get(key) or processed_by_name.get(bundle.name)
            mark = "✓" if known else "○"
            xml_status = None
            if xml_keys is not None:
                xml_status = (
                    "XML found"
                    if normalize_library_key(bundle.name) in xml_keys
                    else "XML missing"
                )
            rows.append((mark, bundle.name, xml_status))

    if not rows:
        return []
    if xml_keys is None:
        return [f"{mark} {name}" for mark, name, _xml in rows]
    width = max(len(name) for _mark, name, _xml in rows)
    return [f"{mark} {name:<{width}}  {xml_status}" for mark, name, xml_status in rows]
