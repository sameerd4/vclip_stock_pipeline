"""Frame sampling and visual enrichment of final approved exports."""

from __future__ import annotations

import html
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..util import sha256_file, stable_id, utc_now
from .catalog import WorkflowCatalog
from .frames import SAMPLER_VERSION, FrameSampler
from .models import (
    FrameSampleSet,
    ProviderUsage,
    VisualAnalysis,
    VisualAnalysisResult,
)
from .pricing import PRICING_VERSION, pricing_manifest
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
    ) -> VisualAnalysisResult: ...


@dataclass
class UsageTotals:
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = None
    missing_usage_responses: int = 0
    unpriced_requests: int = 0

    def add(self, usage: ProviderUsage | None) -> None:
        if usage is None:
            return
        self.requests += 1
        if usage.usage_missing:
            self.missing_usage_responses += 1
        self.input_tokens += int(usage.input_tokens or 0)
        self.cached_input_tokens += int(usage.cached_input_tokens or 0)
        self.output_tokens += int(usage.output_tokens or 0)
        self.reasoning_tokens += int(usage.reasoning_tokens or 0)
        self.total_tokens += int(usage.total_tokens or 0)
        if usage.estimated_total_cost_usd is None:
            if not usage.usage_missing:
                self.unpriced_requests += 1
            return
        if self.estimated_cost_usd is None:
            self.estimated_cost_usd = 0.0
        self.estimated_cost_usd += float(usage.estimated_total_cost_usd)


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
    usage: UsageTotals = field(default_factory=UsageTotals)
    warnings: list[str] = field(default_factory=list)
    pricing_version: str = PRICING_VERSION


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
                    "pricing": pricing_manifest(),
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
                            self._report_clip(row, samples, None, None, analysis_key)
                        )
                        continue
                    result = self.analyzer.analyze(
                        samples.frames,
                        context=self._context(row),
                    )
                    analysis = result.analysis
                    usage = result.usage
                    report.usage.add(usage)
                    if usage is not None and usage.usage_missing:
                        warning = (
                            f"{clip_id}: OpenAI response omitted usage; "
                            "tokens/cost stored as null"
                        )
                        report.warnings.append(warning)
                        self._announce(f"warning: {warning}")
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
                            usage=usage,
                        )
                    report.analyzed += 1
                    report.clips.append(
                        self._report_clip(
                            row, samples, analysis, usage, analysis_key
                        )
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
        usage: ProviderUsage | None,
        analysis_key: str,
    ) -> dict[str, Any]:
        # Markets are persisted separately; surface session location for the report.
        location_label = row.get("public_label") or row.get("city")
        duration = (
            row.get("export_duration_seconds")
            or row.get("final_duration_seconds")
            or row.get("proposed_duration_seconds")
        )
        return {
            "stockify_run_id": row["stockify_run_id"],
            "stock_clip_id": row["stock_clip_id"],
            "exported_path": row["exported_path"],
            "exported_filename": row.get("exported_filename"),
            "project_label": row.get("generated_project_label")
            or row.get("generated_clip_project_name")
            or row.get("source_name"),
            "analysis_key": analysis_key,
            "frames": [str(path) for path in samples.frames],
            "caption": analysis.caption if analysis else None,
            "tags": [asdict(tag) for tag in analysis.tags] if analysis else [],
            "named_subjects": (
                [asdict(subject) for subject in analysis.named_subjects]
                if analysis
                else []
            ),
            "duration_seconds": duration,
            "orientation": row.get("orientation"),
            "market_label": location_label,
            "city": row.get("city"),
            "neighborhood": row.get("neighborhood"),
            "state": row.get("state"),
            "public_label": row.get("public_label"),
            "time_of_day": row.get("time_of_day"),
            "usage": usage.as_dict() if usage is not None else None,
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
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        usage = report.usage
        cost_label = (
            "n/a"
            if usage.estimated_cost_usd is None
            else f"${usage.estimated_cost_usd:.4f}"
        )
        summary = (
            "<section class='summary'>"
            f"<p><strong>Provider:</strong> {html.escape(report.provider)} "
            f"· <strong>Model:</strong> {html.escape(report.model)}</p>"
            f"<p><strong>Analyzed:</strong> {report.analyzed} "
            f"· <strong>Cached:</strong> {report.cached} "
            f"· <strong>Failed:</strong> {report.failed}</p>"
            f"<p><strong>Estimated OpenAI cost:</strong> {html.escape(cost_label)} "
            f"· <strong>Requests:</strong> {usage.requests} "
            f"· <strong>Total tokens:</strong> {usage.total_tokens:,}</p>"
            "</section>"
        )
        cards: list[str] = []
        for clip in report.clips:
            images = "".join(
                "<img "
                f'src="{html.escape(portable_frame_src(path, frame))}" '
                'loading="lazy">'
                for frame in clip.get("frames", [])
            )
            tags = ", ".join(
                f"{tag['tag']} ({tag['strength']})"
                for tag in clip.get("tags", [])
            )
            named = ", ".join(
                str(item.get("raw_name") or item.get("name") or "")
                for item in clip.get("named_subjects", [])
                if item.get("raw_name") or item.get("name")
            )
            canonical = ", ".join(
                sorted(
                    {
                        str(item.get("canonical_label"))
                        for item in clip.get("named_subjects", [])
                        if item.get("canonical_label")
                    }
                )
            )
            clip_usage = clip.get("usage") or {}
            clip_cost = clip_usage.get("estimated_total_cost_usd")
            clip_cost_text = (
                "n/a" if clip_cost is None else f"${float(clip_cost):.4f}"
            )
            duration = clip.get("duration_seconds")
            duration_text = (
                "n/a" if duration is None else f"{float(duration):.1f}s"
            )
            location = (
                clip.get("public_label")
                or clip.get("market_label")
                or clip.get("city")
                or "Unknown location"
            )
            project = clip.get("project_label") or clip.get("exported_filename") or "Clip"
            cards.append(
                "<article>"
                f"<h2>{html.escape(str(project))}</h2>"
                f"<p class='meta'><strong>VCLIP:</strong> "
                f"{html.escape(str(clip['stock_clip_id']))}</p>"
                f"<p>{html.escape(str(clip.get('caption') or 'Frames only'))}</p>"
                f"<p><strong>Tags:</strong> {html.escape(tags or '—')}</p>"
                f"<p><strong>Named subjects:</strong> {html.escape(named or '—')}</p>"
                f"<p><strong>Canonical entities:</strong> "
                f"{html.escape(canonical or '—')}</p>"
                f"<p><strong>Duration:</strong> {html.escape(duration_text)} "
                f"· <strong>Orientation:</strong> "
                f"{html.escape(str(clip.get('orientation') or '—'))} "
                f"· <strong>Location:</strong> {html.escape(str(location))} "
                f"· <strong>Est. cost:</strong> {html.escape(clip_cost_text)}</p>"
                f"<div class='frames'>{images}</div>"
                "</article>"
            )
        document = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>VClip Visual Review</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:24px;background:#111;color:#eee}}
.summary{{border:1px solid #555;border-radius:12px;padding:16px;margin:0 0 20px;background:#1a1a1a}}
article{{border:1px solid #444;border-radius:12px;padding:16px;margin:0 0 20px}}
.meta{{color:#bbb}}
.frames{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}}
img{{width:100%;height:180px;object-fit:cover;border-radius:8px;background:#222}}
</style></head><body><h1>VClip Visual Review</h1>{summary}{''.join(cards)}</body></html>"""
        path.write_text(document, encoding="utf-8")

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)


def portable_frame_src(html_path: Path, frame_path: str | Path) -> str:
    """Return a host-portable relative img src for a frame beside the HTML file.

    Absolute ``file:///work/...`` Docker paths break when the report is opened
    on the host. Relative paths remain valid as long as the frame cache and HTML
    stay in the same filesystem layout.
    """
    html_parent = html_path.expanduser().resolve().parent
    frame = Path(frame_path).expanduser()
    try:
        resolved = frame.resolve()
    except OSError:
        resolved = frame
    try:
        return Path(os.path.relpath(resolved, html_parent)).as_posix()
    except ValueError:
        # Different drives (Windows): fall back to the filename only.
        return resolved.name


def format_openai_usage_block(report: EnrichmentReport) -> list[str]:
    """CLI lines for aggregate OpenAI usage/cost."""
    usage = report.usage
    if usage.requests <= 0:
        return []
    cost = usage.estimated_cost_usd
    if cost is None:
        cost_line = "Estimated cost:      n/a"
        avg_line = "Avg cost / clip:     n/a"
    else:
        avg = cost / usage.requests
        cost_line = f"Estimated cost:      ${cost:.2f}"
        avg_line = f"Avg cost / clip:     ${avg:.4f}"
    return [
        "OpenAI usage",
        "------------",
        f"Requests:             {usage.requests}",
        f"Input tokens:       {usage.input_tokens:,}",
        f"Cached input:       {usage.cached_input_tokens:,}",
        f"Output tokens:      {usage.output_tokens:,}",
        f"Reasoning tokens:    {usage.reasoning_tokens:,}",
        cost_line,
        avg_line,
    ]
