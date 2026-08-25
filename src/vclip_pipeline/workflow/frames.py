"""Deterministic representative-frame extraction from final exported clips."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import VClipError
from ..packaging.media import probe_media
from ..util import sha256_file, stable_id, utc_now
from .models import FrameSampleSet

SAMPLER_VERSION = "uniform-six-v1"
DEFAULT_POSITIONS = (0.10, 0.25, 0.40, 0.60, 0.75, 0.90)


@dataclass(frozen=True)
class FrameSamplerConfig:
    sampler_version: str = SAMPLER_VERSION
    positions: tuple[float, ...] = DEFAULT_POSITIONS
    max_dimension: int = 1024
    jpeg_quality: int = 3
    timeout_seconds: float = 90.0


class FrameSampler:
    """Extract a tiny, reusable image set instead of generating proxy videos."""

    def __init__(self, cache_root: Path, config: FrameSamplerConfig | None = None) -> None:
        self.cache_root = cache_root.expanduser().resolve()
        self.config = config or FrameSamplerConfig()

    def sample(
        self,
        export_path: Path,
        *,
        export_sha256: str | None = None,
        overwrite: bool = False,
    ) -> FrameSampleSet:
        export_path = export_path.expanduser().resolve()
        if not export_path.is_file():
            raise VClipError(f"Export does not exist: {export_path}")
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise VClipError("ffmpeg is required for visual enrichment but was not found.")
        probe = probe_media(export_path)
        if probe.duration_seconds is None or probe.duration_seconds <= 0:
            raise VClipError(f"Could not determine export duration: {export_path}")
        checksum = export_sha256 or sha256_file(export_path)
        config_payload = {
            "sampler_version": self.config.sampler_version,
            "positions": list(self.config.positions),
            "max_dimension": self.config.max_dimension,
            "jpeg_quality": self.config.jpeg_quality,
        }
        cache_key = stable_id("FRAMES", checksum, json.dumps(config_payload, sort_keys=True))
        directory = self.cache_root / cache_key
        manifest_path = directory / "manifest.json"
        expected = tuple(
            directory / f"frame-{index:02d}.jpg"
            for index in range(1, len(self.config.positions) + 1)
        )
        if not overwrite and manifest_path.is_file() and all(path.is_file() for path in expected):
            return FrameSampleSet(
                cache_key=cache_key,
                export_path=export_path,
                export_sha256=checksum,
                duration_seconds=float(probe.duration_seconds),
                frames=expected,
                positions=self.config.positions,
                cache_directory=directory,
            )
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)
        duration = float(probe.duration_seconds)
        for index, (position, output) in enumerate(
            zip(self.config.positions, expected, strict=True),
            start=1,
        ):
            timestamp = max(0.0, min(duration - 0.001, duration * position))
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.6f}",
                "-i",
                str(export_path),
                "-frames:v",
                "1",
                "-vf",
                (
                    f"scale={self.config.max_dimension}:{self.config.max_dimension}:"
                    "force_original_aspect_ratio=decrease"
                ),
                "-q:v",
                str(self.config.jpeg_quality),
                "-y",
                str(output),
            ]
            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise VClipError(
                    f"Frame extraction timed out for {export_path.name} at sample {index}."
                ) from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "ffmpeg failed").strip()
                raise VClipError(
                    f"Could not extract frame {index} from {export_path.name}: {detail}"
                ) from exc
        manifest = {
            "manifest_version": 1,
            "created_at": utc_now(),
            "cache_key": cache_key,
            "export_path": str(export_path),
            "export_sha256": checksum,
            "duration_seconds": duration,
            "config": config_payload,
            "frames": [
                {
                    "path": str(path),
                    "position": position,
                    "timestamp_seconds": duration * position,
                }
                for path, position in zip(expected, self.config.positions, strict=True)
            ],
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return FrameSampleSet(
            cache_key=cache_key,
            export_path=export_path,
            export_sha256=checksum,
            duration_seconds=duration,
            frames=expected,
            positions=self.config.positions,
            cache_directory=directory,
        )
