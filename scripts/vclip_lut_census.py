#!/usr/bin/env python3
"""Inventory Camera LUT and Custom LUT evidence across an FCPXML corpus.

This is read-only. It distinguishes:

- Camera LUTs serialized directly on <asset customLUTOverride="...">.
- Custom LUT clip effects serialized as <filter-video ref="..."> with a
  referenced <effect> resource plus params/effectConfig payloads.

The report also scans Final Cut's installed Custom LUT directory and attempts a
conservative filename/stem match against strings recovered from Custom LUT
filter payloads. Local resource IDs (for example r17) are never treated as
global LUT identities.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import plistlib
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


CAMERA_LUT_RE = re.compile(r"^LUT:([^ ]+) \((.*)\)$")
PRINTABLE_BYTES_RE = re.compile(rb"[ -~]{4,}")
PRINTABLE_TEXT_RE = re.compile(r"[ -~]{4,}")
LUT_FILE_EXTENSIONS = {".cube", ".mga"}
XML_EXTENSIONS = {".fcpxml", ".xml"}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def flatten_plist_strings(value: Any, out: set[str]) -> None:
    if isinstance(value, str):
        if value.strip():
            out.add(value.strip())
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.strip():
                out.add(key.strip())
            flatten_plist_strings(item, out)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            flatten_plist_strings(item, out)


def plausible_string(value: str) -> bool:
    text = value.strip().replace("\x00", "")
    if len(text) < 4 or len(text) > 240:
        return False
    printable = sum(ch.isprintable() for ch in text) / max(1, len(text))
    alnum = sum(ch.isalnum() for ch in text) / max(1, len(text))
    return printable > 0.95 and alnum > 0.20


def decode_data_strings(raw_text: str | None) -> list[str]:
    """Recover human-readable strings from an FCP effect data payload."""
    if not raw_text or not raw_text.strip():
        return []
    compact = "".join(raw_text.split())
    decoded: bytes | None = None
    try:
        decoded = base64.b64decode(compact, validate=True)
    except Exception:
        decoded = None

    values: set[str] = set()
    if decoded is None:
        if plausible_string(raw_text):
            values.add(raw_text.strip())
        return sorted(values)

    try:
        plist = plistlib.loads(decoded)
        flatten_plist_strings(plist, values)
    except Exception:
        pass

    for match in PRINTABLE_BYTES_RE.findall(decoded):
        try:
            text = match.decode("utf-8", errors="ignore").strip()
        except Exception:
            continue
        if plausible_string(text):
            values.add(text)

    for encoding in ("utf-16le", "utf-16be"):
        try:
            text = decoded.decode(encoding, errors="ignore")
        except Exception:
            continue
        for match in PRINTABLE_TEXT_RE.findall(text):
            cleaned = match.strip()
            if plausible_string(cleaned):
                values.add(cleaned)

    return sorted(values)


def scan_installed_luts(root: Path | None) -> list[dict[str, Any]]:
    if root is None or not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda p: str(p).casefold()):
        if not path.is_file() or path.suffix.casefold() not in LUT_FILE_EXTENSIONS:
            continue
        try:
            checksum = sha256_file(path)
        except OSError:
            checksum = ""
        rows.append(
            {
                "name": path.stem,
                "filename": path.name,
                "extension": path.suffix.casefold(),
                "relative_path": str(path.relative_to(root)),
                "absolute_path": str(path),
                "sha256": checksum,
                "normalized_name": norm(path.stem),
            }
        )
    return rows


def nearest_ancestor_name(
    node: ET.Element,
    parent_map: dict[ET.Element, ET.Element],
    wanted: set[str],
) -> str:
    current = parent_map.get(node)
    while current is not None:
        tag = local_name(current.tag)
        if tag in wanted:
            return current.get("name") or ""
        current = parent_map.get(current)
    return ""


def filter_evidence(
    filter_node: ET.Element,
    effect: ET.Element | None,
) -> tuple[list[str], list[dict[str, str]], list[dict[str, Any]]]:
    strings: set[str] = set()
    params: list[dict[str, str]] = []
    data_rows: list[dict[str, Any]] = []

    for value in (
        filter_node.get("name"),
        effect.get("name") if effect is not None else None,
        effect.get("uid") if effect is not None else None,
        effect.get("src") if effect is not None else None,
    ):
        if value:
            strings.add(value)

    for elem in filter_node.iter():
        tag = local_name(elem.tag)
        if tag == "param":
            row = {
                "name": elem.get("name") or "",
                "key": elem.get("key") or "",
                "value": elem.get("value") or "",
            }
            params.append(row)
            strings.update(v for v in row.values() if v)
        elif tag == "data":
            recovered = decode_data_strings(elem.text)
            data_rows.append(
                {
                    "key": elem.get("key") or "",
                    "recovered_strings": recovered,
                }
            )
            strings.update(recovered)

    return sorted(s for s in strings if s), params, data_rows


def looks_lut_related(strings: Iterable[str]) -> bool:
    joined = "\n".join(strings).casefold()
    return any(
        token in joined
        for token in (
            "custom lut",
            "customlut",
            ".cube",
            ".mga",
            " lut",
            "lut ",
            "lut/",
            "/lut",
        )
    )


def match_installed_luts(
    strings: Iterable[str],
    installed: list[dict[str, Any]],
) -> list[str]:
    evidence_norm = norm(" ".join(strings))
    if not evidence_norm:
        return []
    matches: list[str] = []
    for row in installed:
        key = row["normalized_name"]
        if len(key) >= 4 and key in evidence_norm:
            matches.append(row["name"])
    return sorted(set(matches))


def scan_corpus(
    root: Path,
    installed_luts: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    xml_files = sorted(
        (
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix.casefold() in XML_EXTENSIONS
        ),
        key=lambda p: str(p).casefold(),
    )

    camera_occurrences: list[dict[str, Any]] = []
    effect_occurrences: list[dict[str, Any]] = []
    custom_filter_occurrences: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    file_rows: list[dict[str, Any]] = []

    for index, path in enumerate(xml_files, 1):
        try:
            tree = ET.parse(path)
            doc = tree.getroot()
        except Exception as exc:
            parse_errors.append({"file": str(path), "error": str(exc)})
            continue

        parent_map = {child: parent for parent in doc.iter() for child in parent}
        effects = {
            elem.get("id") or "": elem
            for elem in doc.iter()
            if local_name(elem.tag) == "effect" and elem.get("id")
        }
        file_camera = 0
        file_filters = 0
        file_custom = 0

        for asset in doc.iter():
            if local_name(asset.tag) != "asset":
                continue
            raw = asset.get("customLUTOverride")
            if not raw:
                continue
            match = CAMERA_LUT_RE.match(raw)
            camera_occurrences.append(
                {
                    "file": str(path),
                    "asset_id": asset.get("id") or "",
                    "asset_name": asset.get("name") or "",
                    "raw": raw,
                    "lut_id": match.group(1) if match else "",
                    "lut_name": match.group(2) if match else "",
                    "kind": "custom_camera_lut" if match else "builtin_or_none",
                }
            )
            file_camera += 1

        for filt in doc.iter():
            if local_name(filt.tag) != "filter-video":
                continue
            file_filters += 1
            ref = filt.get("ref") or ""
            effect = effects.get(ref)
            strings, params, data_rows = filter_evidence(filt, effect)
            effect_name = effect.get("name") if effect is not None else ""
            effect_uid = effect.get("uid") if effect is not None else ""
            effect_src = effect.get("src") if effect is not None else ""
            project_name = nearest_ancestor_name(
                filt, parent_map, {"project"}
            )
            clip_name = nearest_ancestor_name(
                filt,
                parent_map,
                {"asset-clip", "clip", "ref-clip", "sync-clip", "mc-clip"},
            )
            occurrence = {
                "file": str(path),
                "project_name": project_name,
                "clip_name": clip_name,
                "local_effect_ref": ref,
                "filter_name": filt.get("name") or "",
                "filter_enabled": filt.get("enabled") or "1",
                "effect_name": effect_name or "",
                "effect_uid": effect_uid or "",
                "effect_src": effect_src or "",
            }
            effect_occurrences.append(occurrence)

            if not looks_lut_related(strings):
                continue
            file_custom += 1
            matches = match_installed_luts(strings, installed_luts)
            custom_filter_occurrences.append(
                {
                    **occurrence,
                    "installed_lut_matches": " | ".join(matches),
                    "param_json": json.dumps(params, ensure_ascii=False),
                    "data_json": json.dumps(data_rows, ensure_ascii=False),
                    "evidence_strings_json": json.dumps(
                        strings, ensure_ascii=False
                    ),
                }
            )

        file_rows.append(
            {
                "file": str(path),
                "fcpxml_version": doc.get("version") or "",
                "camera_lut_occurrences": file_camera,
                "filter_video_occurrences": file_filters,
                "lut_related_filter_occurrences": file_custom,
            }
        )
        if index % 50 == 0:
            print(f"scanned {index}/{len(xml_files)} XML files")

    return (
        file_rows,
        camera_occurrences,
        effect_occurrences,
        custom_filter_occurrences,
        parse_errors,
    )


def summarize_camera(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], int] = Counter(
        (row["kind"], row["lut_id"], row["lut_name"], row["raw"])
        for row in rows
    )
    return [
        {
            "count": count,
            "kind": key[0],
            "lut_id": key[1],
            "lut_name": key[2],
            "raw": key[3],
        }
        for key, count in sorted(
            grouped.items(), key=lambda item: (-item[1], item[0])
        )
    ]


def summarize_effects(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], int] = Counter(
        (row["effect_name"], row["effect_uid"], row["effect_src"])
        for row in rows
    )
    return [
        {
            "count": count,
            "effect_name": key[0],
            "effect_uid": key[1],
            "effect_src": key[2],
        }
        for key, count in sorted(
            grouped.items(), key=lambda item: (-item[1], item[0])
        )
    ]


def summarize_custom_matches(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        matches = [
            value.strip()
            for value in row["installed_lut_matches"].split("|")
            if value.strip()
        ]
        if not matches:
            counter["<unresolved>"] += 1
        else:
            for match in matches:
                counter[match] += 1
    return [
        {"count": count, "lut_name": name}
        for name, count in counter.most_common()
    ]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument(
        "--custom-lut-dir",
        type=Path,
        default=Path.home()
        / "Library"
        / "Application Support"
        / "ProApps"
        / "Custom LUTs",
    )
    p.add_argument("--show-unresolved", type=int, default=20)
    return p


def main() -> int:
    args = parser().parse_args()
    root = args.root.expanduser().resolve()
    output = args.output_root.expanduser().resolve()
    lut_dir = args.custom_lut_dir.expanduser().resolve()

    if not root.is_dir():
        raise SystemExit(f"Corpus root does not exist: {root}")
    output.mkdir(parents=True, exist_ok=True)

    installed = scan_installed_luts(lut_dir)
    (
        file_rows,
        camera_rows,
        effect_rows,
        custom_rows,
        errors,
    ) = scan_corpus(root, installed)

    camera_summary = summarize_camera(camera_rows)
    effect_summary = summarize_effects(effect_rows)
    custom_summary = summarize_custom_matches(custom_rows)

    write_csv(output / "camera-lut-summary.csv", camera_summary)
    write_csv(output / "camera-lut-occurrences.csv", camera_rows)
    write_csv(output / "video-effect-summary.csv", effect_summary)
    write_csv(output / "video-effect-occurrences.csv", effect_rows)
    write_csv(output / "custom-lut-filter-occurrences.csv", custom_rows)
    write_csv(output / "custom-lut-match-summary.csv", custom_summary)
    write_csv(output / "installed-custom-luts.csv", installed)
    write_csv(output / "files.csv", file_rows)
    write_csv(output / "parse-errors.csv", errors)

    summary = {
        "corpus_root": str(root),
        "custom_lut_dir": str(lut_dir),
        "xml_files_scanned": len(file_rows),
        "parse_errors": len(errors),
        "camera_lut_occurrences": len(camera_rows),
        "camera_lut_unique_values": len(camera_summary),
        "filter_video_occurrences": len(effect_rows),
        "lut_related_filter_occurrences": len(custom_rows),
        "installed_custom_lut_files": len(installed),
        "camera_luts": camera_summary,
        "custom_lut_matches": custom_summary,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print()
    print("VCLIP LUT CENSUS")
    print("================")
    print("XML files scanned          :", len(file_rows))
    print("parse errors               :", len(errors))
    print("Camera LUT occurrences     :", len(camera_rows))
    print("Camera LUT unique values   :", len(camera_summary))
    print("filter-video occurrences   :", len(effect_rows))
    print("LUT-related filter effects :", len(custom_rows))
    print("installed Custom LUT files :", len(installed))

    print()
    print("CAMERA LUT MAPPINGS")
    print("-------------------")
    for row in camera_summary:
        print(
            f"{row['count']:5d}  {row['lut_id'] or '-':34s}  "
            f"{row['lut_name'] or row['raw']}"
        )

    print()
    print("CUSTOM LUT EFFECT RESOURCES")
    print("---------------------------")
    lut_effects = [
        row
        for row in effect_summary
        if looks_lut_related(
            [row["effect_name"], row["effect_uid"], row["effect_src"]]
        )
    ]
    if not lut_effects:
        print("(none identified from effect resource names/UIDs)")
    for row in lut_effects[:30]:
        print(
            f"{row['count']:5d}  name={row['effect_name']!r}  "
            f"uid={row['effect_uid']!r}  src={row['effect_src']!r}"
        )

    print()
    print("CUSTOM LUT SELECTED-LOOK MATCHES")
    print("--------------------------------")
    if not custom_summary:
        print("(no LUT-related filter-video instances found)")
    for row in custom_summary:
        print(f"{row['count']:5d}  {row['lut_name']}")

    unresolved = [
        row for row in custom_rows if not row["installed_lut_matches"]
    ]
    if unresolved:
        print()
        print("UNRESOLVED CUSTOM LUT FILTER SAMPLES")
        print("------------------------------------")
        for row in unresolved[: max(0, args.show_unresolved)]:
            print()
            print("file       :", row["file"])
            print("project    :", row["project_name"])
            print("clip       :", row["clip_name"])
            print("effect     :", row["effect_name"])
            print("effect uid :", row["effect_uid"])
            strings = json.loads(row["evidence_strings_json"])
            print("evidence   :", " | ".join(strings[:20]))

    print()
    print("output:", output)
    print("VCLIP LUT CENSUS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
