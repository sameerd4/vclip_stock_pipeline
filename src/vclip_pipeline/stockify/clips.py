"""Classify, clean, and safely expand source clips before project generation."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Callable

from .constants import (
    PRESERVED_VIDEO_CHILD_TAGS,
    STRIPPED_CHILD_TAGS,
    SUPPORTED_STOCK_SEGMENT_TAGS,
    VIDEO_CLIP_TAGS,
)
from .core import format_time, parse_time
from .fcpxml import (
    asset_has_media_rep,
    asset_has_video,
    asset_is_still_image,
    direct_children_by_name,
    existing_asset_media_path,
    first_direct_child,
    has_descendant_named,
    is_custom_lut_filter,
    local_name,
    referenced_asset,
)
from .models import (
    RecoveryResult,
    SidecarIndex,
    SrtInfo,
    SrtWindowScore,
    StockifyError,
    VisualMotionScore,
)
from .scoring import score_srt_window, score_visual_window, visual_metrics_dict
from .sidecars import parse_srt_info, sidecar_for_asset


# Clip classification and cleanup

# Check whether a clip or composite visibly contains video.
def clip_has_video(
    clip: ET.Element,
    resource_index: dict[str, ET.Element],
) -> bool:
    tag = local_name(clip.tag)
    if tag in {"asset-clip", "video"}:
        return asset_has_video(referenced_asset(clip, resource_index))
    if tag in {"clip", "sync-clip", "mc-clip", "ref-clip"}:
        # Composite clips are accepted only if they visibly contain video.
        return any(
            local_name(node.tag) in {"video", "asset-clip"}
            for node in clip.iter()
            if node is not clip
        )
    return False


# Accept only clips whose nearest structural parent is the main spine.
def is_primary_storyline_candidate(
    node: ET.Element,
    parent_map: dict[ET.Element, ET.Element],
) -> bool:
    """
    Accept clips whose nearest structural parent is the main spine.

    This avoids turning connected titles, overlays, and B-roll into separate
    stock projects by default.
    """
    current = parent_map.get(node)
    while current is not None:
        tag = local_name(current.tag)
        if tag == "spine":
            return True
        if tag in VIDEO_CLIP_TAGS or tag in {"title", "generator", "caption"}:
            return False
        current = parent_map.get(current)
    return False


# Infer a normal-speed source range from a simple time map.
def time_map_source_range(time_map: ET.Element) -> tuple[Fraction, Fraction] | None:
    """
    Infer a source range from the first and last timept values.

    This is suitable for flattening a retimed selection back to normal speed.
    It intentionally does not preserve speed ramps.
    """
    points: list[tuple[Fraction, Fraction]] = []
    for point in direct_children_by_name(time_map, "timept"):
        try:
            timeline_time = parse_time(point.get("time"))
            source_value = parse_time(point.get("value"))
        except ValueError:
            continue
        points.append((timeline_time, source_value))

    if len(points) < 2:
        return None

    points.sort(key=lambda pair: pair[0])
    source_start = points[0][1]
    source_end = points[-1][1]
    if source_end <= source_start:
        return None
    return source_start, source_end - source_start


# Copy a clip, remove editorial structure, and keep video treatments.
def clean_clip_for_stock(
    source_clip: ET.Element,
    *,
    resource_index: dict[str, ET.Element],
    force_disable_audio: bool,
    replacement_ref: str | None = None,
) -> tuple[ET.Element, list[str], bool, bool]:
    """
    Produce a clean, standalone clip.

    Returns:
      (clean_clip, warnings, had_time_map, retime_normalized)
    """
    clip = copy.deepcopy(source_clip)
    warnings: list[str] = []

    original_offset = clip.get("offset")
    clip.set("offset", "0s")
    if replacement_ref:
        clip.set("ref", replacement_ref)
        warnings.append(
            f"Replaced malformed source asset reference with {replacement_ref}."
        )

    if force_disable_audio and "audioRole" in clip.attrib:
        del clip.attrib["audioRole"]

    # Remove editorial children that do not belong in a standalone stock segment.
    for child in list(clip):
        tag = local_name(child.tag)

        if tag == "timeMap":
            raise StockifyError("unsupported retime")

        if tag in PRESERVED_VIDEO_CHILD_TAGS or tag.startswith("adjust-"):
            continue

        if tag in STRIPPED_CHILD_TAGS or tag in VIDEO_CLIP_TAGS:
            clip.remove(child)
            continue

        if tag in {"filter-audio", "spine"}:
            clip.remove(child)
            continue

        # Unknown direct children are treated as editorial structure rather than
        # media essence. Opaque video treatments are represented by filter-video
        # and adjust-* nodes, which are preserved above.
        clip.remove(child)

    time_map = first_direct_child(clip, "timeMap")
    had_time_map = time_map is not None
    retime_normalized = False

    if time_map is not None:
        raise StockifyError("unsupported retime")

    if "duration" not in clip.attrib:
        asset = referenced_asset(clip, resource_index)
        if asset is not None and asset.get("duration"):
            clip.set("duration", asset.get("duration", "1s"))
            warnings.append("Clip duration was missing and inherited from its asset.")
        else:
            raise StockifyError(
                f"Cannot determine duration for clip {clip.get('name')!r}."
            )

    if not clip.get("start"):
        clip.set("start", "0s")

    if original_offset and original_offset != "0s":
        # Informational only; the new project starts at zero by design.
        pass

    return clip, warnings, had_time_map, retime_normalized


# Keep only Custom LUT filter-video nodes on emitted review clips.
def sanitize_review_clip_effects(
    clip: ET.Element,
    resource_index: dict[str, ET.Element] | None = None,
) -> ET.Element:
    """Strip non-LUT video effect plugins from review XML only.

    Source XML and in-memory analysis provenance are left untouched; callers
    should pass a deepcopy destined for emission.
    """
    for child in list(clip):
        if local_name(child.tag) != "filter-video":
            continue
        if not is_custom_lut_filter(child, resource_index):
            clip.remove(child)
    return clip


# Return the first conservative reason a source segment cannot be stockified.
def candidate_skip_reason(
    source_clip: ET.Element,
    resource_index: dict[str, ET.Element],
    replacement_refs: dict[str, str],
) -> tuple[str | None, ET.Element | None, str | None]:
    tag = local_name(source_clip.tag)
    if tag not in SUPPORTED_STOCK_SEGMENT_TAGS:
        return "unsupported_clip_type", None, None
    if source_clip.get("lane") is not None:
        return "connected_clip", None, None
    if has_descendant_named(source_clip, "timeMap"):
        return "unsupported_retime", None, None

    ref = source_clip.get("ref")
    if not ref:
        return "missing_asset_reference", None, None

    asset = referenced_asset(source_clip, resource_index)
    if asset is None:
        return "missing_asset_reference", None, None
    if not asset_has_video(asset):
        return "non_video_element", asset, None
    if asset_is_still_image(asset):
        return "photo_asset", asset, None
    if not asset_has_media_rep(asset):
        replacement_ref = replacement_refs.get(ref)
        if not replacement_ref:
            return "asset_missing_media_rep", asset, None
        return None, asset, replacement_ref
    return None, asset, None


# Short-clip recovery

# Generate forward, centered, and backward expansion windows.
def candidate_windows_for_target(
    original_start: Fraction,
    original_duration: Fraction,
    target_duration: Fraction,
    asset_duration: Fraction,
) -> list[tuple[str, Fraction, Fraction]]:
    if target_duration <= original_duration or target_duration > asset_duration:
        return []

    original_end = original_start + original_duration
    starts = [
        ("forward", original_start),
        ("centered", original_start - ((target_duration - original_duration) / 2)),
        ("backward", original_end - target_duration),
    ]

    windows: list[tuple[str, Fraction, Fraction]] = []
    seen_starts: set[Fraction] = set()
    latest_start = asset_duration - target_duration
    for mode, start in starts:
        start = min(max(start, Fraction(0)), latest_start)
        end = start + target_duration
        if start <= original_start and end >= original_end and start not in seen_starts:
            windows.append((mode, start, target_duration))
            seen_starts.add(start)
    return windows


# Assign the output clip to a simple quality/review tier.
def recovery_tier(duration: Fraction, status: str) -> str:
    seconds = float(duration)
    if status == "not_applicable":
        if seconds >= 10:
            return "A_clean_10s"
        if seconds >= 5:
            return "B_clean_5s"
        if seconds >= 3:
            return "C_clean_3s"
        return "short_original"
    if status == "expanded":
        if seconds >= 10:
            return "Review_expanded_10s"
        if seconds >= 5:
            return "Review_expanded_5s"
        if seconds >= 3:
            return "Review_expanded_3s"
    return "Review_unexpanded_short"


# Try longer source handles while respecting telemetry and visual checks.
def recover_short_clip(
    clean_clip: ET.Element,
    source_clip: ET.Element,
    asset: ET.Element | None,
    *,
    sidecar_index: SidecarIndex | None,
    srt_cache: dict[Path, SrtInfo],
    enabled: bool,
    short_clip_threshold_seconds: float,
    minimum_duration_seconds: float,
    preferred_duration_seconds: float,
    ideal_duration_seconds: float,
    require_srt_for_expansion: bool,
    visual_score: bool = False,
    require_visual_for_expansion: bool = False,
    visual_fps: int = 12,
    visual_width: int = 320,
    visual_height: int = 180,
    visual_reject_shift_px: float = 12.0,
    visual_reject_frame_diff: float = 12.0,
    visual_timeout_seconds: float = 120.0,
    progress: Callable[[str], None] | None = None,
) -> RecoveryResult:
    original_start = parse_time(clean_clip.get("start"))
    original_duration = parse_time(clean_clip.get("duration"))
    sidecar_path = sidecar_for_asset(asset, sidecar_index)
    srt_status = "not_checked"
    srt_window_status = "not_checked"
    reasons: list[str] = []
    warnings: list[str] = []

    if sidecar_path is not None:
        srt_status = "matched"
    elif sidecar_index is not None:
        srt_status = "missing"

    if not enabled or float(original_duration) >= short_clip_threshold_seconds:
        return RecoveryResult(
            output_start=original_start,
            output_duration=original_duration,
            status="not_applicable",
            candidate_tier=recovery_tier(original_duration, "not_applicable"),
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            srt_status=srt_status,
            srt_window_status=srt_window_status,
            smoothness_reasons=tuple(reasons),
            warnings=tuple(warnings),
        )

    if asset is None or not asset.get("duration"):
        return RecoveryResult(
            output_start=original_start,
            output_duration=original_duration,
            status="no_asset_duration",
            candidate_tier=recovery_tier(original_duration, "unexpanded"),
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            srt_status=srt_status,
            srt_window_status=srt_window_status,
            smoothness_reasons=("missing_asset_duration",),
            warnings=("Short clip was not expanded because asset duration is unavailable.",),
        )

    asset_duration = parse_time(asset.get("duration"))
    if asset_duration <= original_duration:
        return RecoveryResult(
            output_start=original_start,
            output_duration=original_duration,
            status="not_enough_source_media",
            candidate_tier=recovery_tier(original_duration, "unexpanded"),
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            srt_status=srt_status,
            srt_window_status=srt_window_status,
            smoothness_reasons=("not_enough_source_media",),
            warnings=("Short clip was not expanded because the source media is too short.",),
        )

    srt_info: SrtInfo | None = None
    if sidecar_path is not None:
        if sidecar_path not in srt_cache:
            srt_cache[sidecar_path] = parse_srt_info(sidecar_path)
        srt_info = srt_cache[sidecar_path]
    elif require_srt_for_expansion:
        return RecoveryResult(
            output_start=original_start,
            output_duration=original_duration,
            status="missing_srt",
            candidate_tier=recovery_tier(original_duration, "unexpanded"),
            sidecar_path=None,
            srt_status=srt_status,
            srt_window_status="missing_srt",
            smoothness_reasons=("missing_srt",),
            warnings=("Short clip was not expanded because no matching SRT was found.",),
        )

    target_seconds = [
        ideal_duration_seconds,
        preferred_duration_seconds,
        minimum_duration_seconds,
    ]
    targets = sorted(
        {
            Fraction(str(seconds))
            for seconds in target_seconds
            if seconds > float(original_duration)
        },
        reverse=True,
    )

    media_path = existing_asset_media_path(asset) if visual_score else None
    first_reject: SrtWindowScore | None = None
    first_visual_reject: VisualMotionScore | None = None
    first_visual_unavailable: VisualMotionScore | None = None

    def rank_status(status: str) -> int:
        if status in {"clean", "not_checked"}:
            return 0
        if status == "review":
            return 1
        if status == "unavailable":
            return 2
        if status == "missing_srt":
            return 2
        return 3

    for target_duration in targets:
        viable_windows: list[
            tuple[
                tuple[int, int, int],
                str,
                Fraction,
                Fraction,
                SrtWindowScore | None,
                VisualMotionScore,
            ]
        ] = []

        for mode, candidate_start, candidate_duration in candidate_windows_for_target(
            original_start,
            original_duration,
            target_duration,
            asset_duration,
        ):
            score: SrtWindowScore | None = None
            if srt_info is not None:
                score = score_srt_window(srt_info, candidate_start, candidate_duration)
                if score.status == "reject":
                    if first_reject is None:
                        first_reject = score
                    continue

            visual = VisualMotionScore(status="not_checked")
            if visual_score:
                visual = score_visual_window(
                    media_path,
                    candidate_start,
                    candidate_duration,
                    fps=visual_fps,
                    width=visual_width,
                    height=visual_height,
                    reject_shift_px=visual_reject_shift_px,
                    reject_frame_diff=visual_reject_frame_diff,
                    timeout_seconds=visual_timeout_seconds,
                    progress=progress,
                )
                if visual.status == "unavailable":
                    if first_visual_unavailable is None:
                        first_visual_unavailable = visual
                    if require_visual_for_expansion:
                        continue
                elif visual.status == "reject":
                    if first_visual_reject is None:
                        first_visual_reject = visual
                    continue

            srt_window_status = score.status if score is not None else "missing_srt"
            # Earlier window modes stay deterministic when scores tie.
            mode_priority = {"forward": 0, "centered": 1, "backward": 2}.get(mode, 3)
            viable_windows.append(
                (
                    (
                        rank_status(visual.status),
                        rank_status(srt_window_status),
                        mode_priority,
                    ),
                    mode,
                    candidate_start,
                    candidate_duration,
                    score,
                    visual,
                )
            )

        if viable_windows:
            _rank, mode, candidate_start, candidate_duration, score, visual = sorted(
                viable_windows,
                key=lambda item: item[0],
            )[0]
            clean_clip.set("start", format_time(candidate_start))
            clean_clip.set("duration", format_time(candidate_duration))

            srt_window_status = score.status if score is not None else "missing_srt"
            reasons: list[str] = []
            if score is not None:
                reasons.extend(score.reasons)
            else:
                reasons.append("missing_srt")
            reasons.extend(visual.reasons)
            visual_status = visual.status
            visual_metrics = visual_metrics_dict(visual)

            clean_enough = (
                srt_window_status in {"clean", "not_checked"}
                and visual_status in {"clean", "not_checked"}
            )
            status = "expanded" if clean_enough else "expanded_review"
            if status == "expanded":
                warnings.append(
                    f"Expanded short clip to {float(candidate_duration):.3f}s "
                    f"using {mode} source handles."
                )
            else:
                warnings.append(
                    f"Expanded short clip to {float(candidate_duration):.3f}s "
                    f"using {mode} source handles; review recommended."
                )

            return RecoveryResult(
                output_start=candidate_start,
                output_duration=candidate_duration,
                status=status,
                candidate_tier=recovery_tier(candidate_duration, "expanded"),
                sidecar_path=str(sidecar_path) if sidecar_path else None,
                srt_status=srt_status,
                srt_window_status=srt_window_status,
                visual_status=visual_status,
                visual_reasons=visual.reasons,
                visual_metrics=visual_metrics,
                smoothness_reasons=tuple(reasons),
                warnings=tuple(warnings),
            )

        if srt_info is None and not require_srt_for_expansion:
            for mode, candidate_start, candidate_duration in candidate_windows_for_target(
                original_start,
                original_duration,
                target_duration,
                asset_duration,
            ):
                visual = VisualMotionScore(status="not_checked")
                if visual_score:
                    visual = score_visual_window(
                        media_path,
                        candidate_start,
                        candidate_duration,
                        fps=visual_fps,
                        width=visual_width,
                        height=visual_height,
                        reject_shift_px=visual_reject_shift_px,
                        reject_frame_diff=visual_reject_frame_diff,
                    )
                    if visual.status == "unavailable":
                        if first_visual_unavailable is None:
                            first_visual_unavailable = visual
                        if require_visual_for_expansion:
                            continue
                    elif visual.status == "reject":
                        if first_visual_reject is None:
                            first_visual_reject = visual
                        continue

                clean_clip.set("start", format_time(candidate_start))
                clean_clip.set("duration", format_time(candidate_duration))
                warnings.append(
                    f"Expanded short clip to {float(candidate_duration):.3f}s "
                    f"using {mode} source handles without SRT verification."
                )
                return RecoveryResult(
                    output_start=candidate_start,
                    output_duration=candidate_duration,
                    status="expanded_review",
                    candidate_tier=recovery_tier(candidate_duration, "expanded"),
                    sidecar_path=None,
                    srt_status=srt_status,
                    srt_window_status="missing_srt",
                    visual_status=visual.status,
                    visual_reasons=visual.reasons,
                    visual_metrics=visual_metrics_dict(visual),
                    smoothness_reasons=tuple(["missing_srt", *visual.reasons]),
                    warnings=tuple(warnings),
                )

    if require_visual_for_expansion and first_visual_unavailable is not None:
        reason_text = ", ".join(first_visual_unavailable.reasons) or "unknown"
        return RecoveryResult(
            output_start=original_start,
            output_duration=original_duration,
            status="visual_unavailable_expansion",
            candidate_tier=recovery_tier(original_duration, "unexpanded"),
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            srt_status=srt_status,
            srt_window_status="not_checked",
            visual_status=first_visual_unavailable.status,
            visual_reasons=first_visual_unavailable.reasons,
            visual_metrics=visual_metrics_dict(first_visual_unavailable),
            smoothness_reasons=first_visual_unavailable.reasons,
            warnings=(
                "Short clip was not expanded because visual scoring was required "
                f"but unavailable: {reason_text}.",
            ),
        )

    if first_visual_reject is not None:
        return RecoveryResult(
            output_start=original_start,
            output_duration=original_duration,
            status="visual_rejected_expansion",
            candidate_tier=recovery_tier(original_duration, "unexpanded"),
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            srt_status=srt_status,
            srt_window_status="not_checked",
            visual_status=first_visual_reject.status,
            visual_reasons=first_visual_reject.reasons,
            visual_metrics=visual_metrics_dict(first_visual_reject),
            smoothness_reasons=first_visual_reject.reasons,
            warnings=(
                "Short clip was not expanded because visual motion scoring "
                "rejected the available expansion windows.",
            ),
        )

    if first_reject is not None:
        return RecoveryResult(
            output_start=original_start,
            output_duration=original_duration,
            status="srt_rejected_expansion",
            candidate_tier=recovery_tier(original_duration, "unexpanded"),
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            srt_status=srt_status,
            srt_window_status=first_reject.status,
            smoothness_reasons=first_reject.reasons,
            warnings=(
                "Short clip was not expanded because SRT telemetry rejected "
                "the available expansion windows.",
            ),
        )

    return RecoveryResult(
        output_start=original_start,
        output_duration=original_duration,
        status="not_enough_source_media",
        candidate_tier=recovery_tier(original_duration, "unexpanded"),
        sidecar_path=str(sidecar_path) if sidecar_path else None,
        srt_status=srt_status,
        srt_window_status=srt_window_status,
        smoothness_reasons=("not_enough_source_media",),
        warnings=("Short clip was not expanded because no target duration fit.",),
    )
