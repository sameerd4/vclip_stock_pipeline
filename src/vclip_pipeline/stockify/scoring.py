"""Score candidate source windows using DJI telemetry and optional decoded video frames."""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from fractions import Fraction
from pathlib import Path

from .metadata import (
    angular_delta_degrees,
    bearing_degrees,
    haversine_meters,
    is_usable_gps,
)
from .models import (
    SrtInfo,
    SrtSample,
    SrtWindowScore,
    VisualMotionScore,
    VisualPreflightReport,
)

# Telemetry scoring

# Thin dense telemetry to a stable comparison interval.
def downsample_srt_samples(
    samples: Iterable[SrtSample],
    *,
    interval: Fraction = Fraction(1, 2),
) -> list[SrtSample]:
    selected: list[SrtSample] = []
    next_time: Fraction | None = None
    for sample in samples:
        if next_time is None or sample.time >= next_time:
            selected.append(sample)
            next_time = sample.time + interval
    return selected


# Flag abrupt speed, altitude, or direction changes in an SRT window.
def score_srt_window(
    srt_info: SrtInfo,
    start: Fraction,
    duration: Fraction,
) -> SrtWindowScore:
    end = start + duration
    if srt_info.sample_count == 0:
        return SrtWindowScore(
            status="review",
            sample_count=0,
            coverage=0.0,
            reasons=("empty_srt",),
        )

    window_samples = [
        sample for sample in srt_info.samples
        if start <= sample.time <= end
    ]
    reasons: list[str] = []

    coverage_start = max(start, srt_info.start)
    coverage_end = min(end, srt_info.end)
    coverage = 0.0
    if duration > 0:
        coverage = max(0.0, float((coverage_end - coverage_start) / duration))
    if coverage < 0.8:
        reasons.append("partial_srt_coverage")

    if not srt_info.has_orientation:
        reasons.append("srt_missing_orientation_fields")

    if not srt_info.has_position and not srt_info.has_altitude:
        return SrtWindowScore(
            status="review",
            sample_count=len(window_samples),
            coverage=coverage,
            reasons=tuple(reasons + ["srt_has_no_motion_fields"]),
        )

    reduced = downsample_srt_samples(window_samples)
    horizontal_speeds: list[float] = []
    vertical_speeds: list[float] = []
    bearings: list[float] = []

    for previous, current in zip(reduced, reduced[1:]):
        dt = float(current.time - previous.time)
        if dt <= 0:
            continue
        if is_usable_gps(previous.latitude, previous.longitude) and is_usable_gps(
            current.latitude,
            current.longitude,
        ):
            distance = haversine_meters(
                float(previous.latitude),
                float(previous.longitude),
                float(current.latitude),
                float(current.longitude),
            )
            horizontal_speeds.append(distance / dt)
            if distance > 0.25:
                bearings.append(
                    bearing_degrees(
                        previous.latitude,
                        previous.longitude,
                        current.latitude,
                        current.longitude,
                    )
                )
        if previous.rel_alt is not None and current.rel_alt is not None:
            vertical_speeds.append(abs(current.rel_alt - previous.rel_alt) / dt)

    speed_jumps = [
        abs(current - previous)
        for previous, current in zip(horizontal_speeds, horizontal_speeds[1:])
    ]
    bearing_jumps = [
        angular_delta_degrees(previous, current)
        for previous, current in zip(bearings, bearings[1:])
    ]

    status = "clean"
    if horizontal_speeds and max(horizontal_speeds) > 22:
        reasons.append("high_ground_speed")
    if speed_jumps and max(speed_jumps) > 8:
        reasons.append("ground_speed_spike")
    if vertical_speeds and max(vertical_speeds) > 7:
        reasons.append("altitude_rate_spike")
    if bearing_jumps and max(bearing_jumps) > 45:
        reasons.append("direction_change_spike")

    hard_reasons = {
        "high_ground_speed",
        "ground_speed_spike",
        "altitude_rate_spike",
        "direction_change_spike",
    }
    if hard_reasons & set(reasons):
        status = "reject"
    elif reasons:
        status = "review"

    return SrtWindowScore(
        status=status,
        sample_count=len(window_samples),
        coverage=coverage,
        reasons=tuple(reasons),
    )


# Frame-based visual scoring

# Convert a visual score into JSON-friendly metrics.
def visual_metrics_dict(score: VisualMotionScore) -> dict[str, float | int | None]:
    return {
        "frame_count": score.frame_count,
        "max_shift_px": score.max_shift_px,
        "avg_shift_px": score.avg_shift_px,
        "max_frame_diff": score.max_frame_diff,
        "avg_frame_diff": score.avg_frame_diff,
        "spike_time_seconds": score.spike_time_seconds,
    }


# Check ffmpeg and NumPy before expensive visual analysis starts.
def preflight_visual_scoring(
    *,
    requested: bool,
    required_for_expansion: bool,
) -> VisualPreflightReport:
    report = VisualPreflightReport(
        requested=requested,
        required_for_expansion=required_for_expansion,
    )

    if required_for_expansion and not requested:
        report.blockers.append("require_visual_for_expansion_without_visual_score")
        return report

    if not requested:
        return report

    ffmpeg = shutil.which("ffmpeg")
    report.ffmpeg_path = ffmpeg
    if ffmpeg is None:
        report.blockers.append("ffmpeg_missing")

    try:
        import numpy  # noqa: F401
    except ImportError:
        report.numpy_available = False
        report.blockers.append("numpy_missing")
    except Exception:
        report.numpy_available = False
        report.blockers.append("numpy_unavailable")
    else:
        report.numpy_available = True

    report.available = not report.blockers
    return report


# Decode low-resolution frames and measure abrupt global motion.
def score_visual_window(
    media_path: Path | None,
    start: Fraction,
    duration: Fraction,
    *,
    fps: int = 12,
    width: int = 320,
    height: int = 180,
    reject_shift_px: float = 12.0,
    review_shift_px: float = 6.0,
    reject_frame_diff: float = 12.0,
    review_frame_diff: float = 6.0,
    timeout_seconds: float = 120.0,
    progress: Callable[[str], None] | None = None,
) -> VisualMotionScore:
    if media_path is None:
        return VisualMotionScore(status="unavailable", reasons=("missing_media_path",))
    if not media_path.is_file():
        return VisualMotionScore(status="unavailable", reasons=("media_file_missing",))

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return VisualMotionScore(status="unavailable", reasons=("ffmpeg_missing",))

    try:
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:
        return VisualMotionScore(status="unavailable", reasons=("numpy_missing",))

    command = [
        ffmpeg,
        "-v",
        "error",
        "-ss",
        f"{float(start):.6f}",
        "-t",
        f"{float(duration):.6f}",
        "-i",
        str(media_path),
        "-vf",
        f"fps={fps},scale={width}:{height},format=gray",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    started_at = time.monotonic()
    if progress:
        progress(
            f"Decoding {media_path.name} from {float(start):.2f}s "
            f"for {float(duration):.2f}s ({fps} fps, {width}x{height})."
        )

    try:
        with tempfile.TemporaryFile() as raw_output:
            process = subprocess.Popen(command, stdout=raw_output)
            next_heartbeat = started_at + 10.0
            while process.poll() is None:
                now = time.monotonic()
                elapsed = now - started_at
                if elapsed >= timeout_seconds:
                    process.kill()
                    process.wait()
                    if progress:
                        progress(
                            f"Stopped visual analysis after {elapsed:.1f}s; "
                            f"the {timeout_seconds:.0f}s timeout was reached."
                        )
                    return VisualMotionScore(
                        status="unavailable",
                        reasons=("ffmpeg_decode_timeout",),
                    )
                if progress and now >= next_heartbeat:
                    progress(f"Still analyzing {media_path.name} ({elapsed:.0f}s elapsed).")
                    next_heartbeat = now + 10.0
                time.sleep(0.25)

            if process.returncode:
                return VisualMotionScore(
                    status="unavailable",
                    reasons=("ffmpeg_decode_failed",),
                )
            raw_output.seek(0)
            raw = raw_output.read()
    except OSError:
        return VisualMotionScore(status="unavailable", reasons=("ffmpeg_decode_failed",))

    if progress:
        progress(
            f"Decoded {media_path.name} in {time.monotonic() - started_at:.1f}s; "
            "measuring camera motion."
        )

    frame_size = width * height
    if len(raw) < frame_size * 2:
        return VisualMotionScore(status="unavailable", reasons=("insufficient_frames",))

    frame_count = len(raw) // frame_size
    frames = np.frombuffer(raw[: frame_count * frame_size], dtype=np.uint8)
    frames = frames.reshape((frame_count, height, width)).astype(np.float32)
    window = np.outer(np.hanning(height), np.hanning(width)).astype(np.float32)

    def phase_shift(previous: np.ndarray, current: np.ndarray) -> tuple[float, float]:
        previous = (previous - previous.mean()) * window
        current = (current - current.mean()) * window
        previous_fft = np.fft.fft2(previous)
        current_fft = np.fft.fft2(current)
        cross_power = previous_fft * np.conj(current_fft)
        cross_power /= np.maximum(np.abs(cross_power), 1e-9)
        correlation = np.fft.ifft2(cross_power).real
        y, x = np.unravel_index(np.argmax(correlation), correlation.shape)
        if x > width // 2:
            x -= width
        if y > height // 2:
            y -= height
        return -float(x), -float(y)

    shifts: list[float] = []
    diffs: list[float] = []
    spike_time: float | None = None
    max_signal = -1.0

    for index in range(1, frame_count):
        dx, dy = phase_shift(frames[index - 1], frames[index])
        shift = math.hypot(dx, dy)
        diff = float(np.mean(np.abs(frames[index] - frames[index - 1])))
        shifts.append(shift)
        diffs.append(diff)
        signal = max(shift / reject_shift_px, diff / reject_frame_diff)
        if signal > max_signal:
            max_signal = signal
            spike_time = index / fps

    max_shift = max(shifts) if shifts else 0.0
    avg_shift = sum(shifts) / len(shifts) if shifts else 0.0
    max_diff = max(diffs) if diffs else 0.0
    avg_diff = sum(diffs) / len(diffs) if diffs else 0.0

    reasons: list[str] = []
    status = "clean"
    if max_shift >= reject_shift_px:
        reasons.append("visual_shift_spike")
    if max_diff >= reject_frame_diff:
        reasons.append("visual_frame_diff_spike")
    if reasons:
        status = "reject"
    else:
        if max_shift >= review_shift_px:
            reasons.append("visual_shift_review")
        if max_diff >= review_frame_diff:
            reasons.append("visual_frame_diff_review")
        if reasons:
            status = "review"

    return VisualMotionScore(
        status=status,
        frame_count=frame_count,
        max_shift_px=max_shift,
        avg_shift_px=avg_shift,
        max_frame_diff=max_diff,
        avg_frame_diff=avg_diff,
        spike_time_seconds=spike_time,
        reasons=tuple(reasons),
    )
