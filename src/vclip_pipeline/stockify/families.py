"""Select graded project families and dedupe clips before Stockify emission.

Keep this layer narrow:
1. require strong grading coverage before emission
2. detect obvious same-session revisions from source footage
3. pick one winner per family
4. collapse duplicate source ranges inside the winner
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any, Iterable

from ..util import stable_id
from .core import parse_time
from .domain import CandidateBuild, ProjectBuild


MIN_GRADING_COVERAGE = 0.80
REVISION_MATCH_RATIO = 0.70
RANGE_IOU_THRESHOLD = 0.70


@dataclass(frozen=True)
class TimelineClip:
    media_identity: str
    start_seconds: float
    end_seconds: float
    candidate: CandidateBuild | None = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)


@dataclass(frozen=True)
class ProjectFamilyMember:
    source_project_id: str
    source_project_name: str
    family_role: str
    family_selection_reason: str | None
    grading_coverage: float
    accepted_clip_count: int
    useful_duration_seconds: float
    rejected_clip_count: int
    source_mod_date: str | None
    source_uid: str | None


@dataclass
class ProjectFamily:
    id: str
    run_id: str
    session_id: str
    selected_source_project_id: str | None
    timeline_signature: tuple[dict[str, object], ...]
    members: list[ProjectFamilyMember] = field(default_factory=list)

    @property
    def member_count(self) -> int:
        return len(self.members)

    def similarity_payload(self) -> dict[str, Any]:
        return {
            "timeline_signature": list(self.timeline_signature),
            "member_count": self.member_count,
            "selected_source_project_id": self.selected_source_project_id,
            "min_grading_coverage": MIN_GRADING_COVERAGE,
            "revision_match_ratio": REVISION_MATCH_RATIO,
            "members": [
                {
                    "source_project_id": member.source_project_id,
                    "source_project_name": member.source_project_name,
                    "family_role": member.family_role,
                    "family_selection_reason": member.family_selection_reason,
                    "grading_coverage": member.grading_coverage,
                    "accepted_clip_count": member.accepted_clip_count,
                    "useful_duration_seconds": member.useful_duration_seconds,
                    "rejected_clip_count": member.rejected_clip_count,
                    "source_mod_date": member.source_mod_date,
                    "source_uid": member.source_uid,
                }
                for member in self.members
            ],
        }


def media_identity_for_candidate(candidate: CandidateBuild) -> str | None:
    """Stable media identity for timeline comparison within a Stockify run."""
    if candidate.source_media_id:
        record = candidate.media_record
        if record is not None and record.media_path:
            return f"path:{record.media_path}"
        if record is not None and record.normalized_stem:
            return f"stem:{record.normalized_stem}"
        return f"media:{candidate.source_media_id}"
    ref = candidate.source_clip.get("ref") if candidate.source_clip is not None else None
    if ref:
        return f"ref:{ref}"
    return None


def _seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(parse_time(value))
    except ValueError:
        return None


def timeline_clips(project: ProjectBuild) -> list[TimelineClip]:
    """Ordered eligible-clip ranges used for revision detection and dedupe."""
    clips: list[TimelineClip] = []
    for candidate in sorted(project.accepted, key=lambda item: item.segment_index):
        media_identity = media_identity_for_candidate(candidate)
        record = candidate.candidate_record
        if media_identity is None or record is None:
            continue
        start = _seconds(record.original_start or "0s")
        duration = _seconds(record.original_duration)
        if start is None or duration is None or duration <= 0:
            continue
        clips.append(
            TimelineClip(
                media_identity=media_identity,
                start_seconds=start,
                end_seconds=start + duration,
                candidate=candidate,
            )
        )
    return clips


def timeline_signature(project: ProjectBuild) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "media_identity": clip.media_identity,
            "start_seconds": round(clip.start_seconds, 3),
            "end_seconds": round(clip.end_seconds, 3),
        }
        for clip in timeline_clips(project)
    )


def ranges_similar(left: TimelineClip, right: TimelineClip) -> bool:
    if left.media_identity != right.media_identity:
        return False
    overlap = min(left.end_seconds, right.end_seconds) - max(
        left.start_seconds, right.start_seconds
    )
    if overlap <= 0:
        return False
    union = max(left.end_seconds, right.end_seconds) - min(
        left.start_seconds, right.start_seconds
    )
    if union <= 0:
        return False
    return (overlap / union) >= RANGE_IOU_THRESHOLD


def ordered_match_ratio(smaller: list[TimelineClip], larger: list[TimelineClip]) -> float:
    """Share of smaller clips that appear in larger in roughly the same order."""
    if not smaller:
        return 0.0
    index = 0
    matched = 0
    for clip in smaller:
        while index < len(larger) and not ranges_similar(clip, larger[index]):
            index += 1
        if index >= len(larger):
            break
        matched += 1
        index += 1
    return matched / len(smaller)


def projects_are_revisions(left: ProjectBuild, right: ProjectBuild) -> bool:
    """Conservatively detect obvious revisions from source footage, not names."""
    if left.session_id != right.session_id or not left.session_id:
        return False
    left_clips = timeline_clips(left)
    right_clips = timeline_clips(right)
    if not left_clips or not right_clips:
        return False
    if len(left_clips) <= len(right_clips):
        smaller, larger = left_clips, right_clips
    else:
        smaller, larger = right_clips, left_clips
    return ordered_match_ratio(smaller, larger) >= REVISION_MATCH_RATIO


def grading_coverage(project: ProjectBuild) -> float:
    """Fraction of accepted clips that carry creative timeline treatment."""
    if not project.accepted:
        return 0.0
    graded = 0
    for candidate in project.accepted:
        report = candidate.segment_report
        effects = []
        if report is not None:
            effects = report.creative_video_effects
        elif candidate.candidate_record is not None:
            effects = candidate.candidate_record.creative_effects
        if effects:
            graded += 1
    return graded / len(project.accepted)


def is_emission_eligible(project: ProjectBuild) -> bool:
    return bool(project.accepted) and grading_coverage(project) >= MIN_GRADING_COVERAGE


def useful_duration_seconds(project: ProjectBuild) -> float:
    total = 0.0
    for candidate in project.accepted:
        record = candidate.candidate_record
        if record is None:
            continue
        if record.proposed_duration_seconds is not None:
            total += float(record.proposed_duration_seconds)
        elif record.original_duration_seconds is not None:
            total += float(record.original_duration_seconds)
    return total


def rejected_clip_count(project: ProjectBuild) -> int:
    return sum(
        1 for candidate in project.candidates if candidate.eligibility_status == "rejected"
    )


def source_mod_date(project: ProjectBuild) -> str | None:
    value = project.source_project.get("modDate")
    return value if value else None


def ranking_key(project: ProjectBuild) -> tuple:
    """Deterministic preference key; higher tuples win."""
    return (
        grading_coverage(project),
        useful_duration_seconds(project),
        len(project.accepted),
        -rejected_clip_count(project),
        source_mod_date(project) or "",
        project.source_project_uid or "",
        project.source_project_name,
        project.source_project_id,
    )


def selection_reason_for_loser(*, winner: ProjectBuild, loser: ProjectBuild) -> str:
    if grading_coverage(loser) < MIN_GRADING_COVERAGE:
        return "ungraded_variant"
    if grading_coverage(winner) >= MIN_GRADING_COVERAGE:
        return "superseded_graded_variant"
    return "lower_quality_duplicate"


def _member_record(
    project: ProjectBuild,
    *,
    role: str,
    reason: str | None,
) -> ProjectFamilyMember:
    return ProjectFamilyMember(
        source_project_id=project.source_project_id,
        source_project_name=project.source_project_name,
        family_role=role,
        family_selection_reason=reason,
        grading_coverage=grading_coverage(project),
        accepted_clip_count=len(project.accepted),
        useful_duration_seconds=useful_duration_seconds(project),
        rejected_clip_count=rejected_clip_count(project),
        source_mod_date=source_mod_date(project),
        source_uid=project.source_project_uid,
    )


def _cluster_session_projects(projects: list[ProjectBuild]) -> list[list[ProjectBuild]]:
    parent = {project.source_project_id: project.source_project_id for project in projects}

    def find(project_id: str) -> str:
        while parent[project_id] != project_id:
            parent[project_id] = parent[parent[project_id]]
            project_id = parent[project_id]
        return project_id

    def union(left_id: str, right_id: str) -> None:
        root_left = find(left_id)
        root_right = find(right_id)
        if root_left != root_right:
            parent[root_right] = root_left

    for index, left in enumerate(projects):
        for right in projects[index + 1 :]:
            if projects_are_revisions(left, right):
                union(left.source_project_id, right.source_project_id)

    clusters: dict[str, list[ProjectBuild]] = {}
    for project in projects:
        clusters.setdefault(find(project.source_project_id), []).append(project)
    return list(clusters.values())


def select_project_families(
    projects: list[ProjectBuild],
    *,
    run_id: str,
) -> list[ProjectFamily]:
    """Group same-session revisions and choose one graded winner when possible."""
    families: list[ProjectFamily] = []
    by_session: dict[str, list[ProjectBuild]] = {}
    for project in projects:
        coverage = grading_coverage(project)
        project.grading_coverage = coverage
        project.timeline_signature = timeline_signature(project)
        if not project.session_id or not project.accepted:
            project.family_role = "standalone"
            continue
        by_session.setdefault(project.session_id, []).append(project)

    for session_id, session_projects in sorted(by_session.items()):
        for cluster in _cluster_session_projects(session_projects):
            signature = timeline_signature(
                max(cluster, key=lambda item: len(timeline_clips(item)))
            )
            if len(cluster) == 1:
                project = cluster[0]
                project.project_family_id = None
                if is_emission_eligible(project):
                    project.family_role = "standalone"
                    project.family_selection_reason = None
                else:
                    project.family_role = "withheld"
                    project.family_selection_reason = "insufficient_grading"
                continue

            family_id = stable_id(
                "FAMILY",
                run_id,
                session_id,
                *(
                    f"{item['media_identity']}|{item['start_seconds']}|{item['end_seconds']}"
                    for item in signature
                ),
                *(sorted(item.source_project_id for item in cluster)),
            )
            eligible = [project for project in cluster if is_emission_eligible(project)]
            winner = sorted(eligible, key=ranking_key, reverse=True)[0] if eligible else None
            family = ProjectFamily(
                id=family_id,
                run_id=run_id,
                session_id=session_id,
                selected_source_project_id=(
                    winner.source_project_id if winner is not None else None
                ),
                timeline_signature=signature,
            )
            ordered = sorted(cluster, key=ranking_key, reverse=True)
            for project in ordered:
                project.project_family_id = family_id
                project.grading_coverage = grading_coverage(project)
                project.timeline_signature = timeline_signature(project)
                if winner is None:
                    project.family_role = "withheld"
                    project.family_selection_reason = "insufficient_grading"
                    family.members.append(
                        _member_record(
                            project,
                            role="withheld",
                            reason="insufficient_grading",
                        )
                    )
                    continue
                if project is winner:
                    project.family_role = "selected"
                    project.family_selection_reason = "selected"
                    family.members.append(
                        _member_record(project, role="selected", reason="selected")
                    )
                    continue
                reason = selection_reason_for_loser(winner=winner, loser=project)
                project.family_role = "superseded"
                project.family_selection_reason = reason
                family.members.append(
                    _member_record(project, role="superseded", reason=reason)
                )
            families.append(family)
    return families


def _demote_candidate(
    candidate: CandidateBuild,
    *,
    reason: str,
    detail: str,
) -> None:
    candidate.eligibility_status = "rejected"
    candidate.rejection_reason = reason
    candidate.rejection_detail = detail
    if candidate.candidate_record is not None:
        candidate.candidate_record = replace(
            candidate.candidate_record,
            eligibility_status="rejected",
            rejection_reason=reason,
            rejection_detail=detail,
        )


def withhold_project_candidates(project: ProjectBuild) -> list[str]:
    """Keep under-graded projects in SQLite but remove them from emission."""
    demoted: list[str] = []
    if project.family_role != "withheld":
        return demoted
    reason = project.family_selection_reason or "insufficient_grading"
    for candidate in list(project.accepted):
        demoted.append(candidate.stock_clip_id)
        _demote_candidate(
            candidate,
            reason="insufficient_grading",
            detail=(
                f"Project grading coverage "
                f"{grading_coverage(project):.1%} is below the "
                f"{MIN_GRADING_COVERAGE:.0%} emission threshold ({reason})."
            ),
        )
    project.accepted = []
    project.anchor = None
    return demoted


def supersede_project_candidates(project: ProjectBuild) -> list[str]:
    """Demote a superseded project's accepted clips so they are not active stock."""
    demoted: list[str] = []
    if project.family_role != "superseded":
        return demoted
    reason = project.family_selection_reason or "lower_quality_duplicate"
    for candidate in list(project.accepted):
        demoted.append(candidate.stock_clip_id)
        _demote_candidate(
            candidate,
            reason="superseded_project_family",
            detail=(
                "Superseded by preferred duplicate source project "
                f"({reason})."
            ),
        )
    project.accepted = []
    project.anchor = None
    return demoted


def _clip_rank(candidate: CandidateBuild) -> tuple:
    graded = 0
    if candidate.segment_report and candidate.segment_report.creative_video_effects:
        graded = 1
    elif candidate.candidate_record and candidate.candidate_record.creative_effects:
        graded = 1
    duration = 0.0
    record = candidate.candidate_record
    if record is not None:
        if record.proposed_duration_seconds is not None:
            duration = float(record.proposed_duration_seconds)
        elif record.original_duration_seconds is not None:
            duration = float(record.original_duration_seconds)
    return (graded, duration, -candidate.segment_index)


def dedupe_accepted_clips(project: ProjectBuild) -> list[str]:
    """Within one selected project, emit only one clip per overlapping source range."""
    demoted_ids: list[str] = []
    clips = timeline_clips(project)
    if len(clips) < 2:
        return demoted_ids

    keep: list[TimelineClip] = []
    drop: set[str] = set()
    ordered = sorted(
        clips,
        key=lambda item: _clip_rank(item.candidate) if item.candidate else (0, 0.0, 0),
        reverse=True,
    )
    for clip in ordered:
        assert clip.candidate is not None
        if clip.candidate.stock_clip_id in drop:
            continue
        overlapped = False
        for kept in keep:
            if ranges_similar(clip, kept):
                overlapped = True
                break
        if overlapped:
            drop.add(clip.candidate.stock_clip_id)
            continue
        keep.append(clip)

    if not drop:
        return demoted_ids

    remaining: list[CandidateBuild] = []
    for candidate in project.accepted:
        if candidate.stock_clip_id not in drop:
            remaining.append(candidate)
            continue
        demoted_ids.append(candidate.stock_clip_id)
        _demote_candidate(
            candidate,
            reason="duplicate_source_range",
            detail=(
                "Duplicate source-media range omitted; a better overlapping "
                "accepted clip was kept for emission."
            ),
        )
    remaining.sort(key=lambda item: item.segment_index)
    for index, candidate in enumerate(remaining, start=1):
        candidate.clip_sequence = index
        if candidate.candidate_record is not None:
            candidate.candidate_record = replace(
                candidate.candidate_record,
                clip_sequence=index,
            )
    project.accepted = remaining
    project.anchor = remaining[0] if remaining else None
    return demoted_ids


def apply_emission_gates(projects: Iterable[ProjectBuild]) -> list[str]:
    """Apply withheld/superseded/dedupe demotions; return demoted stock_clip_ids."""
    demoted: list[str] = []
    for project in projects:
        if project.family_role == "withheld":
            demoted.extend(withhold_project_candidates(project))
        elif project.family_role == "superseded":
            demoted.extend(supersede_project_candidates(project))
    for project in projects:
        if project.family_role in {"selected", "standalone"} and project.accepted:
            demoted.extend(dedupe_accepted_clips(project))
    return demoted
