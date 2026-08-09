"""Audit and backfill helpers for visual catalog quality."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..util import utc_now
from .catalog import WorkflowCatalog
from .entities import EntityCatalog
from .models import NamedSubject

# Broad tags that alone do not make a clip merchandisable.
GENERIC_TAGS = frozenset(
    {"city_urban", "establishing", "golden_hour", "clear_skies", "cloudy", "background"}
)
SUBJECT_GROUPS = frozenset({"subject"})
MANY_TAGS_THRESHOLD = 10


@dataclass
class CatalogQualityReport:
    run_id: str | None
    total_enriched_clips: int = 0
    clips_with_zero_tags: int = 0
    clips_with_zero_primary_tags: int = 0
    clips_with_one_plus_primary_tags: int = 0
    clips_with_two_plus_subject_tags: int = 0
    clips_with_named_subject_suggestions: int = 0
    clips_with_canonical_named_subjects: int = 0
    clips_with_unresolved_named_subjects: int = 0
    average_tags_per_clip: float = 0.0
    median_tags_per_clip: float = 0.0
    tag_frequency: dict[str, int] = field(default_factory=dict)
    primary_tag_frequency: dict[str, int] = field(default_factory=dict)
    named_subject_raw_frequency: dict[str, int] = field(default_factory=dict)
    canonical_entity_frequency: dict[str, int] = field(default_factory=dict)
    clips_with_unusually_many_tags: list[dict[str, Any]] = field(default_factory=list)
    clips_with_generic_metadata: list[dict[str, Any]] = field(default_factory=list)
    tag_diagnostics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_generic_metadata(
    tags: list[dict[str, Any]],
    named_subjects: list[dict[str, Any]],
) -> bool:
    """True when tags are only broad concepts and no named subjects exist."""
    if named_subjects:
        return False
    tag_ids = {str(tag.get("tag") or "") for tag in tags if tag.get("tag")}
    if not tag_ids:
        return True
    subject_tags = {
        str(tag.get("tag") or "")
        for tag in tags
        if str(tag.get("tag_group") or tag.get("group") or "") in SUBJECT_GROUPS
        or str(tag.get("tag") or "")
        in {
            "road",
            "waterfront",
            "architecture",
            "skyline",
            "bridge",
            "mountain",
            "campus",
        }
    }
    if subject_tags:
        return False
    return tag_ids <= GENERIC_TAGS


class CatalogQualityService:
    """Read-only audit plus offline entity canonicalization."""

    def __init__(
        self,
        catalog: WorkflowCatalog,
        entities: EntityCatalog | None = None,
    ) -> None:
        self.catalog = catalog
        self.entities = entities or EntityCatalog.default()

    def audit(
        self,
        *,
        run_id: str | None = None,
        include_diagnostics: bool = True,
    ) -> CatalogQualityReport:
        rows = [
            row
            for row in self.catalog.catalog_rows(run_id=run_id)
            if row.get("visual_analysis")
        ]
        report = CatalogQualityReport(run_id=run_id, total_enriched_clips=len(rows))
        if not rows:
            report.warnings.append("No enriched clips found for the selected cohort.")
            return report

        tag_counts: list[int] = []
        tag_freq: Counter[str] = Counter()
        primary_freq: Counter[str] = Counter()
        raw_subject_freq: Counter[str] = Counter()
        entity_freq: Counter[str] = Counter()
        strength_by_tag: dict[str, Counter[str]] = {}

        for row in rows:
            tags = list(row.get("tags") or [])
            visual_tags = [tag for tag in tags if tag.get("source") == "visual"]
            # Fall back to all tags if source not set on older rows.
            useful_tags = visual_tags or tags
            subjects = list(row.get("named_subjects") or [])
            tag_count = len(useful_tags)
            tag_counts.append(tag_count)

            if tag_count == 0:
                report.clips_with_zero_tags += 1

            primaries = [
                tag
                for tag in useful_tags
                if str(tag.get("strength") or "") == "primary"
            ]
            if not primaries:
                report.clips_with_zero_primary_tags += 1
            else:
                report.clips_with_one_plus_primary_tags += 1

            subject_tags = [
                tag
                for tag in useful_tags
                if str(tag.get("tag_group") or "") == "subject"
            ]
            if len(subject_tags) >= 2:
                report.clips_with_two_plus_subject_tags += 1

            if subjects:
                report.clips_with_named_subject_suggestions += 1
            if any(item.get("canonical_entity_id") for item in subjects):
                report.clips_with_canonical_named_subjects += 1
            if any(
                item.get("subject") or item.get("raw_name")
                for item in subjects
                if not item.get("canonical_entity_id")
            ):
                report.clips_with_unresolved_named_subjects += 1

            for tag in useful_tags:
                tag_id = str(tag.get("tag") or "")
                if not tag_id:
                    continue
                tag_freq[tag_id] += 1
                strength = str(tag.get("strength") or "context")
                strength_by_tag.setdefault(tag_id, Counter())[strength] += 1
                if strength == "primary":
                    primary_freq[tag_id] += 1

            for subject in subjects:
                raw = str(subject.get("raw_name") or subject.get("subject") or "")
                if raw:
                    raw_subject_freq[raw] += 1
                entity_id = subject.get("canonical_entity_id")
                if entity_id:
                    label = subject.get("canonical_label") or entity_id
                    entity_freq[str(label)] += 1

            if tag_count >= MANY_TAGS_THRESHOLD:
                report.clips_with_unusually_many_tags.append(
                    _clip_ref(row, note=f"{tag_count} tags")
                )
            if is_generic_metadata(useful_tags, subjects):
                report.clips_with_generic_metadata.append(
                    _clip_ref(row, note="broad tags only; no subject/entity")
                )

        report.average_tags_per_clip = round(statistics.mean(tag_counts), 3)
        report.median_tags_per_clip = float(statistics.median(tag_counts))
        report.tag_frequency = dict(tag_freq.most_common())
        report.primary_tag_frequency = dict(primary_freq.most_common())
        report.named_subject_raw_frequency = dict(raw_subject_freq.most_common())
        report.canonical_entity_frequency = dict(entity_freq.most_common())

        if include_diagnostics:
            report.tag_diagnostics = _tag_diagnostics(
                tag_freq,
                strength_by_tag,
                total=len(rows),
            )
        return report

    def canonicalize_entities(
        self,
        *,
        run_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Re-resolve named subjects from persisted enrichment without OpenAI."""
        rows = [
            row
            for row in self.catalog.catalog_rows(run_id=run_id)
            if row.get("visual_analysis")
        ]
        updated = 0
        resolved = 0
        unresolved = 0
        samples: list[dict[str, Any]] = []
        for row in rows:
            analysis_payload = dict(row.get("visual_analysis") or {})
            raw_subjects = list(analysis_payload.get("named_subjects") or [])
            if not raw_subjects and not row.get("named_subjects"):
                continue
            # Prefer raw names from result_json; fall back to table rows.
            source_subjects = raw_subjects or [
                {
                    "name": item.get("raw_name") or item.get("subject"),
                    "confidence": item.get("confidence") or "possible",
                    "verified": bool(item.get("verified")),
                }
                for item in row.get("named_subjects") or []
            ]
            canonical_subjects: list[NamedSubject] = []
            for item in source_subjects:
                if item.get("verified") and item.get("canonical_entity_id"):
                    canonical_subjects.append(
                        NamedSubject(
                            name=str(item.get("name") or item.get("raw_name") or ""),
                            confidence=str(item.get("confidence") or "possible"),
                            verified=True,
                            canonical_entity_id=item.get("canonical_entity_id"),
                            canonical_label=item.get("canonical_label"),
                            resolution_source=item.get("resolution_source")
                            or "human",
                        )
                    )
                    resolved += 1
                    continue
                raw_name = str(item.get("name") or item.get("raw_name") or "").strip()
                if not raw_name:
                    continue
                subject = self.entities.canonicalize_subject(
                    NamedSubject(
                        name=raw_name,
                        confidence=str(item.get("confidence") or "possible"),
                        verified=False,
                    )
                )
                canonical_subjects.append(subject)
                if subject.canonical_entity_id:
                    resolved += 1
                else:
                    unresolved += 1
                if len(samples) < 20:
                    samples.append(
                        {
                            "stock_clip_id": row["stock_clip_id"],
                            "raw_name": subject.raw_name,
                            "canonical_entity_id": subject.canonical_entity_id,
                            "canonical_label": subject.canonical_label,
                        }
                    )

            if dry_run:
                updated += 1
                continue

            self.catalog.replace_named_subjects(
                stockify_run_id=str(row["stockify_run_id"]),
                stock_clip_id=str(row["stock_clip_id"]),
                subjects=canonical_subjects,
            )
            updated += 1

        if not dry_run:
            self.catalog.rebuild_search_index()
        return {
            "run_id": run_id,
            "clips_considered": len(rows),
            "clips_updated": updated,
            "subjects_resolved": resolved,
            "subjects_unresolved": unresolved,
            "dry_run": dry_run,
            "samples": samples,
            "completed_at": utc_now(),
        }


def write_quality_report(path: Path, report: CatalogQualityReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def format_quality_report(report: CatalogQualityReport) -> list[str]:
    lines = [
        "Catalog quality audit",
        "---------------------",
        f"Enriched clips:                    {report.total_enriched_clips}",
        f"Zero tags:                         {report.clips_with_zero_tags}",
        f"Zero primary tags:                 {report.clips_with_zero_primary_tags}",
        f"1+ primary tags:                   {report.clips_with_one_plus_primary_tags}",
        f"2+ subject tags:                   {report.clips_with_two_plus_subject_tags}",
        f"Named-subject suggestions:         {report.clips_with_named_subject_suggestions}",
        f"Canonical named subjects:          {report.clips_with_canonical_named_subjects}",
        f"Unresolved named subjects:         {report.clips_with_unresolved_named_subjects}",
        f"Avg tags / clip:                   {report.average_tags_per_clip}",
        f"Median tags / clip:                {report.median_tags_per_clip}",
        f"Unusually many tags:               {len(report.clips_with_unusually_many_tags)}",
        f"Too-generic metadata:              {len(report.clips_with_generic_metadata)}",
    ]
    if report.tag_frequency:
        lines.append("Top tags:")
        for tag, count in list(report.tag_frequency.items())[:12]:
            lines.append(f"  {tag:<22} {count}")
    if report.canonical_entity_frequency:
        lines.append("Canonical entities:")
        for label, count in list(report.canonical_entity_frequency.items())[:12]:
            lines.append(f"  {label:<22} {count}")
    if report.clips_with_generic_metadata:
        lines.append("Generic metadata examples:")
        for item in report.clips_with_generic_metadata[:8]:
            lines.append(
                f"  {item['stock_clip_id']}: {item.get('caption') or '(no caption)'}"
            )
    return lines


def _clip_ref(row: dict[str, Any], *, note: str) -> dict[str, Any]:
    return {
        "stockify_run_id": row.get("stockify_run_id"),
        "stock_clip_id": row.get("stock_clip_id"),
        "caption": row.get("caption"),
        "exported_filename": row.get("exported_filename"),
        "generated_project_label": row.get("generated_project_label"),
        "note": note,
    }


def _tag_diagnostics(
    tag_freq: Counter[str],
    strength_by_tag: dict[str, Counter[str]],
    *,
    total: int,
) -> dict[str, Any]:
    if total <= 0:
        return {}
    ubiquitous = {
        tag: count for tag, count in tag_freq.items() if count / total >= 0.8
    }
    rare = {tag: count for tag, count in tag_freq.items() if count == 1}
    mostly_primary = {}
    mostly_context = {}
    for tag, strengths in strength_by_tag.items():
        total_tag = sum(strengths.values())
        if total_tag == 0:
            continue
        if strengths.get("primary", 0) / total_tag >= 0.8:
            mostly_primary[tag] = dict(strengths)
        if strengths.get("context", 0) / total_tag >= 0.8:
            mostly_context[tag] = dict(strengths)
    suspicious = []
    if "coastal" in tag_freq and "waterfront" in tag_freq:
        # Not inherently wrong, but worth reviewing after the coastal redefinition.
        suspicious.append(
            {
                "pattern": "coastal_and_waterfront_both_present_in_cohort",
                "coastal": tag_freq["coastal"],
                "waterfront": tag_freq["waterfront"],
                "note": (
                    "After taxonomy v2, coastal should be ocean/beach/cliff only; "
                    "bay/marina scenes should prefer waterfront."
                ),
            }
        )
    return {
        "tags_on_gt_80pct_of_clips": ubiquitous,
        "tags_appearing_once": rare,
        "tags_almost_always_primary": mostly_primary,
        "tags_almost_always_context": mostly_context,
        "suspicious_combinations": suspicious,
    }


