"""Frame sampling and visual enrichment of final approved exports."""

from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..util import sha256_file, stable_id, utc_now
from .catalog import WorkflowCatalog
from .frames import SAMPLER_VERSION, FrameSampler
from .models import FrameSampleSet, VisualAnalysis
from .providers.openai import PROMPT_VERSION
from .review_shard import MarketCatalog
from .taxonomy import VisualTaxonomy


class VisualAnalyzer(Protocol):
    provider_name: str
    model: str

    def analyze(
        self,
        frames: tuple[Path, ...],
        *,
        context: dict[str, Any],
    ) -> VisualAnalysis: ...


@dataclass
class EnrichmentReport:
    provider: str
    model: str
    taxonomy_version: int
    sampler_version: str
    exports_considered: int = 0
    cached: int = 0
    frames_extracted: int = 0
    analyzed: int = 0
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    clips: list[dict[str, Any]] = field(default_factory=list)


class VisualEnrichmentService:
    """Turn exported clips into pixel-grounded catalog metadata."""

    def __init__(
        self,
        catalog: WorkflowCatalog,
        sampler: FrameSampler,
        taxonomy: VisualTaxonomy,
        markets: MarketCatalog,
        analyzer: VisualAnalyzer | None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.sampler = sampler
        self.taxonomy = taxonomy
        self.markets = markets
        self.analyzer = analyzer
        self.progress = progress

    def run(
        self,
        *,
        run_id: str | None = None,
        include_pending: bool = False,
        limit: int | None = None,
        force: bool = False,
        fail_fast: bool = False,
        dry_run: bool = False,
        report_path: Path | None = None,
        html_path: Path | None = None,
    ) -> EnrichmentReport:
        provider = self.analyzer.provider_name if self.analyzer else "frames-only"
        model = self.analyzer.model if self.analyzer else "none"
        report = EnrichmentReport(
            provider=provider,
            model=model,
            taxonomy_version=self.taxonomy.version,
            sampler_version=SAMPLER_VERSION,
        )
        exports = self.catalog.eligible_exports(
            run_id=run_id,
            include_pending=include_pending,
            limit=limit,
        )
        report.exports_considered = len(exports)
        visual_run_id = (
            stable_id("VISUALRUN", "DRYRUN", provider, model, utc_now())
            if dry_run
            else self.catalog.start_visual_run(
                provider=provider,
                model=model,
                taxonomy_version=self.taxonomy.version,
                prompt_version=PROMPT_VERSION,
                sampler_version=SAMPLER_VERSION,
                config={
                    "include_pending": include_pending,
                    "limit": limit,
                    "force": force,
                    "dry_run": dry_run,
                },
            )
        )
        try:
            for index, row in enumerate(exports, start=1):
                clip_id = str(row["stock_clip_id"])
                run = str(row["stockify_run_id"])
                export_path = Path(str(row["exported_path"]))
                self._announce(f"{index}/{len(exports)} {export_path.name}")
                try:
                    checksum = row.get("export_sha256") or sha256_file(export_path)
                    analysis_key = stable_id(
                        "ANALYSISKEY",
                        checksum,
                        provider,
                        model,
                        self.taxonomy.version,
                        PROMPT_VERSION,
                        SAMPLER_VERSION,
                    )
                    self._persist_objective_facets(row, dry_run=dry_run)
                    if not force and self.catalog.has_analysis_key(analysis_key):
                        report.cached += 1
                        continue
                    samples = self.sampler.sample(
                        export_path,
                        export_sha256=str(checksum),
                        overwrite=force,
                    )
                    report.frames_extracted += len(samples.frames)
                    if self.analyzer is None:
                        report.clips.append(
                            self._report_clip(row, samples, None, analysis_key)
                        )
                        continue
                    analysis = self.analyzer.analyze(
                        samples.frames,
                        context=self._context(row),
                    )
                    if not dry_run:
                        self.catalog.upsert_visual_analysis(
                            analysis_key=analysis_key,
                            analysis_run_id=visual_run_id,
                            stockify_run_id=run,
                            stock_clip_id=clip_id,
                            export_id=str(row["export_id"]),
                            export_sha256=str(checksum),
                            provider=provider,
                            model=model,
                            taxonomy_version=self.taxonomy.version,
                            analysis=analysis,
                            evidence=self._evidence(samples),
                        )
                    report.analyzed += 1
                    report.clips.append(
                        self._report_clip(row, samples, analysis, analysis_key)
                    )
                except Exception as exc:
                    report.failed += 1
                    report.failures.append(
                        {
                            "stockify_run_id": str(row["stockify_run_id"]),
                            "stock_clip_id": clip_id,
                            "exported_path": str(export_path),
                            "error": str(exc),
                        }
                    )
                    if fail_fast:
                        raise
            if not dry_run:
                self.catalog.rebuild_search_index()
            if not dry_run:
                self.catalog.finish_visual_run(visual_run_id)
        except Exception as exc:
            if not dry_run:
                self.catalog.finish_visual_run(visual_run_id, error=str(exc))
            self._write_report(report_path, report)
            raise
        self._write_report(report_path, report)
        if html_path:
            self._write_html(html_path, report)
        return report

    def _persist_objective_facets(self, row: dict[str, Any], *, dry_run: bool) -> None:
        if dry_run:
            return
        run_id = str(row["stockify_run_id"])
        clip_id = str(row["stock_clip_id"])
        orientation = row.get("orientation")
        if orientation:
            self.catalog.upsert_tag(
                run_id=run_id,
                clip_id=clip_id,
                group="format",
                tag=str(orientation),
                source="probe",
                strength="primary",
                score=1.0,
                evidence={"width": row.get("width"), "height": row.get("height")},
            )
        market_id, market_label = self.markets.resolve(
            row.get("city"), row.get("state")
        )
        self.catalog.upsert_market(
            run_id=run_id,
            clip_id=clip_id,
            market_id=market_id,
            market_label=market_label,
            source="camera_location",
            confidence="high" if row.get("city") else "low",
        )
        # Capture-time metadata is a useful factual signal, but it does not prove
        # visible golden light. Store it separately from pixel-derived golden_hour.
        if row.get("time_of_day"):
            self.catalog.upsert_tag(
                run_id=run_id,
                clip_id=clip_id,
                group="capture_time",
                tag=str(row["time_of_day"]),
                source="telemetry",
                strength="context",
                score=1.0,
            )

    @staticmethod
    def _context(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "camera_location": {
                "public_label": row.get("public_label"),
                "city": row.get("city"),
                "neighborhood": row.get("neighborhood"),
                "state": row.get("state"),
            },
            "capture_time_of_day": row.get("time_of_day"),
            "orientation": row.get("orientation"),
            "duration_seconds": (
                row.get("export_duration_seconds")
                or row.get("final_duration_seconds")
                or row.get("proposed_duration_seconds")
            ),
            "source_name": row.get("source_name"),
            "instruction": (
                "Location is where the camera was, not necessarily the visible subject, "
                "especially for telephoto drone footage."
            ),
        }

    @staticmethod
    def _evidence(samples: FrameSampleSet) -> dict[str, Any]:
        return {
            "frame_cache_key": samples.cache_key,
            "export_sha256": samples.export_sha256,
            "duration_seconds": samples.duration_seconds,
            "positions": list(samples.positions),
            "frames": [str(path) for path in samples.frames],
        }

    @staticmethod
    def _report_clip(
        row: dict[str, Any],
        samples: FrameSampleSet,
        analysis: VisualAnalysis | None,
        analysis_key: str,
    ) -> dict[str, Any]:
        return {
            "stockify_run_id": row["stockify_run_id"],
            "stock_clip_id": row["stock_clip_id"],
            "exported_path": row["exported_path"],
            "analysis_key": analysis_key,
            "frames": [str(path) for path in samples.frames],
            "caption": analysis.caption if analysis else None,
            "tags": [asdict(tag) for tag in analysis.tags] if analysis else [],
            "named_subjects": (
                [asdict(subject) for subject in analysis.named_subjects]
                if analysis
                else []
            ),
        }

    @staticmethod
    def _write_report(path: Path | None, report: EnrichmentReport) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(report), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _write_html(path: Path, report: EnrichmentReport) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        cards: list[str] = []
        for clip in report.clips:
            images = "".join(
                f'<img src="{html.escape(Path(frame).as_uri())}" loading="lazy">'
                for frame in clip.get("frames", [])
            )
            tags = ", ".join(
                f"{tag['tag']} ({tag['strength']})"
                for tag in clip.get("tags", [])
            )
            cards.append(
                "<article>"
                f"<h2>{html.escape(str(clip['stock_clip_id']))}</h2>"
                f"<p>{html.escape(str(clip.get('caption') or 'Frames only'))}</p>"
                f"<p><strong>Tags:</strong> {html.escape(tags)}</p>"
                f"<div class='frames'>{images}</div>"
                "</article>"
            )
        document = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>VClip Visual Review</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:24px;background:#111;color:#eee}}
article{{border:1px solid #444;border-radius:12px;padding:16px;margin:0 0 20px}}
.frames{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}}
img{{width:100%;height:180px;object-fit:cover;border-radius:8px;background:#222}}
</style></head><body><h1>VClip Visual Review</h1>{''.join(cards)}</body></html>"""
        path.write_text(document, encoding="utf-8")

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)
