from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_content_readiness import build_content_release

from vclip_pipeline.errors import VClipError
from vclip_pipeline.publishing import ReviewService
from vclip_pipeline.publishing.paths import (
    load_json,
    review_validation_path,
    rights_evidence_path,
    rights_review_path,
    write_json,
)
from vclip_pipeline.publishing.rights_evidence import (
    FORBIDDEN_CONCLUSION_KEYS,
    OBSERVATION_FIELDS,
    evidence_risk,
)
from vclip_pipeline.publishing.rights_policy import (
    ARTWORK_NOTICE,
    IDENTIFYING_INFORMATION_NOTICE,
    PROPERTY_NOTICE,
    TRADEMARK_NOTICE,
    derive_classification,
)
from vclip_pipeline.workflow import cli as workflow_cli
from vclip_pipeline.workflow.cli import build_parser, main


def observations(
    status: str = "none_detected",
    *,
    confidence: float | None = 0.9,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in OBSERVATION_FIELDS:
        item: dict[str, Any] = {
            "status": status,
            "confidence": confidence,
            "notes": [],
        }
        if field in {
            "trademarks",
            "copyrighted_artwork",
            "identifiable_property",
        }:
            item["prominence"] = "none" if status == "none_detected" else "unknown"
            item["candidates"] = []
        result[field] = item
    return result


def set_stored_rights_payload(built, clip_index: int, payload: dict[str, Any]) -> None:
    item = built["prepared"][clip_index]
    run_id = item["candidate"]["run_id"]
    clip_id = item["candidate"]["stock_clip_id"]
    with built["catalog"].database.transaction() as connection:
        row = connection.execute(
            """
            SELECT id, result_json
            FROM clip_visual_analysis
            WHERE stockify_run_id=? AND stock_clip_id=?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (run_id, clip_id),
        ).fetchone()
        result = json.loads(row["result_json"])
        result.setdefault("raw", {})["rights_evidence"] = {"observations": payload}
        connection.execute(
            "UPDATE clip_visual_analysis SET result_json=? WHERE id=?",
            (json.dumps(result, sort_keys=True), row["id"]),
        )


def review_clip(
    *,
    facts: dict[str, str] | None = None,
    capture: str = "confirmed_by_operator",
    status: str = "confirmed",
) -> dict[str, Any]:
    clean = {
        "recognizable_people": "none",
        "trademarks": "none",
        "copyrighted_artwork": "none",
        "identifiable_property": "none",
        "identifying_information": "none",
        "professional_event_content": "none",
    }
    clean.update(facts or {})
    return {
        "facts": clean,
        "capture_provenance": {"status": capture, "notes": ""},
        "human_review": {
            "status": status,
            "reviewed_by": "reviewer",
            "reviewed_at": "2026-08-20T00:00:00+00:00",
            "notes": "",
        },
    }


def confirm_clean(service: ReviewService, built, clip: int = 1, **overrides: Any):
    values = {
        "recognizable_people": "none",
        "trademarks": "none",
        "copyrighted_artwork": "none",
        "identifiable_property": "none",
        "identifying_information": "none",
        "professional_event_content": "none",
    }
    values.update(overrides)
    return service.confirm(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        clip=clip,
        reviewed_by="sameer",
        capture_provenance="confirmed_by_operator",
        **values,
    )


def test_evidence_unknown_without_explicit_source_and_has_no_legal_fields(
    pipeline_run,
) -> None:
    built = build_content_release(pipeline_run)
    service = ReviewService(built["catalog"])
    result = service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    evidence = load_json(Path(result["rights_evidence_path"]))
    assert evidence["schema_version"] == 1
    assert evidence["clip_count"] == 1
    assert all(
        item["status"] == "unknown" for item in evidence["clips"][0]["observations"].values()
    )
    blob = json.dumps(evidence)
    assert not any(f'"{field}"' in blob for field in FORBIDDEN_CONCLUSION_KEYS)


def test_evidence_explicit_payload_is_deterministic_and_review_is_preserved(
    pipeline_run,
) -> None:
    built = build_content_release(pipeline_run)
    stored = observations()
    stored["trademarks"].update(
        {
            "status": "possible",
            "prominence": "incidental",
            "candidates": ["Example Brand"],
        }
    )
    set_stored_rights_payload(built, 0, stored)
    service = ReviewService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    first_evidence = load_json(rights_evidence_path(built["release_dir"]))
    first_review = load_json(rights_review_path(built["release_dir"]))
    first_review["clips"][0]["human_review"]["notes"] = "human note"
    write_json(rights_review_path(built["release_dir"]), first_review)

    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    second_evidence = load_json(rights_evidence_path(built["release_dir"]))
    second_review = load_json(rights_review_path(built["release_dir"]))
    for document in (first_evidence, second_evidence):
        document.pop("generated_at", None)
    assert first_evidence == second_evidence
    assert second_review["clips"][0]["human_review"]["notes"] == "human note"
    assert second_evidence["clips"][0]["stock_clip_id"] == first_review["clips"][0]["stock_clip_id"]


def test_old_pending_schema_migrates_but_human_decisions_refuse(pipeline_run) -> None:
    built = build_content_release(pipeline_run)
    service = ReviewService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    package = load_json(built["release_dir"] / "package-release.json")
    package_clip = package["clips"][0]
    old = {
        "document_version": 1,
        "clip_count": 1,
        "clips": [
            {
                "sort_order": 1,
                "stock_clip_id": package_clip["stock_clip_id"],
                "export_id": package_clip["export_id"],
                "customer_filename": package_clip["customer_filename"],
                "review_status": "pending",
                "people": "unchecked",
                "logos_trademarks": "unchecked",
                "artwork_property": "unchecked",
                "license_plates": "unchecked",
                "reviewed_by": "",
                "reviewed_at": "",
                "notes": "",
            }
        ],
    }
    path = rights_review_path(built["release_dir"])
    write_json(path, old)
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    assert load_json(path)["schema_version"] == 2

    old["clips"][0]["review_status"] = "approved"
    old["clips"][0]["reviewed_by"] = "human"
    write_json(path, old)
    with pytest.raises(VClipError, match="Automatic migration is refused"):
        service.prepare(
            slug=built["slug"],
            version=built["version"],
            release_root=built["release_root"],
        )


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda value: value, "LOW"),
        (
            lambda value: value["trademarks"].update(
                {"status": "possible", "prominence": "incidental"}
            ),
            "REVIEW",
        ),
        (
            lambda value: value["recognizable_people"].update({"status": "possible"}),
            "HIGH",
        ),
        (
            lambda value: value["identifying_information"].update({"status": "detected"}),
            "HIGH",
        ),
        (
            lambda value: value["professional_event_content"].update({"status": "possible"}),
            "HIGH",
        ),
        (
            lambda value: value["copyrighted_artwork"].update(
                {"status": "detected", "prominence": "prominent"}
            ),
            "HIGH",
        ),
        (
            lambda value: value["recognizable_people"].update({"status": "unknown"}),
            "UNKNOWN",
        ),
    ],
)
def test_machine_risk_triage(mutate, expected) -> None:
    value = observations()
    mutate(value)
    assert evidence_risk({"observations": value}) == expected


def test_list_filters_and_show_internal_master_path(pipeline_run) -> None:
    built = build_content_release(pipeline_run, clip_count=2)
    set_stored_rights_payload(built, 0, observations())
    high = observations()
    high["recognizable_people"]["status"] = "detected"
    set_stored_rights_payload(built, 1, high)
    service = ReviewService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    all_rows = service.list_clips(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    assert [row["risk"] for row in all_rows["rows"]] == ["LOW", "HIGH"]
    high_rows = service.list_clips(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        risk="HIGH",
    )
    assert [row["sort_order"] for row in high_rows["rows"]] == [2]
    assert high_rows["totals"]["HIGH"] == 1
    assert high_rows["totals"]["LOW"] == 1
    assert (
        len(
            service.list_clips(
                slug=built["slug"],
                version=built["version"],
                release_root=built["release_root"],
                pending_only=True,
            )["rows"]
        )
        == 2
    )

    shown = service.show(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        clip=1,
    )
    assert shown["caption"]
    assert shown["tags"]
    assert shown["markets"]
    assert shown["master_path"] == str(built["prepared"][0]["path"].resolve())
    public_blob = json.dumps(load_json(built["release_dir"] / "public-metadata.json"))
    assert shown["master_path"] not in public_blob


def test_accept_machine_evidence_only_accepts_none_detected(pipeline_run) -> None:
    built = build_content_release(pipeline_run)
    value = observations()
    value["trademarks"].update({"status": "possible", "prominence": "incidental"})
    set_stored_rights_payload(built, 0, value)
    service = ReviewService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    with pytest.raises(VClipError, match="trademarks"):
        service.confirm(
            slug=built["slug"],
            version=built["version"],
            release_root=built["release_root"],
            clip=1,
            reviewed_by="sameer",
            accept_machine_evidence=True,
            capture_provenance="confirmed_by_operator",
        )
    review = load_json(rights_review_path(built["release_dir"]))
    assert all(value == "unconfirmed" for value in review["clips"][0]["facts"].values())

    confirmed = service.confirm(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        clip=1,
        reviewed_by="sameer",
        accept_machine_evidence=True,
        trademarks="incidental",
        capture_provenance="confirmed_by_operator",
    )
    assert confirmed["facts"]["recognizable_people"] == "none"
    assert confirmed["facts"]["trademarks"] == "incidental"
    assert confirmed["classification"]["value"] == "standard_with_notice"


def test_confirm_requires_reviewer_and_human_capture_attestation(pipeline_run) -> None:
    built = build_content_release(pipeline_run)
    set_stored_rights_payload(built, 0, observations())
    service = ReviewService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    with pytest.raises(VClipError, match="reviewed-by"):
        service.confirm(
            slug=built["slug"],
            version=built["version"],
            release_root=built["release_root"],
            clip=1,
            reviewed_by="",
            accept_machine_evidence=True,
            capture_provenance="confirmed_by_operator",
        )
    with pytest.raises(VClipError, match="capture_provenance"):
        service.confirm(
            slug=built["slug"],
            version=built["version"],
            release_root=built["release_root"],
            clip=1,
            reviewed_by="sameer",
            accept_machine_evidence=True,
        )
    result = service.confirm(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        clip=1,
        reviewed_by="sameer",
        accept_machine_evidence=True,
        capture_provenance="confirmed_by_operator",
    )
    assert result["human_review"]["reviewed_at"]
    assert result["capture_provenance"]["status"] == "confirmed_by_operator"


def test_confirm_one_clip_does_not_change_another(pipeline_run) -> None:
    built = build_content_release(pipeline_run, clip_count=2)
    service = ReviewService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    before = load_json(rights_review_path(built["release_dir"]))["clips"][1]
    confirm_clean(service, built, clip=1)
    after = load_json(rights_review_path(built["release_dir"]))["clips"][1]
    assert before == after


@pytest.mark.parametrize(
    ("facts", "capture", "status", "classification", "notices"),
    [
        ({}, "confirmed_by_operator", "confirmed", "standard", []),
        (
            {"trademarks": "incidental"},
            "confirmed_by_operator",
            "confirmed",
            "standard_with_notice",
            [TRADEMARK_NOTICE],
        ),
        (
            {"identifiable_property": "prominent"},
            "confirmed_by_operator",
            "confirmed",
            "standard_with_notice",
            [PROPERTY_NOTICE],
        ),
        (
            {"trademarks": "prominent"},
            "confirmed_by_operator",
            "confirmed",
            "standard_with_notice",
            [TRADEMARK_NOTICE],
        ),
        (
            {"recognizable_people": "present_unreleased"},
            "confirmed_by_operator",
            "confirmed",
            "needs_clearance",
            [],
        ),
        (
            {"copyrighted_artwork": "prominent"},
            "confirmed_by_operator",
            "confirmed",
            "needs_clearance",
            [],
        ),
        (
            {"professional_event_content": "present"},
            "confirmed_by_operator",
            "confirmed",
            "blocked",
            [],
        ),
        ({}, "known_problem", "confirmed", "blocked", []),
        ({}, "needs_research", "confirmed", "needs_clearance", []),
        (
            {"copyrighted_artwork": "incidental"},
            "confirmed_by_operator",
            "confirmed",
            "standard_with_notice",
            [ARTWORK_NOTICE],
        ),
        (
            {"identifying_information": "present"},
            "confirmed_by_operator",
            "confirmed",
            "standard_with_notice",
            [IDENTIFYING_INFORMATION_NOTICE],
        ),
    ],
)
def test_policy_v1(facts, capture, status, classification, notices) -> None:
    result = derive_classification(review_clip(facts=facts, capture=capture, status=status))
    assert result["value"] == classification
    assert result["customer_notices"] == notices


def test_policy_v1_released_people_are_standard_and_blocked_review_wins() -> None:
    released = derive_classification(review_clip(facts={"recognizable_people": "present_released"}))
    assert released["value"] == "standard"
    blocked = derive_classification(review_clip(status="blocked"))
    assert blocked["value"] == "blocked"
    assert "human_review_blocked" in blocked["reasons"]
    # editorial_only is reserved and never inferred by policy v1.
    inferred = derive_classification(review_clip())
    assert inferred["value"] != "editorial_only"


@pytest.mark.parametrize(
    "classification",
    ["editorial_only", "needs_clearance", "blocked", "unclassified"],
)
def test_review_validation_rejects_nonstandard_classifications(
    pipeline_run, classification
) -> None:
    built = build_content_release(pipeline_run)
    service = ReviewService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    path = rights_review_path(built["release_dir"])
    review = load_json(path)
    clip = review["clips"][0]
    clip["human_review"].update(
        {
            "status": "confirmed",
            "reviewed_by": "reviewer",
            "reviewed_at": "2026-08-20T00:00:00+00:00",
        }
    )
    clip["facts"] = review_clip()["facts"]
    clip["capture_provenance"]["status"] = "confirmed_by_operator"
    clip["classification"] = {
        "value": classification,
        "derived_at": "2026-08-20T00:00:00+00:00",
        "policy_version": "v1",
        "reasons": [],
        "customer_notices": [],
    }
    write_json(path, review)
    result = service.validate(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    assert result["status"] == "not_review_ready"
    assert any("not eligible" in failure for failure in result["failures"])


def test_review_validation_standard_and_notice_pass_and_public_is_safe(
    pipeline_run,
) -> None:
    built = build_content_release(pipeline_run, clip_count=2)
    service = ReviewService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    confirm_clean(service, built, clip=1)
    confirm_clean(service, built, clip=2, trademarks="prominent")
    result = service.validate(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    assert result["status"] == "review_ready"
    assert load_json(review_validation_path(built["release_dir"]))["review_ready"]
    public = load_json(built["release_dir"] / "public-metadata.json")
    assert [clip["rights"]["classification"] for clip in public["clips"]] == [
        "standard",
        "standard_with_notice",
    ]
    blob = json.dumps(public)
    assert "sameer" not in blob
    assert "confidence" not in blob
    assert "master_path" not in blob


def test_review_cli_parser_and_list_invocation(monkeypatch, capsys, tmp_path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "publish",
            "review",
            "confirm",
            "--clip",
            "1",
            "--reviewed-by",
            "sameer",
            "--capture-provenance",
            "confirmed_by_operator",
            "--recognizable-people",
            "none",
            "--trademarks",
            "none",
            "--copyrighted-artwork",
            "none",
            "--identifiable-property",
            "none",
            "--identifying-information",
            "none",
            "--professional-event-content",
            "none",
            "--release-root",
            str(tmp_path),
            "slug",
        ]
    )
    assert args.handler is workflow_cli._run_publish_review_confirm

    class FakeReview:
        def __init__(self, catalog):
            pass

        def list_clips(self, **kwargs):
            return {
                "rows": [
                    {
                        "sort_order": 1,
                        "risk": "UNKNOWN",
                        "human_review_status": "pending",
                        "classification": "unclassified",
                        "caption": "A skyline.",
                    }
                ],
                "totals": {"LOW": 0, "REVIEW": 0, "HIGH": 0, "UNKNOWN": 1},
                "total": 1,
            }

    monkeypatch.setattr(workflow_cli, "_catalog", lambda db: (None, object()))
    monkeypatch.setattr(workflow_cli, "ReviewService", FakeReview)
    code = main(
        [
            "publish",
            "review",
            "list",
            "--release-root",
            str(tmp_path),
            "slug",
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "UNKNOWN" in output
    assert "A skyline." in output


def test_review_cli_prepare_show_confirm_validate(monkeypatch, capsys, tmp_path) -> None:
    calls: list[str] = []

    class FakeReview:
        def __init__(self, catalog):
            pass

        def prepare(self, **kwargs):
            calls.append("prepare")
            return {
                "collection_slug": kwargs["slug"],
                "collection_version": 1,
                "clip_count": 1,
                "rights_evidence_path": str(tmp_path / "rights-evidence.json"),
                "rights_review_path": str(tmp_path / "rights-review.json"),
            }

        def show(self, **kwargs):
            calls.append("show")
            return {
                "sort_order": kwargs["clip"],
                "customer_filename": "Clip 01.mp4",
                "master_path": "/internal/master.mp4",
                "duration_seconds": 7.3,
                "caption": "A skyline.",
                "tags": ["skyline"],
                "markets": ["San Francisco"],
                "machine_risk": "UNKNOWN",
                "machine_evidence": {"recognizable_people": {"status": "unknown", "notes": []}},
                "human_confirmed_facts": {"recognizable_people": "unconfirmed"},
                "human_review": {"status": "pending", "reviewed_by": None},
                "capture_provenance": {"status": "unconfirmed", "notes": ""},
                "classification": {"value": "unclassified", "reasons": [], "customer_notices": []},
            }

        def confirm(self, **kwargs):
            calls.append("confirm")
            return {
                "sort_order": kwargs["clip"],
                "customer_filename": "Clip 01.mp4",
                "human_review": {"status": "confirmed"},
                "classification": {"value": "standard", "reasons": ["ok"]},
            }

        def validate(self, **kwargs):
            calls.append("validate")
            return {
                "collection_slug": kwargs["slug"],
                "collection_version": 1,
                "clip_count": 1,
                "status": "review_ready",
                "path": str(tmp_path / "review-validation.json"),
                "failures": [],
            }

    monkeypatch.setattr(workflow_cli, "_catalog", lambda db: (None, object()))
    monkeypatch.setattr(workflow_cli, "ReviewService", FakeReview)
    assert (
        main(
            [
                "publish",
                "review",
                "prepare",
                "--release-root",
                str(tmp_path),
                "slug",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "publish",
                "review",
                "show",
                "--clip",
                "1",
                "--release-root",
                str(tmp_path),
                "slug",
            ]
        )
        == 0
    )
    shown = capsys.readouterr().out
    assert "MACHINE EVIDENCE" in shown
    assert "/internal/master.mp4" in shown
    assert (
        main(
            [
                "publish",
                "review",
                "confirm",
                "--clip",
                "1",
                "--reviewed-by",
                "sameer",
                "--accept-machine-evidence",
                "--capture-provenance",
                "confirmed_by_operator",
                "--release-root",
                str(tmp_path),
                "slug",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "publish",
                "review",
                "validate",
                "--release-root",
                str(tmp_path),
                "slug",
            ]
        )
        == 0
    )
    assert calls == ["prepare", "show", "confirm", "validate"]
