"""Local rights-review still-frame sampling. Never uploads video."""

from __future__ import annotations

from ..workflow.frames import FrameSamplerConfig

RIGHTS_SAMPLER_VERSION = "rights-uniform-v1"
RIGHTS_MAX_DIMENSION = 1024
RIGHTS_JPEG_QUALITY = 3


def rights_frame_count(duration_seconds: float) -> int:
    """Return the rights-uniform-v1 frame count for a clip duration."""
    if duration_seconds <= 10:
        return 10
    if duration_seconds <= 20:
        return 12
    return 16


def rights_frame_positions(duration_seconds: float) -> tuple[float, ...]:
    """Uniform interior positions. Avoids exactly 0% and 100%."""
    count = rights_frame_count(duration_seconds)
    return tuple((index + 1) / (count + 1) for index in range(count))


def rights_sample_timestamps(
    duration_seconds: float,
    positions: tuple[float, ...] | None = None,
) -> tuple[float, ...]:
    """Timestamps matching FrameSampler's interior clamp."""
    duration = float(duration_seconds)
    chosen = positions if positions is not None else rights_frame_positions(duration)
    return tuple(max(0.0, min(duration - 0.001, duration * position)) for position in chosen)


def rights_sampler_config(duration_seconds: float) -> FrameSamplerConfig:
    return FrameSamplerConfig(
        sampler_version=RIGHTS_SAMPLER_VERSION,
        positions=rights_frame_positions(duration_seconds),
        max_dimension=RIGHTS_MAX_DIMENSION,
        jpeg_quality=RIGHTS_JPEG_QUALITY,
    )


def rights_sampling_identity(duration_seconds: float) -> dict[str, object]:
    config = rights_sampler_config(duration_seconds)
    return {
        "sampler_version": config.sampler_version,
        "frame_count": len(config.positions),
        "positions": list(config.positions),
        "max_dimension": config.max_dimension,
        "jpeg_quality": config.jpeg_quality,
    }
