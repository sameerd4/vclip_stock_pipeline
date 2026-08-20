from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from test_content_readiness import build_content_release
from test_publish_review import observations, set_stored_rights_payload

from vclip_pipeline.errors import VClipError
from vclip_pipeline.publishing import ReviewService
from vclip_pipeline.publishing.paths import rights_evidence_path, rights_review_path
from vclip_pipeline.publishing.rights_evidence import (
    PROMPT_VERSION,
    SAMPLED_FRAMES_LIMITATION,
    SCHEMA_VERSION,
    RightsEvidenceService,
    evidence_risk,
    machine_evidence_cache_key,
    machine_evidence_identity,
)
from vclip_pipeline.publishing.rights_frames import (
    RIGHTS_JPEG_QUALITY,
    RIGHTS_MAX_DIMENSION,
    RIGHTS_SAMPLER_VERSION,
    rights_frame_count,
    rights_frame_positions,
    rights_sampler_config,
)
from vclip_pipeline.workflow.cli import build_parser
from vclip_pipeline.workflow.frames import SAMPLER_VERSION, FrameSamplerConfig
from vclip_pipeline.workflow.models import FrameSampleSet, ProviderUsage
from vclip_pipeline.workflow.providers.openai_rights import (
    RIGHTS_EVIDENCE_INSTRUCTIONS,
    OpenAIRightsEvidenceAnalyzer,
    RightsEvidenceAnalysisResult,
    assert_image_only_payload,
    require_jpeg_frames,
)


def _jpeg_frames(directory: Path, count: int) -> tuple[Path, ...]:
    directory.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(1, count + 1):
        path = directory / f"frame-{index:02d}.jpg"
        path.write_bytes(b"fake-jpeg")
        frames.append(path)
    return tuple(frames)


def _valid_model_payload(**overrides: Any) -> dict[str, Any]:
    payload = observations()
    payload["identifiable_property"].update(
        {
            "status": "possible",
            "prominence": "incidental",
            "candidates": ["baseball stadium"],
            "confidence": 0.4,
            "notes": ["structure visible; specific venue not certain"],
        }
    )
    for key, value in overrides.items():
        payload[key].update(value)
    return payload


def _sample_frames(
    tmp_path: Path,
    call_order: list[str] | None = None,
    sampled_shas: list[str] | None = None,
):
    def sample(
        master_path: Path,
        *,
        export_sha256: str,
        duration_seconds: float,
        overwrite: bool = False,
    ) -> FrameSampleSet:
        if call_order is not None:
            call_order.append("sample")
        if sampled_shas is not None:
            sampled_shas.append(export_sha256)
        assert master_path.suffix.lower() in {".mp4", ".mov"}
        assert master_path.is_file()
        positions = rights_frame_positions(duration_seconds)
        frames = _jpeg_frames(tmp_path / "sampled" / export_sha256[:12], len(positions))
        return FrameSampleSet(
            cache_key=f"FRAMES_{export_sha256[:8]}",
            export_path=master_path,
            export_sha256=export_sha256,
            duration_seconds=duration_seconds,
            frames=frames,
            positions=positions,
            cache_directory=frames[0].parent,
        )

    return sample


class RecordingAnalyzer:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or _valid_model_payload()
        self.calls = 0
        self.frames: tuple[Path, ...] | None = None
        self.context: dict[str, Any] | None = None
        self.request_payloads: list[dict[str, Any]] = []

    def analyze(self, frames: tuple[Path, ...], *, context: dict[str, Any]):
        self.calls += 1
        self.frames = frames
        self.context = context
        inner = OpenAIRightsEvidenceAnalyzer(api_key="test")
        request = inner.build_payload(frames, context=context)
        self.request_payloads.append(request)
        return RightsEvidenceAnalysisResult(
            observations=self.payload,
            usage=ProviderUsage(
                provider="openai",
                model="gpt-5-mini",
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                estimated_total_cost_usd=0.001,
            ),
        )


def _review_service(built, *, analyzer=None, sample=None, tmp_path: Path | None = None):
    evidence = RightsEvidenceService(
        built["catalog"],
        analyzer=analyzer,
        sample_frames=sample or (_sample_frames(tmp_path) if tmp_path is not None else None),
    )
    return ReviewService(built["catalog"], evidence_service=evidence)


def test_merchandising_sampler_is_unchanged() -> None:
    assert SAMPLER_VERSION == "uniform-six-v1"
    assert FrameSamplerConfig().sampler_version == "uniform-six-v1"


@pytest.mark.parametrize(
    ("duration", "count"),
    [(0.5, 10), (10, 10), (10.01, 12), (20, 12), (20.01, 16), (45, 16)],
)
def test_rights_frame_counts(duration: float, count: int) -> None:
    assert rights_frame_count(duration) == count
    positions = rights_frame_positions(duration)
    assert len(positions) == count
    assert positions == tuple((index + 1) / (count + 1) for index in range(count))
    assert 0.0 not in positions
    assert 1.0 not in positions
    assert positions == tuple(sorted(positions))


def test_rights_sampler_reuses_jpeg_sizing() -> None:
    config = rights_sampler_config(7.3)
    assert config.sampler_version == RIGHTS_SAMPLER_VERSION == "rights-uniform-v1"
    assert config.max_dimension == RIGHTS_MAX_DIMENSION == 1024
    assert config.jpeg_quality == RIGHTS_JPEG_QUALITY == 3
    merch = FrameSamplerConfig()
    assert config.max_dimension == merch.max_dimension
    assert config.jpeg_quality == merch.jpeg_quality


def _prompt_text() -> str:
    return " ".join(RIGHTS_EVIDENCE_INSTRUCTIONS.split())


def test_prompt_prohibits_legal_conclusions_and_explains_sampling(tmp_path: Path) -> None:
    assert PROMPT_VERSION == "rights-evidence-v1.1"
    assert PROMPT_VERSION != "rights-evidence-v1"
    assert "You are NOT making legal determinations." in RIGHTS_EVIDENCE_INSTRUCTIONS
    assert "whether the footage is legally safe" in RIGHTS_EVIDENCE_INSTRUCTIONS
    assert "whether the footage should be classified as commercial or editorial" in (
        RIGHTS_EVIDENCE_INSTRUCTIONS
    )
    assert SAMPLED_FRAMES_LIMITATION in RIGHTS_EVIDENCE_INSTRUCTIONS
    analyzer = OpenAIRightsEvidenceAnalyzer(api_key="test")
    frames = _jpeg_frames(tmp_path, 2)
    prompt = analyzer._prompt(frames, {"duration_seconds": 8, "caption": "Waterfront."})
    assert "You are NOT making legal determinations." in prompt
    assert SAMPLED_FRAMES_LIMITATION in prompt
    assert "master_path" not in prompt


def test_prompt_trademark_prominence_is_about_the_mark_not_the_object() -> None:
    text = _prompt_text()
    assert "visibility and visual significance of the trademark or branding itself" in text
    assert "not the prominence of the object or property containing it" in text
    assert (
        "Do not promote trademark prominence merely because the associated property is prominent."
        in text
    )
    assert "status = possible" in text
    assert "prominence = incidental" in text
    assert (
        "Property prominence describes the prominence of the identifiable property itself." in text
    )
    assert "These are independent dimensions." in text


def test_prompt_artwork_requires_distinct_creative_work() -> None:
    text = _prompt_text()
    assert "Do not mark copyrighted_artwork as possible merely because decorative colors" in text
    assert "generic banners/advertisements" in text
    assert "architecture" in text
    assert "A trademark/advertisement is primarily a trademarks observation" in text
    assert "affirmative visual reason to suspect a distinct creative work" in text
    assert "mural" in text
    assert "sculpture" in text


def test_prompt_version_invalidates_v1_cache_identity() -> None:
    current = machine_evidence_identity(
        export_sha256="abc",
        provider="openai",
        model="gpt-5-mini",
        duration_seconds=7.3,
    )
    assert current["prompt_version"] == "rights-evidence-v1.1"
    v1 = {**current, "prompt_version": "rights-evidence-v1"}
    assert machine_evidence_cache_key(current) != machine_evidence_cache_key(v1)


def test_unreadable_branding_on_prominent_property_triages_review() -> None:
    value = observations()
    value["identifiable_property"].update(
        {
            "status": "detected",
            "prominence": "prominent",
            "candidates": ["baseball stadium"],
        }
    )
    value["trademarks"].update(
        {
            "status": "possible",
            "prominence": "incidental",
            "candidates": ["stadium exterior signage"],
            "notes": ["Text and logos on signage are not legible in the sampled frames."],
        }
    )
    assert evidence_risk({"observations": value}) == "REVIEW"


def test_readable_dominant_brand_triages_high() -> None:
    value = observations()
    value["trademarks"].update(
        {
            "status": "detected",
            "prominence": "prominent",
            "candidates": ["ORACLE PARK"],
        }
    )
    assert evidence_risk({"observations": value}) == "HIGH"


def test_tiny_storefront_branding_is_incidental() -> None:
    value = observations()
    value["trademarks"].update(
        {
            "status": "possible",
            "prominence": "incidental",
            "candidates": ["storefront signs"],
        }
    )
    assert value["trademarks"]["prominence"] == "incidental"
    assert evidence_risk({"observations": value}) == "REVIEW"


def test_distinct_mural_or_sculpture_is_valid_artwork_observation() -> None:
    mural = observations()
    mural["copyrighted_artwork"].update(
        {
            "status": "detected",
            "prominence": "incidental",
            "candidates": ["waterfront mural"],
        }
    )
    sculpture = observations()
    sculpture["copyrighted_artwork"].update(
        {
            "status": "possible",
            "prominence": "incidental",
            "candidates": ["public sculpture"],
        }
    )
    assert evidence_risk({"observations": mural}) == "REVIEW"
    assert evidence_risk({"observations": sculpture}) == "REVIEW"


def test_prominent_identifiable_property_alone_is_review_not_high() -> None:
    value = observations()
    value["identifiable_property"].update(
        {
            "status": "detected",
            "prominence": "prominent",
            "candidates": ["baseball stadium"],
        }
    )
    assert evidence_risk({"observations": value}) == "REVIEW"


def test_require_jpeg_frames_rejects_video_and_non_images(tmp_path: Path) -> None:
    jpeg = tmp_path / "frame.jpg"
    jpeg.write_bytes(b"fake-jpeg")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"ftypisom")
    png = tmp_path / "frame.png"
    png.write_bytes(b"png")
    assert require_jpeg_frames((jpeg,)) == (jpeg,)
    with pytest.raises(VClipError, match="rejected video"):
        require_jpeg_frames((video,))
    with pytest.raises(VClipError, match="non-image"):
        require_jpeg_frames((png,))
    with pytest.raises(VClipError, match="at least one JPEG"):
        require_jpeg_frames(())


def test_provider_payload_is_jpeg_only_and_never_includes_master(tmp_path: Path) -> None:
    master = tmp_path / "master.mp4"
    master.write_bytes(b"not-a-real-video")
    frames = _jpeg_frames(tmp_path / "frames", 3)
    analyzer = OpenAIRightsEvidenceAnalyzer(api_key="test")
    payload = analyzer.build_payload(
        frames,
        context={
            "duration_seconds": 7.3,
            "caption": "Waterfront aerial.",
            "market_label": "San Francisco",
            "master_path": str(master),
        },
    )
    assert_image_only_payload(payload)
    blob = json.dumps(payload)
    assert str(master) not in blob
    assert "master.mp4" not in blob
    assert ".mp4" not in blob
    assert "input_image" in blob
    assert blob.count("data:image/jpeg;base64,") == 3
    types = [part["type"] for part in payload["input"][0]["content"]]
    assert types[0] == "input_text"
    assert types[1:] == ["input_image"] * 3
    with pytest.raises(VClipError, match="rejected video"):
        analyzer.build_payload((master,), context={"duration_seconds": 7.3})


def test_analyze_parses_structured_response_and_rejects_malformed(tmp_path: Path) -> None:
    analyzer = OpenAIRightsEvidenceAnalyzer(api_key="test", model="gpt-5-mini")
    frames = _jpeg_frames(tmp_path, 2)

    def fake_request(payload: dict[str, Any]) -> dict[str, Any]:
        assert_image_only_payload(payload)
        return {"output_text": json.dumps(_valid_model_payload())}

    analyzer._request = fake_request  # type: ignore[method-assign]
    result = analyzer.analyze(frames, context={"duration_seconds": 8})
    assert result.observations["identifiable_property"]["status"] == "possible"
    assert result.observations["identifiable_property"]["candidates"] == ["baseball stadium"]
    blob = json.dumps(result.observations)
    assert "legally safe" not in blob
    assert "commercially_cleared" not in blob

    analyzer._request = lambda payload: {"output_text": "not-json"}  # type: ignore[method-assign]
    with pytest.raises(VClipError, match="invalid JSON"):
        analyzer.analyze(frames, context={"duration_seconds": 8})

    analyzer._request = lambda payload: {"output_text": json.dumps({"cleared": True})}  # type: ignore[method-assign]
    with pytest.raises(VClipError, match="legal-conclusion"):
        analyzer.analyze(frames, context={"duration_seconds": 8})

    analyzer._request = lambda payload: {"output_text": "{}"}  # type: ignore[method-assign]
    with pytest.raises(VClipError, match="no observation fields"):
        analyzer.analyze(frames, context={"duration_seconds": 8})


def test_cache_identity_changes_with_inputs() -> None:
    base = machine_evidence_identity(
        export_sha256="abc",
        provider="openai",
        model="gpt-5-mini",
        duration_seconds=7.3,
    )
    key = machine_evidence_cache_key(base)
    assert key != machine_evidence_cache_key({**base, "export_sha256": "def"})
    assert key != machine_evidence_cache_key({**base, "model": "other-model"})
    assert key != machine_evidence_cache_key({**base, "prompt_version": "other"})
    assert key != machine_evidence_cache_key({**base, "schema_version": 2})
    sampler = dict(base["sampler"])
    sampler["sampler_version"] = "other-sampler"
    assert key != machine_evidence_cache_key({**base, "sampler": sampler})
    longer = machine_evidence_identity(
        export_sha256="abc",
        provider="openai",
        model="gpt-5-mini",
        duration_seconds=21.0,
    )
    assert key != machine_evidence_cache_key(longer)


def test_existing_provider_makes_zero_network_calls(
    pipeline_run, monkeypatch, tmp_path: Path
) -> None:
    built = build_content_release(pipeline_run)
    set_stored_rights_payload(built, 0, observations())

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("existing provider must not make network calls")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setattr(
        "vclip_pipeline.workflow.providers.openai.OpenAIResponsesClient.post",
        boom,
    )
    analyzer = RecordingAnalyzer()
    service = _review_service(built, analyzer=analyzer, tmp_path=tmp_path)
    result = service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="existing",
        cache=tmp_path / "cache",
    )
    assert result["provider"] == "existing"
    assert result["network_calls"] is False
    assert result["openai_requests"] == 0
    assert analyzer.calls == 0
    evidence = json.loads(Path(result["rights_evidence_path"]).read_text())
    assert evidence["clips"][0]["observations"]["recognizable_people"]["status"] == "none_detected"


def test_openai_runs_only_when_requested_and_extracts_jpegs_first(
    pipeline_run, tmp_path: Path
) -> None:
    built = build_content_release(pipeline_run)
    call_order: list[str] = []
    analyzer = RecordingAnalyzer()
    original_analyze = analyzer.analyze

    def analyze(frames: tuple[Path, ...], *, context: dict[str, Any]):
        call_order.append("analyze")
        return original_analyze(frames, context=context)

    analyzer.analyze = analyze  # type: ignore[method-assign]
    sample = _sample_frames(tmp_path, call_order)
    service = _review_service(built, analyzer=analyzer, sample=sample)
    with pytest.raises(VClipError, match="--cache is required"):
        service.prepare(
            slug=built["slug"],
            version=built["version"],
            release_root=built["release_root"],
            provider="openai",
        )
    result = service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="openai",
        cache=tmp_path / "cache",
    )
    assert call_order == ["sample", "analyze"]
    assert result["openai_requests"] == 1
    assert result["network_calls"] is True
    assert analyzer.frames is not None
    assert all(path.suffix == ".jpg" for path in analyzer.frames)
    assert analyzer.context is not None
    assert "master_path" not in analyzer.context
    assert analyzer.context["sampled_frames_limitation"] == SAMPLED_FRAMES_LIMITATION
    payload = analyzer.request_payloads[0]
    assert_image_only_payload(payload)
    evidence = json.loads(Path(result["rights_evidence_path"]).read_text())
    clip = evidence["clips"][0]
    assert clip["source_analysis"]["provider"] == "openai"
    assert clip["source_analysis"]["prompt_version"] == PROMPT_VERSION
    assert clip["source_analysis"]["sampler_version"] == RIGHTS_SAMPLER_VERSION
    assert clip["source_analysis"]["schema_version"] == SCHEMA_VERSION
    assert clip["source_analysis"]["sampled_frame_count"] == 10
    assert clip["source_analysis"]["video_inputs"] is False
    assert evidence["source"]["usage"]["estimated_total_cost_usd"] == pytest.approx(0.001)


def test_openai_cache_reuse_and_invalidation(pipeline_run, tmp_path: Path) -> None:
    built = build_content_release(pipeline_run)
    analyzer = RecordingAnalyzer()
    service = _review_service(built, analyzer=analyzer, tmp_path=tmp_path)
    kwargs: dict[str, Any] = {
        "slug": built["slug"],
        "version": built["version"],
        "release_root": built["release_root"],
        "provider": "openai",
        "cache": tmp_path / "cache",
        "model": "gpt-5-mini",
    }
    first = service.prepare(**kwargs)
    second = service.prepare(**kwargs)
    assert first["openai_requests"] == 1
    assert second["openai_requests"] == 0
    assert second["cached_clips"] == 1
    assert analyzer.calls == 1
    third = service.prepare(**{**kwargs, "model": "other-model"})
    assert third["openai_requests"] == 1
    assert analyzer.calls == 2


def test_existing_reuses_openai_cache_without_network(
    pipeline_run, monkeypatch, tmp_path: Path
) -> None:
    built = build_content_release(pipeline_run)
    analyzer = RecordingAnalyzer()
    service = _review_service(built, analyzer=analyzer, tmp_path=tmp_path)
    cache = tmp_path / "cache"
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="openai",
        cache=cache,
    )
    analyzer.calls = 0

    def boom(*args: object, **kwargs: object) -> None:
        raise URLError("network should not be used")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    result = service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="existing",
        cache=cache,
    )
    assert analyzer.calls == 0
    assert result["openai_requests"] == 0
    assert result["network_calls"] is False
    evidence = json.loads(Path(result["rights_evidence_path"]).read_text())
    assert evidence["clips"][0]["observations"]["identifiable_property"]["status"] == "possible"


def test_machine_evidence_refresh_leaves_human_review_bytes_unchanged(
    pipeline_run, tmp_path: Path
) -> None:
    built = build_content_release(pipeline_run)
    analyzer = RecordingAnalyzer()
    service = _review_service(built, analyzer=analyzer, tmp_path=tmp_path)
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="existing",
    )
    review_path = rights_review_path(built["release_dir"])
    review = json.loads(review_path.read_text())
    review["clips"][0]["human_review"]["notes"] = "human-only note"
    review_path.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n")
    before = review_path.read_bytes()
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="openai",
        cache=tmp_path / "cache",
        refresh=True,
    )
    assert review_path.read_bytes() == before
    evidence = json.loads(rights_evidence_path(built["release_dir"]).read_text())
    assert evidence["clips"][0]["observations"]["identifiable_property"]["status"] == "possible"


def test_accept_machine_evidence_still_refuses_possible(pipeline_run, tmp_path: Path) -> None:
    built = build_content_release(pipeline_run)
    analyzer = RecordingAnalyzer()
    service = _review_service(built, analyzer=analyzer, tmp_path=tmp_path)
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="openai",
        cache=tmp_path / "cache",
    )
    with pytest.raises(VClipError, match="identifiable_property"):
        service.confirm(
            slug=built["slug"],
            version=built["version"],
            release_root=built["release_root"],
            clip=1,
            reviewed_by="sameer",
            accept_machine_evidence=True,
            capture_provenance="confirmed_by_operator",
        )


def test_uncertain_observations_stay_possible() -> None:
    analyzer = OpenAIRightsEvidenceAnalyzer(api_key="test")
    normalized = analyzer._normalize(_valid_model_payload())
    assert normalized["identifiable_property"]["status"] == "possible"
    assert normalized["identifiable_property"]["candidates"] == ["baseball stadium"]
    assert normalized["identifiable_property"]["confidence"] == pytest.approx(0.4)


def test_review_prepare_cli_provider_flags(tmp_path: Path) -> None:
    parser = build_parser()
    default = parser.parse_args(
        ["publish", "review", "prepare", "--release-root", str(tmp_path), "slug"]
    )
    assert default.provider == "existing"
    assert default.refresh is False
    openai = parser.parse_args(
        [
            "publish",
            "review",
            "prepare",
            "--provider",
            "openai",
            "--model",
            "gpt-5-mini",
            "--cache",
            str(tmp_path / "cache"),
            "--refresh",
            "--release-root",
            str(tmp_path),
            "slug",
        ]
    )
    assert openai.provider == "openai"
    assert openai.model == "gpt-5-mini"
    assert openai.cache == tmp_path / "cache"
    assert openai.refresh is True
    assert default.clip is None
    scoped = parser.parse_args(
        [
            "publish",
            "review",
            "prepare",
            "--clip",
            "1",
            "--provider",
            "openai",
            "--cache",
            str(tmp_path / "cache"),
            "--release-root",
            str(tmp_path),
            "slug",
        ]
    )
    assert scoped.clip == 1


def test_clip_scope_analyzes_only_selected_clip_and_keeps_others_unknown(
    pipeline_run, tmp_path: Path
) -> None:
    built = build_content_release(pipeline_run, clip_count=2)
    call_order: list[str] = []
    sampled_shas: list[str] = []
    analyzer = RecordingAnalyzer()
    original_analyze = analyzer.analyze

    def analyze(frames: tuple[Path, ...], *, context: dict[str, Any]):
        call_order.append("analyze")
        return original_analyze(frames, context=context)

    analyzer.analyze = analyze  # type: ignore[method-assign]
    service = _review_service(
        built,
        analyzer=analyzer,
        sample=_sample_frames(tmp_path, call_order, sampled_shas),
    )
    result = service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="openai",
        cache=tmp_path / "cache",
        clip=1,
    )
    assert call_order == ["sample", "analyze"]
    assert analyzer.calls == 1
    assert result["openai_requests"] == 1
    assert sampled_shas == [built["prepared"][0]["digest"]]
    evidence = json.loads(rights_evidence_path(built["release_dir"]).read_text())
    assert evidence["clip_count"] == 2
    assert len(evidence["clips"]) == 2
    assert evidence["clips"][0]["observations"]["identifiable_property"]["status"] == "possible"
    assert all(
        item["status"] == "unknown" for item in evidence["clips"][1]["observations"].values()
    )


def test_clip_scope_preserves_other_evidence_and_human_review_bytes(
    pipeline_run, tmp_path: Path
) -> None:
    built = build_content_release(pipeline_run, clip_count=2)
    analyzer = RecordingAnalyzer()
    service = _review_service(built, analyzer=analyzer, tmp_path=tmp_path)
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="openai",
        cache=tmp_path / "cache",
    )
    evidence_path = rights_evidence_path(built["release_dir"])
    review_path = rights_review_path(built["release_dir"])
    before_second = json.loads(evidence_path.read_text())["clips"][1]
    before_review = review_path.read_bytes()
    analyzer.payload = observations()
    analyzer.payload["recognizable_people"]["status"] = "detected"
    result = service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="openai",
        cache=tmp_path / "cache",
        clip=1,
        refresh=True,
    )
    assert analyzer.calls == 3
    assert result["openai_requests"] == 1
    evidence = json.loads(evidence_path.read_text())
    assert evidence["clips"][0]["observations"]["recognizable_people"]["status"] == "detected"
    assert evidence["clips"][1]["observations"] == before_second["observations"]
    assert evidence["clips"][1]["source_analysis"] == before_second["source_analysis"]
    assert review_path.read_bytes() == before_review


def test_clip_scope_without_flag_still_prepares_full_collection(
    pipeline_run, tmp_path: Path
) -> None:
    built = build_content_release(pipeline_run, clip_count=2)
    sampled_shas: list[str] = []
    analyzer = RecordingAnalyzer()
    service = _review_service(
        built,
        analyzer=analyzer,
        sample=_sample_frames(tmp_path, sampled_shas=sampled_shas),
    )
    result = service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        provider="openai",
        cache=tmp_path / "cache",
    )
    assert analyzer.calls == 2
    assert result["openai_requests"] == 2
    assert sampled_shas == [item["digest"] for item in built["prepared"]]
    evidence = json.loads(rights_evidence_path(built["release_dir"]).read_text())
    assert [
        clip["observations"]["identifiable_property"]["status"] for clip in evidence["clips"]
    ] == ["possible", "possible"]


def test_clip_scope_rejects_missing_sort_order(pipeline_run, tmp_path: Path) -> None:
    built = build_content_release(pipeline_run, clip_count=2)
    service = _review_service(built, analyzer=RecordingAnalyzer(), tmp_path=tmp_path)
    with pytest.raises(VClipError, match="sort_order 99"):
        service.prepare(
            slug=built["slug"],
            version=built["version"],
            release_root=built["release_root"],
            provider="existing",
            clip=99,
        )
