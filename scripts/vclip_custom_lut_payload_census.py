#!/usr/bin/env python3
"""Fingerprint Final Cut Custom LUT effect payloads across an FCPXML corpus.

The first LUT census establishes that every Custom LUT use points to the same
FxPlug effect UID. This second-stage census asks whether the per-clip filter
configuration itself varies in a stable way that can identify the selected
look.

It records:
- sorted <param> name/key/value tuples
- decoded <data> payload lengths and SHA-256 fingerprints
- NSKeyedArchive strings, integers, byte blocks, and small opaque IDs
- one stable config fingerprint per filter-video occurrence

The tool is read-only and does not claim that a payload fingerprint is a LUT
name. It simply reveals whether distinct Custom LUT configurations exist and
whether their object graphs contain recoverable identifiers.
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
from typing import Any


CUSTOM_LUT_UID = "FxPlug:14B39AEF-607D-42DF-98DD-DB3DD345E925"
XML_EXTENSIONS = {".fcpxml", ".xml"}
PRINTABLE_BYTES_RE = re.compile(rb"[ -~]{4,}")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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


def nearest_ancestor_name(
    node: ET.Element,
    parent_map: dict[ET.Element, ET.Element],
    wanted: set[str],
) -> str:
    current = parent_map.get(node)
    while current is not None:
        if local_name(current.tag) in wanted:
            return current.get("name") or ""
        current = parent_map.get(current)
    return ""


def decode_data(raw_text: str | None) -> bytes:
    if not raw_text or not raw_text.strip():
        return b""
    compact = "".join(raw_text.split())
    try:
        return base64.b64decode(compact, validate=True)
    except Exception:
        return raw_text.encode("utf-8", errors="replace")


def walk_plist(
    value: Any,
    *,
    path: str = "$",
    strings: list[dict[str, Any]],
    numbers: list[dict[str, Any]],
    byte_blocks: list[dict[str, Any]],
) -> None:
    if isinstance(value, str):
        strings.append({"path": path, "value": value})
        return
    if isinstance(value, bool):
        numbers.append({"path": path, "type": "bool", "value": int(value)})
        return
    if isinstance(value, int):
        numbers.append({"path": path, "type": "int", "value": value})
        return
    if isinstance(value, float):
        numbers.append({"path": path, "type": "float", "value": value})
        return
    if isinstance(value, bytes):
        row: dict[str, Any] = {
            "path": path,
            "length": len(value),
            "sha256": sha256_bytes(value),
        }
        if len(value) <= 64:
            row["hex"] = value.hex()
        printable = [
            match.decode("utf-8", errors="ignore")
            for match in PRINTABLE_BYTES_RE.findall(value)
        ]
        if printable:
            row["printable_strings"] = printable
        byte_blocks.append(row)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            strings.append({"path": f"{path}.$key", "value": key_text})
            walk_plist(
                item,
                path=f"{path}.{key_text}",
                strings=strings,
                numbers=numbers,
                byte_blocks=byte_blocks,
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            walk_plist(
                item,
                path=f"{path}[{index}]",
                strings=strings,
                numbers=numbers,
                byte_blocks=byte_blocks,
            )


def analyze_payload(raw_text: str | None) -> dict[str, Any]:
    decoded = decode_data(raw_text)
    strings: list[dict[str, Any]] = []
    numbers: list[dict[str, Any]] = []
    byte_blocks: list[dict[str, Any]] = []
    plist_status = "not_plist"
    if decoded:
        try:
            payload = plistlib.loads(decoded)
            plist_status = type(payload).__name__
            walk_plist(
                payload,
                strings=strings,
                numbers=numbers,
                byte_blocks=byte_blocks,
            )
        except Exception:
            pass

    raw_printable = sorted(
        {
            match.decode("utf-8", errors="ignore").strip()
            for match in PRINTABLE_BYTES_RE.findall(decoded)
            if match.strip()
        }
    )
    return {
        "decoded_length": len(decoded),
        "decoded_sha256": sha256_bytes(decoded),
        "plist_status": plist_status,
        "strings": strings,
        "numbers": numbers,
        "byte_blocks": byte_blocks,
        "raw_printable": raw_printable,
    }


def scan_corpus(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    xml_files = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() in XML_EXTENSIONS
        ),
        key=lambda path: str(path).casefold(),
    )

    occurrences: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for file_index, path in enumerate(xml_files, 1):
        try:
            doc = ET.parse(path).getroot()
        except Exception as exc:
            errors.append({"file": str(path), "error": str(exc)})
            continue

        parent_map = {child: parent for parent in doc.iter() for child in parent}
        effects = {
            elem.get("id") or "": elem
            for elem in doc.iter()
            if local_name(elem.tag) == "effect" and elem.get("id")
        }

        for filt in doc.iter():
            if local_name(filt.tag) != "filter-video":
                continue
            effect = effects.get(filt.get("ref") or "")
            if effect is None or effect.get("uid") != CUSTOM_LUT_UID:
                continue

            params = sorted(
                [
                    {
                        "name": elem.get("name") or "",
                        "key": elem.get("key") or "",
                        "value": elem.get("value") or "",
                    }
                    for elem in filt.iter()
                    if local_name(elem.tag) == "param"
                ],
                key=lambda row: (row["key"], row["name"], row["value"]),
            )
            data_payloads = [
                analyze_payload(elem.text)
                for elem in filt.iter()
                if local_name(elem.tag) == "data"
            ]
            fingerprint_payload = {
                "enabled": filt.get("enabled") or "1",
                "params": params,
                "data": [
                    {
                        "decoded_sha256": row["decoded_sha256"],
                        "decoded_length": row["decoded_length"],
                    }
                    for row in data_payloads
                ],
            }
            config_id = "CLUTCFG_" + hashlib.sha256(
                canonical_json(fingerprint_payload).encode("utf-8")
            ).hexdigest()[:16].upper()

            all_strings = sorted(
                {
                    item["value"]
                    for payload in data_payloads
                    for item in payload["strings"]
                    if item.get("value")
                }
                | {
                    value
                    for payload in data_payloads
                    for value in payload["raw_printable"]
                    if value
                }
            )
            all_numbers = [
                item
                for payload in data_payloads
                for item in payload["numbers"]
            ]
            all_bytes = [
                item
                for payload in data_payloads
                for item in payload["byte_blocks"]
            ]

            occurrences.append(
                {
                    "config_id": config_id,
                    "file": str(path),
                    "project_name": nearest_ancestor_name(
                        filt, parent_map, {"project"}
                    ),
                    "clip_name": nearest_ancestor_name(
                        filt,
                        parent_map,
                        {"asset-clip", "clip", "ref-clip", "sync-clip", "mc-clip"},
                    ),
                    "filter_enabled": filt.get("enabled") or "1",
                    "local_effect_ref": filt.get("ref") or "",
                    "effect_name": effect.get("name") or "",
                    "effect_uid": effect.get("uid") or "",
                    "params_json": json.dumps(params, ensure_ascii=False),
                    "data_payloads_json": json.dumps(
                        data_payloads, ensure_ascii=False
                    ),
                    "strings_json": json.dumps(all_strings, ensure_ascii=False),
                    "numbers_json": json.dumps(all_numbers, ensure_ascii=False),
                    "byte_blocks_json": json.dumps(all_bytes, ensure_ascii=False),
                }
            )

        if file_index % 50 == 0:
            print(f"scanned {file_index}/{len(xml_files)} XML files")

    return occurrences, errors


def summarize(occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in occurrences:
        grouped[row["config_id"]].append(row)

    summary: list[dict[str, Any]] = []
    for config_id, rows in grouped.items():
        first = rows[0]
        strings_counter: Counter[str] = Counter()
        number_counter: Counter[str] = Counter()
        byte_counter: Counter[str] = Counter()
        for row in rows:
            for value in json.loads(row["strings_json"]):
                strings_counter[value] += 1
            for value in json.loads(row["numbers_json"]):
                number_counter[canonical_json(value)] += 1
            for value in json.loads(row["byte_blocks_json"]):
                byte_counter[canonical_json(value)] += 1

        summary.append(
            {
                "count": len(rows),
                "config_id": config_id,
                "filter_enabled": first["filter_enabled"],
                "params_json": first["params_json"],
                "top_strings_json": json.dumps(
                    [
                        {"value": value, "count": count}
                        for value, count in strings_counter.most_common(40)
                    ],
                    ensure_ascii=False,
                ),
                "top_numbers_json": json.dumps(
                    [
                        {"value": json.loads(value), "count": count}
                        for value, count in number_counter.most_common(40)
                    ],
                    ensure_ascii=False,
                ),
                "top_byte_blocks_json": json.dumps(
                    [
                        {"value": json.loads(value), "count": count}
                        for value, count in byte_counter.most_common(40)
                    ],
                    ensure_ascii=False,
                ),
                "sample_file": first["file"],
                "sample_project": first["project_name"],
                "sample_clip": first["clip_name"],
            }
        )

    summary.sort(key=lambda row: (-row["count"], row["config_id"]))
    return summary


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--show-configs", type=int, default=30)
    return p


def main() -> int:
    args = parser().parse_args()
    root = args.root.expanduser().resolve()
    output = args.output_root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Corpus root does not exist: {root}")
    output.mkdir(parents=True, exist_ok=True)

    occurrences, errors = scan_corpus(root)
    configs = summarize(occurrences)

    write_csv(output / "custom-lut-config-summary.csv", configs)
    write_csv(output / "custom-lut-config-occurrences.csv", occurrences)
    write_csv(output / "parse-errors.csv", errors)
    (output / "summary.json").write_text(
        json.dumps(
            {
                "corpus_root": str(root),
                "custom_lut_occurrences": len(occurrences),
                "unique_config_fingerprints": len(configs),
                "parse_errors": len(errors),
                "configs": configs,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print()
    print("VCLIP CUSTOM LUT PAYLOAD CENSUS")
    print("===============================")
    print("Custom LUT occurrences      :", len(occurrences))
    print("unique config fingerprints  :", len(configs))
    print("parse errors                :", len(errors))

    print()
    print("TOP CONFIG FINGERPRINTS")
    print("-----------------------")
    for row in configs[: max(0, args.show_configs)]:
        params = json.loads(row["params_json"])
        strings = json.loads(row["top_strings_json"])
        numbers = json.loads(row["top_numbers_json"])
        byte_blocks = json.loads(row["top_byte_blocks_json"])
        print()
        print(f"{row['count']:6d}  {row['config_id']}")
        print("sample project :", row["sample_project"])
        print("sample clip    :", row["sample_clip"])
        print("params         :", json.dumps(params, ensure_ascii=False))
        print(
            "strings        :",
            " | ".join(item["value"] for item in strings[:20]),
        )
        if numbers:
            print(
                "numbers        :",
                " | ".join(
                    f"{item['value']} x{item['count']}" for item in numbers[:15]
                ),
            )
        if byte_blocks:
            print(
                "byte blocks    :",
                " | ".join(
                    (
                        f"len={item['value'].get('length')} "
                        f"sha={item['value'].get('sha256','')[:16]} "
                        f"hex={item['value'].get('hex','')[:40]}"
                    )
                    for item in byte_blocks[:15]
                ),
            )

    print()
    print("output:", output)
    print("VCLIP CUSTOM LUT PAYLOAD CENSUS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
