#!/usr/bin/env python3
"""Local visual-coherence evidence for VClip reconstruction.

Uses Apple's on-device Vision feature prints (via a tiny Swift helper) plus
historical editorial-anchor support and flight-telemetry travel distance.

This module deliberately does NOT decide whether a shot is "beautiful".
It asks a narrower question that telemetry cannot answer:

    Does this candidate appear to remain one visually coherent shot,
    or does it contain a composition/regime change worth cutting/reviewing?

No network/API is used. Source-video frames are cached at low resolution.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


@dataclass
class VisualAssessment:
    status: str  # COHERENT | ADVISORY | TRANSITION | NO_VISUAL
    reasons: list[str]
    metrics: dict[str, Any]
    suggested_action: str | None = None
    suggested_boundary_s: float | None = None


@dataclass(frozen=True)
class VisualSettings:
    fps: float = 2.0
    width: int = 320
    min_frames: int = 8
    split_min_segment_s: float = 2.5

    # Relative feature-print separation. Absolute Vision feature-print distances
    # vary by imagery, so classification leans heavily on ratios/structure.
    transition_split_ratio: float = 1.55
    advisory_split_ratio: float = 1.32

    # A candidate that travels a long way while its visual composition splits
    # into distinct regimes deserves more suspicion than the same feature drift
    # during a near-static hover.
    transition_travel_m: float = 18.0
    advisory_travel_m: float = 30.0

    # Path inefficiency catches "leave composition A, pass through something
    # else, arrive at composition B/A-like content" without requiring a hard
    # adjacent-frame jump.
    transition_path_efficiency: float = 0.42
    advisory_path_efficiency: float = 0.60

    # Historical boundary votes are supporting evidence, never sole evidence.
    boundary_tolerance_s: float = 0.80


def _stable_media_key(media: Path) -> str:
    try:
        st = media.stat()
        payload = f"{media.resolve()}\n{st.st_size}\n{st.st_mtime_ns}"
    except OSError:
        payload = str(media)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def ensure_source_frames(
    media: Path,
    cache_root: Path,
    *,
    ffmpeg: str,
    settings: VisualSettings,
) -> tuple[Path, list[Path]]:
    """Extract deterministic low-resolution JPEG frames for one source video."""
    key = _stable_media_key(media)
    frame_dir = cache_root / "frames" / key
    manifest = frame_dir / "manifest.json"

    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if (
                data.get("media") == str(media)
                and float(data.get("fps", 0)) == settings.fps
                and int(data.get("width", 0)) == settings.width
            ):
                frames = sorted(frame_dir.glob("frame-*.jpg"))
                if frames:
                    return frame_dir, frames
        except Exception:
            pass

    frame_dir.mkdir(parents=True, exist_ok=True)
    for old in frame_dir.glob("frame-*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass

    pattern = str(frame_dir / "frame-%06d.jpg")
    vf = (
        f"fps={settings.fps}:start_time=0,"
        f"scale={settings.width}:-2:flags=lanczos"
    )
    # Preserve the proven visual pipeline exactly (original DJI media,
    # same 1 fps sampling, same Lanczos resize, same JPEG quality), changing
    # only the decoder. VideoToolbox avoids software-decoding the entire 4K
    # source just to keep one tiny frame per second.
    hw_cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-hwaccel",
        "videotoolbox",
        "-i",
        str(media),
        "-vf",
        vf,
        "-q:v",
        "3",
        pattern,
    ]
    result = _run(hw_cmd)
    decode_mode = "videotoolbox"

    if result.returncode != 0:
        # Portable fail-safe: fall back to the exact original command.
        sw_cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media),
            "-vf",
            vf,
            "-q:v",
            "3",
            pattern,
        ]
        result = _run(sw_cmd)
        decode_mode = "software_fallback"

    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg visual frame extraction failed for {media}: "
            + result.stderr.decode("utf-8", errors="replace")[-1200:]
        )

    frames = sorted(frame_dir.glob("frame-*.jpg"))
    if not frames:
        raise RuntimeError(f"ffmpeg produced no visual frames for {media}")

    manifest.write_text(
        json.dumps(
            {
                "media": str(media),
                "fps": settings.fps,
                "width": settings.width,
                "frame_count": len(frames),
                "visual_source_kind": "original",
                "decode_mode": decode_mode,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return frame_dir, frames


def frame_time(index_zero_based: int, fps: float) -> float:
    return index_zero_based / fps


def frames_for_range(
    frames: Sequence[Path],
    start_s: float,
    duration_s: float,
    fps: float,
) -> tuple[list[float], list[Path]]:
    end_s = start_s + duration_s
    times: list[float] = []
    selected: list[Path] = []
    for index, frame in enumerate(frames):
        t = frame_time(index, fps)
        if t + (0.51 / fps) < start_s:
            continue
        if t - (0.51 / fps) > end_s:
            break
        times.append(t)
        selected.append(frame)
    return times, selected


def vision_distance_matrix(
    helper: Path,
    frame_paths: Sequence[Path],
    cache_path: Path,
) -> list[list[float]]:
    """Return pairwise Apple Vision feature-print distances for selected frames."""
    if cache_path.is_file():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            matrix = payload.get("distances")
            if isinstance(matrix, list) and len(matrix) == len(frame_paths):
                return [[float(v) for v in row] for row in matrix]
        except Exception:
            pass

    if not helper.is_file():
        raise RuntimeError(f"Vision helper does not exist: {helper}")

    cmd = [str(helper), *[str(path) for path in frame_paths]]
    result = _run(cmd)
    if result.returncode != 0:
        raise RuntimeError(
            "Vision feature-print helper failed: "
            + result.stderr.decode("utf-8", errors="replace")[-1600:]
        )
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
        matrix = payload["distances"]
    except Exception as exc:
        raise RuntimeError(
            "Vision helper returned invalid JSON: "
            + result.stdout.decode("utf-8", errors="replace")[:1000]
        ) from exc

    out = [[float(v) for v in row] for row in matrix]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "frames": [str(path) for path in frame_paths],
                "distances": out,
            }
        ),
        encoding="utf-8",
    )
    return out


def _avg(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _avg_within(matrix: Sequence[Sequence[float]], indices: range) -> float:
    vals: list[float] = []
    ids = list(indices)
    for pos, i in enumerate(ids):
        for j in ids[pos + 1 :]:
            vals.append(float(matrix[i][j]))
    return _avg(vals)


def _avg_between(
    matrix: Sequence[Sequence[float]],
    left: range,
    right: range,
) -> float:
    return _avg(float(matrix[i][j]) for i in left for j in right)


def _support_count(
    source_t: float,
    source_anchors: Sequence[Any],
) -> int:
    count = 0
    for anchor in source_anchors:
        a0 = float(anchor.source_start_s)
        a1 = float(anchor.end_s)
        if a0 - 0.05 <= source_t <= a1 + 0.05:
            count += 1
    return count


def _boundary_votes(
    source_t: float,
    source_anchors: Sequence[Any],
    tolerance_s: float,
) -> int:
    votes = 0
    for anchor in source_anchors:
        if abs(float(anchor.source_start_s) - source_t) <= tolerance_s:
            votes += 1
        if abs(float(anchor.end_s) - source_t) <= tolerance_s:
            votes += 1
    return votes


def _integrate_travel(
    telemetry_samples: Sequence[Any],
    start_s: float,
    duration_s: float,
) -> tuple[float, float, float]:
    """Approximate horizontal and vertical path distance from ~10 Hz telemetry."""
    end_s = start_s + duration_s
    samples = [
        s
        for s in telemetry_samples
        if start_s - 0.05 <= float(s.t) <= end_s + 0.05
    ]
    if len(samples) < 2:
        return 0.0, 0.0, 0.0

    horizontal = 0.0
    vertical_signed = 0.0
    vertical_path = 0.0
    for a, b in zip(samples, samples[1:]):
        dt = max(0.0, float(b.t) - float(a.t))
        if dt <= 0:
            continue
        h = (abs(float(a.h_speed)) + abs(float(b.h_speed))) * 0.5
        z = (float(a.z_speed) + float(b.z_speed)) * 0.5
        horizontal += h * dt
        vertical_signed += z * dt
        vertical_path += abs(z) * dt
    return horizontal, vertical_signed, vertical_path


def _path_metrics(matrix: Sequence[Sequence[float]]) -> tuple[float, float, float]:
    n = len(matrix)
    if n < 2:
        return 0.0, 0.0, 1.0
    adjacent = [float(matrix[i][i + 1]) for i in range(n - 1)]
    path = sum(adjacent)
    net = float(matrix[0][n - 1])
    efficiency = net / path if path > 1e-9 else 1.0
    return path, net, efficiency


def _middle_excursion(matrix: Sequence[Sequence[float]]) -> float:
    n = len(matrix)
    if n < 3:
        return 0.0
    # How far does the middle get from BOTH endpoint compositions?
    return max(
        min(float(matrix[0][i]), float(matrix[i][n - 1]))
        for i in range(1, n - 1)
    )


def _candidate_splits(
    times: Sequence[float],
    matrix: Sequence[Sequence[float]],
    *,
    start_s: float,
    source_anchors: Sequence[Any],
    settings: VisualSettings,
) -> list[dict[str, float]]:
    n = len(times)
    if n < 6:
        return []

    rows: list[dict[str, float]] = []
    for k in range(2, n - 2):
        left_duration = times[k - 1] - times[0]
        right_duration = times[-1] - times[k]
        if (
            left_duration < settings.split_min_segment_s
            or right_duration < settings.split_min_segment_s
        ):
            continue

        left = range(0, k)
        right = range(k, n)
        within_left = _avg_within(matrix, left)
        within_right = _avg_within(matrix, right)
        between = _avg_between(matrix, left, right)
        within = (within_left + within_right) * 0.5
        ratio = between / max(0.025, within)
        contrast = between - within

        boundary_source_t = times[k]
        votes = _boundary_votes(
            boundary_source_t,
            source_anchors,
            settings.boundary_tolerance_s,
        )

        left_support = _avg(
            _support_count(t, source_anchors) for t in times[:k]
        )
        right_support = _avg(
            _support_count(t, source_anchors) for t in times[k:]
        )
        support_imbalance = (
            abs(left_support - right_support) / max(1.0, left_support, right_support)
        )

        # Ranking is intentionally relative. Editorial boundary/support evidence
        # helps choose *where* to inspect, but cannot manufacture a transition.
        rank_score = (
            ratio
            + min(0.45, contrast)
            + min(0.60, votes * 0.15)
            + min(0.35, support_imbalance * 0.35)
        )
        rows.append(
            {
                "frame_index": float(k),
                "source_boundary_s": float(boundary_source_t),
                "clip_boundary_s": float(boundary_source_t - start_s),
                "within_left": within_left,
                "within_right": within_right,
                "between": between,
                "split_ratio": ratio,
                "split_contrast": contrast,
                "boundary_votes": float(votes),
                "left_support": left_support,
                "right_support": right_support,
                "support_imbalance": support_imbalance,
                "rank_score": rank_score,
            }
        )
    rows.sort(key=lambda row: row["rank_score"], reverse=True)
    return rows


def assess_visual_coherence(
    *,
    media: Path,
    start_s: float,
    duration_s: float,
    source_anchors: Sequence[Any],
    telemetry_samples: Sequence[Any],
    cache_root: Path,
    vision_helper: Path,
    ffmpeg: str,
    min_duration_s: float,
    settings: VisualSettings,
) -> VisualAssessment:
    """Assess whether a candidate looks like one visual shot.

    This is conservative and evidence-oriented. TRANSITION means the visual
    feature-print timeline forms distinct regimes strongly enough that a full
    Ready Cut should not be auto-certified. ADVISORY means the imagery evolves
    substantially and deserves review.
    """
    try:
        _frame_dir, all_frames = ensure_source_frames(
            media,
            cache_root,
            ffmpeg=ffmpeg,
            settings=settings,
        )
        times, frame_paths = frames_for_range(
            all_frames,
            start_s,
            duration_s,
            settings.fps,
        )
        if len(frame_paths) < settings.min_frames:
            return VisualAssessment(
                "NO_VISUAL",
                ["insufficient_visual_samples"],
                {
                    "frame_count": len(frame_paths),
                    "fps": settings.fps,
                },
            )

        candidate_key = hashlib.sha1(
            (
                f"{_stable_media_key(media)}|{start_s:.6f}|{duration_s:.6f}|"
                f"{settings.fps}|{settings.width}|"
                + "|".join(path.name for path in frame_paths)
            ).encode("utf-8")
        ).hexdigest()[:24]
        matrix = vision_distance_matrix(
            vision_helper,
            frame_paths,
            cache_root / "distances" / f"{candidate_key}.json",
        )

        path_length, start_end, path_eff = _path_metrics(matrix)
        excursion = _middle_excursion(matrix)
        adjacent = [
            float(matrix[i][i + 1])
            for i in range(len(matrix) - 1)
        ]
        adjacent_mean = _avg(adjacent)
        adjacent_max = max(adjacent, default=0.0)

        splits = _candidate_splits(
            times,
            matrix,
            start_s=start_s,
            source_anchors=source_anchors,
            settings=settings,
        )
        best = splits[0] if splits else None

        horizontal_m, vertical_signed_m, vertical_path_m = _integrate_travel(
            telemetry_samples,
            start_s,
            duration_s,
        )

        metrics: dict[str, Any] = {
            "frame_count": len(frame_paths),
            "visual_fps": settings.fps,
            "visual_start_end_distance": start_end,
            "visual_path_length": path_length,
            "visual_path_efficiency": path_eff,
            "visual_middle_excursion": excursion,
            "adjacent_distance_mean": adjacent_mean,
            "adjacent_distance_max": adjacent_max,
            "horizontal_travel_m": horizontal_m,
            "vertical_signed_travel_m": vertical_signed_m,
            "vertical_path_m": vertical_path_m,
            "frames": [str(path) for path in frame_paths],
        }
        if best:
            metrics["best_split"] = best
            metrics["top_splits"] = splits[:5]

        reasons: list[str] = []
        status = "COHERENT"

        if best:
            ratio = float(best["split_ratio"])
            contrast = float(best["split_contrast"])
            votes = int(best["boundary_votes"])
            support_imbalance = float(best["support_imbalance"])

            strong_split = (
                ratio >= settings.transition_split_ratio
                and (
                    horizontal_m >= settings.transition_travel_m
                    or votes >= 1
                    or support_imbalance >= 0.30
                    or path_eff <= settings.transition_path_efficiency
                )
            )
            if strong_split:
                status = "TRANSITION"
                reasons.append("visual_regime_split")
                if horizontal_m >= settings.transition_travel_m:
                    reasons.append("visual_split_with_large_translation")
                if votes:
                    reasons.append("visual_split_near_editorial_boundary")
                if support_imbalance >= 0.30:
                    reasons.append("editorial_support_changes_across_visual_split")
            elif ratio >= settings.advisory_split_ratio:
                status = "ADVISORY"
                reasons.append("visual_regime_advisory")

        if (
            path_eff <= settings.transition_path_efficiency
            and excursion > max(0.10, adjacent_mean * 2.0)
        ):
            status = "TRANSITION"
            reasons.extend(
                ["visual_composition_excursion", "low_visual_path_efficiency"]
            )
        elif (
            status == "COHERENT"
            and path_eff <= settings.advisory_path_efficiency
            and excursion > max(0.08, adjacent_mean * 1.5)
        ):
            status = "ADVISORY"
            reasons.extend(
                ["visual_composition_excursion_advisory", "visual_path_inefficiency"]
            )

        if (
            status == "COHERENT"
            and horizontal_m >= settings.advisory_travel_m
            and best
            and float(best["split_ratio"]) >= 1.20
        ):
            status = "ADVISORY"
            reasons.append("large_translation_with_visual_drift")

        suggested_action: str | None = None
        boundary: float | None = None
        if status == "TRANSITION" and best:
            boundary = float(best["clip_boundary_s"])
            prefix = boundary
            suffix = duration_s - boundary

            left_support = float(best["left_support"])
            right_support = float(best["right_support"])
            within_left = float(best["within_left"])
            within_right = float(best["within_right"])

            prefix_viable = prefix >= min_duration_s - 0.30
            suffix_viable = suffix >= min_duration_s - 0.30

            if prefix_viable and not suffix_viable:
                suggested_action = "visual_trim_end"
            elif suffix_viable and not prefix_viable:
                suggested_action = "visual_trim_start"
            elif prefix_viable and suffix_viable:
                # Prefer the side with stronger prior human support and tighter
                # internal visual similarity. If neither wins clearly, do not
                # pretend we know which half is the stock shot.
                left_quality = left_support / max(0.05, within_left)
                right_quality = right_support / max(0.05, within_right)
                if left_quality >= right_quality * 1.18:
                    suggested_action = "visual_trim_end"
                elif right_quality >= left_quality * 1.18:
                    suggested_action = "visual_trim_start"
                else:
                    suggested_action = "visual_split_or_review"
            else:
                suggested_action = "visual_review"

        return VisualAssessment(
            status=status,
            reasons=sorted(set(reasons)),
            metrics=metrics,
            suggested_action=suggested_action,
            suggested_boundary_s=boundary,
        )
    except Exception as exc:
        return VisualAssessment(
            "NO_VISUAL",
            [f"visual_analysis_error:{type(exc).__name__}"],
            {"error": str(exc)},
        )


def visual_trim_ranges(
    assessment: VisualAssessment,
    *,
    start_s: float,
    duration_s: float,
    min_duration_s: float,
) -> list[tuple[float, float, str]]:
    """Return non-destructive repair candidates from a visual transition."""
    boundary = assessment.suggested_boundary_s
    if boundary is None or assessment.suggested_action is None:
        return []

    prefix = boundary
    suffix = duration_s - boundary
    out: list[tuple[float, float, str]] = []

    if assessment.suggested_action == "visual_trim_end":
        if prefix >= min_duration_s - 0.30:
            out.append((start_s, prefix, "visual-trim-end"))
    elif assessment.suggested_action == "visual_trim_start":
        if suffix >= min_duration_s - 0.30:
            out.append((start_s + boundary, suffix, "visual-trim-start"))
    elif assessment.suggested_action == "visual_split_or_review":
        if prefix >= min_duration_s - 0.30:
            out.append((start_s, prefix, "visual-split-a"))
        if suffix >= min_duration_s - 0.30:
            out.append((start_s + boundary, suffix, "visual-split-b"))
    return out


def assessment_payload(value: VisualAssessment) -> dict[str, Any]:
    return asdict(value)
