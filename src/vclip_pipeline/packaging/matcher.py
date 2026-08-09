"""Match exported files back to durable stock candidate IDs."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..stockify.core import commandpost_filename_prefix


def normalized_export_name(value: str) -> str:
    """Normalize Final Cut filename punctuation without losing useful tokens."""
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("—", "-").replace("–", "-")
    text = Path(text).stem.lower()
    text = re.sub(r"\s*\(\d+\)$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


@dataclass(frozen=True)
class ExportMatch:
    path: Path
    stock_clip_id: str
    method: str
    confidence: str


@dataclass
class MatchResult:
    matches: list[ExportMatch] = field(default_factory=list)
    unmatched_files: list[Path] = field(default_factory=list)
    ambiguous_files: dict[str, list[str]] = field(default_factory=dict)
    missing_candidate_ids: list[str] = field(default_factory=list)


class ExportMatcher:
    """Use explicit project names first and heuristics only as fallbacks."""

    def match(
        self,
        files: list[Path],
        candidates: list[dict[str, Any]],
    ) -> MatchResult:
        result = MatchResult()
        exact_index: dict[str, list[str]] = defaultdict(list)
        id_index: dict[str, str] = {}
        commandpost_index: dict[tuple[str, str], list[str]] = defaultdict(list)

        for candidate in candidates:
            clip_id = str(candidate["stock_clip_id"])
            id_index[clip_id.lower()] = clip_id
            expected = candidate.get("expected_export_basename") or candidate.get(
                "generated_clip_project_name"
            )
            if expected:
                exact_index[normalized_export_name(str(expected))].append(clip_id)
            label = str(candidate.get("generated_project_label") or "")
            timecode = str(
                candidate.get("final_project_timecode")
                or candidate.get("project_timecode")
                or ""
            )
            if label and timecode:
                commandpost_index[
                    (commandpost_filename_prefix(label).lower(), timecode.lower())
                ].append(clip_id)

        matched_ids: set[str] = set()
        for path in files:
            stem_normalized = normalized_export_name(path.name)
            candidates_for_file: list[tuple[str, str, str]] = []

            exact = exact_index.get(stem_normalized, [])
            candidates_for_file.extend((clip_id, "exact_project_name", "high") for clip_id in exact)

            lower_name = path.stem.lower()
            for lower_id, clip_id in id_index.items():
                if lower_id in lower_name:
                    candidates_for_file.append((clip_id, "embedded_stock_clip_id", "high"))

            if not candidates_for_file:
                for (prefix, timecode), clip_ids in commandpost_index.items():
                    if prefix in lower_name and timecode in lower_name:
                        candidates_for_file.extend(
                            (clip_id, "commandpost_prefix_timecode", "high")
                            for clip_id in clip_ids
                        )

            unique = {
                clip_id: (method, confidence)
                for clip_id, method, confidence in candidates_for_file
            }
            if len(unique) == 1:
                clip_id, (method, confidence) = next(iter(unique.items()))
                if clip_id in matched_ids:
                    result.ambiguous_files[str(path)] = [
                        clip_id,
                        "duplicate export for an already matched candidate",
                    ]
                    continue
                matched_ids.add(clip_id)
                result.matches.append(
                    ExportMatch(
                        path=path,
                        stock_clip_id=clip_id,
                        method=method,
                        confidence=confidence,
                    )
                )
            elif len(unique) > 1:
                result.ambiguous_files[str(path)] = sorted(unique)
            else:
                result.unmatched_files.append(path)

        expected_ids = {str(candidate["stock_clip_id"]) for candidate in candidates}
        result.missing_candidate_ids = sorted(expected_ids - matched_ids)
        return result
