"""Deterministic VClip distribution policy for human-confirmed facts.

This is a product/risk policy, not a declaration of law. In particular,
``editorial_only`` is reserved for a future explicit legal/editorial workflow
and is never inferred by policy v1.
"""

from __future__ import annotations

from typing import Any

POLICY_VERSION = "v1"

FACT_FIELDS = (
    "recognizable_people",
    "trademarks",
    "copyrighted_artwork",
    "identifiable_property",
    "identifying_information",
    "professional_event_content",
)

FACT_VALUES = {
    "recognizable_people": {
        "unconfirmed",
        "none",
        "present_released",
        "present_unreleased",
    },
    "trademarks": {"unconfirmed", "none", "incidental", "prominent"},
    "copyrighted_artwork": {"unconfirmed", "none", "incidental", "prominent"},
    "identifiable_property": {"unconfirmed", "none", "incidental", "prominent"},
    "identifying_information": {"unconfirmed", "none", "present"},
    "professional_event_content": {"unconfirmed", "none", "present"},
}

CAPTURE_PROVENANCE_VALUES = {
    "unconfirmed",
    "confirmed_by_operator",
    "needs_research",
    "known_problem",
}
HUMAN_REVIEW_VALUES = {"pending", "confirmed", "needs_research", "blocked"}
CLASSIFICATION_VALUES = {
    "unclassified",
    "standard",
    "standard_with_notice",
    "editorial_only",
    "needs_clearance",
    "blocked",
}

TRADEMARK_NOTICE = (
    "Third-party trademarks or branding may be depicted. No trademark ownership, "
    "sponsorship, endorsement, or other third-party rights are granted."
)
PROPERTY_NOTICE = (
    "Identifiable third-party property may be depicted. The license covers the "
    "rights VClip controls in the footage and does not grant separate third-party "
    "property rights."
)
ARTWORK_NOTICE = (
    "Third-party artwork or creative works may be incidentally depicted. Additional "
    "clearance may be required for some uses."
)
IDENTIFYING_INFORMATION_NOTICE = (
    "Potentially identifying information may be visible. Licensee is responsible "
    "for evaluating its intended use."
)


def derive_classification(review_clip: dict[str, Any]) -> dict[str, Any]:
    """Return the policy-v1 classification derived only from human fields."""
    facts = review_clip.get("facts") or {}
    capture = (review_clip.get("capture_provenance") or {}).get("status")
    human_status = (review_clip.get("human_review") or {}).get("status")
    reasons: list[str] = []

    if human_status == "blocked":
        reasons.append("human_review_blocked")
    if capture == "known_problem":
        reasons.append("capture_provenance_known_problem")
    if facts.get("professional_event_content") == "present":
        reasons.append("professional_event_content_present")
    if reasons:
        return _result("blocked", reasons, [])

    if human_status == "needs_research":
        reasons.append("human_review_needs_research")
    if capture == "needs_research":
        reasons.append("capture_provenance_needs_research")
    if facts.get("recognizable_people") == "present_unreleased":
        reasons.append("recognizable_people_present_unreleased")
    if facts.get("copyrighted_artwork") == "prominent":
        reasons.append("copyrighted_artwork_prominent")
    if reasons:
        return _result("needs_clearance", reasons, [])

    if (
        human_status != "confirmed"
        or capture != "confirmed_by_operator"
        or any(facts.get(field) == "unconfirmed" for field in FACT_FIELDS)
        or any(field not in facts for field in FACT_FIELDS)
    ):
        return _result("unclassified", ["human_confirmation_incomplete"], [])

    notices: list[str] = []
    if facts.get("trademarks") in {"incidental", "prominent"}:
        reasons.append(f"trademarks_{facts['trademarks']}")
        notices.append(TRADEMARK_NOTICE)
    if facts.get("identifiable_property") in {"incidental", "prominent"}:
        reasons.append(f"identifiable_property_{facts['identifiable_property']}")
        notices.append(PROPERTY_NOTICE)
    if facts.get("copyrighted_artwork") == "incidental":
        reasons.append("copyrighted_artwork_incidental")
        notices.append(ARTWORK_NOTICE)
    if facts.get("identifying_information") == "present":
        reasons.append("identifying_information_present")
        notices.append(IDENTIFYING_INFORMATION_NOTICE)

    if notices:
        return _result("standard_with_notice", reasons, notices)
    return _result("standard", ["all_policy_v1_standard_conditions_met"], [])


def validate_human_fields(review_clip: dict[str, Any]) -> list[str]:
    """Validate enum shape without deriving legal or capture conclusions."""
    failures: list[str] = []
    facts = review_clip.get("facts")
    if not isinstance(facts, dict):
        return ["facts must be an object"]
    for field, allowed in FACT_VALUES.items():
        value = facts.get(field)
        if value not in allowed:
            failures.append(f"invalid {field}: {value!r}")

    capture = review_clip.get("capture_provenance")
    if not isinstance(capture, dict):
        failures.append("capture_provenance must be an object")
    elif capture.get("status") not in CAPTURE_PROVENANCE_VALUES:
        failures.append(f"invalid capture_provenance.status: {capture.get('status')!r}")

    human = review_clip.get("human_review")
    if not isinstance(human, dict):
        failures.append("human_review must be an object")
    elif human.get("status") not in HUMAN_REVIEW_VALUES:
        failures.append(f"invalid human_review.status: {human.get('status')!r}")
    return failures


def _result(value: str, reasons: list[str], notices: list[str]) -> dict[str, Any]:
    return {
        "value": value,
        "policy_version": POLICY_VERSION,
        "reasons": reasons,
        "customer_notices": notices,
    }
