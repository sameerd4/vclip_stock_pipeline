"""Evidence-aware catalog search over local SQLite enrichment metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .entities import EntityCatalog, normalize_entity_alias
from .taxonomy import VisualTaxonomy, normalize_taxonomy_key

# Customer-facing plural / synonym forms -> controlled tag ids.
QUERY_TAG_ALIASES: dict[str, str] = {
    "road": "road",
    "roads": "road",
    "bridge": "bridge",
    "bridges": "bridge",
    "skyline": "skyline",
    "skylines": "skyline",
    "waterfront": "waterfront",
    "waterfronts": "waterfront",
    "building": "architecture",
    "buildings": "architecture",
    "marina": "waterfront",
    "marinas": "waterfront",
}

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "for",
        "in",
        "of",
        "on",
        "the",
        "to",
        "with",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?", re.IGNORECASE)

# Evidence weights (higher is better).
SCORE_EXACT_CANONICAL_ENTITY = 100.0
SCORE_CANONICAL_ENTITY_ALIAS = 90.0
SCORE_PRIMARY_TAG = 40.0
SCORE_SECONDARY_TAG = 25.0
SCORE_CONTEXT_TAG = 8.0
SCORE_RAW_NAMED_SUBJECT = 30.0
SCORE_CAPTION_PHRASE = 18.0
SCORE_CAPTION_TOKEN = 4.0
SCORE_MARKET_LOCATION = 12.0
SCORE_GENERIC_META = 2.0

MULTI_CONCEPT_ALL_BASE = 40.0
MULTI_CONCEPT_ALL_PER = 18.0

# Tags that are useful filters but weakly discriminative alone.
_GENERIC_TAGS = frozenset(
    {
        "city_urban",
        "establishing",
        "golden_hour",
        "clear_skies",
        "background",
    }
)


@dataclass(frozen=True)
class QueryConcept:
    kind: str  # entity | tag | text
    value: str
    matched_text: str
    match_mode: str  # exact_label | alias | id | free_text
    tokens: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedQuery:
    original: str
    concepts: tuple[QueryConcept, ...]

    @property
    def entity_concepts(self) -> tuple[QueryConcept, ...]:
        return tuple(item for item in self.concepts if item.kind == "entity")

    @property
    def tag_concepts(self) -> tuple[QueryConcept, ...]:
        return tuple(item for item in self.concepts if item.kind == "tag")

    @property
    def text_concepts(self) -> tuple[QueryConcept, ...]:
        return tuple(item for item in self.concepts if item.kind == "text")


@dataclass
class ScoreContribution:
    kind: str
    label: str
    points: float
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "kind": self.kind,
            "label": self.label,
            "points": self.points,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class ScoredClip:
    row: dict[str, Any]
    score: float
    contributions: list[ScoreContribution] = field(default_factory=list)
    matched_concept_count: int = 0
    total_concept_count: int = 0

    def explain(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "matched_concepts": self.matched_concept_count,
            "total_concepts": self.total_concept_count,
            "contributions": [item.as_dict() for item in self.contributions],
        }


class CatalogSearch:
    """Normalize queries and rank catalog rows with evidence-aware scoring."""

    def __init__(
        self,
        *,
        taxonomy: VisualTaxonomy | None = None,
        entities: EntityCatalog | None = None,
    ) -> None:
        self.taxonomy = taxonomy or VisualTaxonomy.default()
        self.entities = entities or EntityCatalog.default()

    def parse_query(self, query: str) -> ParsedQuery:
        original = (query or "").strip()
        if not original:
            return ParsedQuery(original="", concepts=())

        tokens = _tokenize(original)
        if not tokens:
            return ParsedQuery(original=original, concepts=())

        concepts: list[QueryConcept] = []
        consumed = [False] * len(tokens)
        index = 0
        while index < len(tokens):
            if consumed[index]:
                index += 1
                continue
            matched = False
            for width in range(len(tokens) - index, 0, -1):
                if any(consumed[index : index + width]):
                    continue
                phrase_tokens = tokens[index : index + width]
                phrase = " ".join(phrase_tokens)
                entity = self.entities.resolve_alias(phrase)
                if entity is not None:
                    phrase_key = normalize_entity_alias(phrase)
                    mode = (
                        "exact_label"
                        if phrase_key
                        in {
                            normalize_entity_alias(entity.label),
                            normalize_entity_alias(entity.id),
                        }
                        else "alias"
                    )
                    concepts.append(
                        QueryConcept(
                            kind="entity",
                            value=entity.id,
                            matched_text=phrase,
                            match_mode=mode,
                            tokens=tuple(phrase_tokens),
                        )
                    )
                    for offset in range(width):
                        consumed[index + offset] = True
                    index += width
                    matched = True
                    break
                tag_id = self._resolve_tag_phrase(phrase)
                if tag_id is not None:
                    key = normalize_taxonomy_key(phrase)
                    if key in QUERY_TAG_ALIASES:
                        mode = "alias"
                    elif key == tag_id:
                        mode = "id"
                    else:
                        mode = "exact_label"
                    concepts.append(
                        QueryConcept(
                            kind="tag",
                            value=tag_id,
                            matched_text=phrase,
                            match_mode=mode,
                            tokens=tuple(phrase_tokens),
                        )
                    )
                    for offset in range(width):
                        consumed[index + offset] = True
                    index += width
                    matched = True
                    break
            if not matched:
                token = tokens[index]
                if token not in _STOPWORDS:
                    concepts.append(
                        QueryConcept(
                            kind="text",
                            value=token,
                            matched_text=token,
                            match_mode="free_text",
                            tokens=(token,),
                        )
                    )
                consumed[index] = True
                index += 1

        # Deduplicate concepts while preserving order (roads + road -> one tag).
        deduped: list[QueryConcept] = []
        seen: set[tuple[str, str]] = set()
        for concept in concepts:
            key = (concept.kind, concept.value)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(concept)
        return ParsedQuery(original=original, concepts=tuple(deduped))

    def _resolve_tag_phrase(self, phrase: str) -> str | None:
        key = normalize_taxonomy_key(phrase)
        if not key:
            return None
        alias = QUERY_TAG_ALIASES.get(key)
        if alias:
            return alias
        return self.taxonomy.resolve_query_term(phrase)

    def score_row(self, row: dict[str, Any], parsed: ParsedQuery) -> ScoredClip | None:
        if not parsed.concepts:
            return None

        contributions: list[ScoreContribution] = []
        matched_flags: list[bool] = []
        entity_hit = False

        tags_by_id = {
            str(tag.get("tag") or ""): tag for tag in row.get("tags", []) if tag.get("tag")
        }
        subjects = list(row.get("named_subjects") or [])
        entity_ids = {
            str(item.get("canonical_entity_id"))
            for item in subjects
            if item.get("canonical_entity_id")
        }
        raw_names = [
            str(item.get("raw_name") or item.get("subject") or "")
            for item in subjects
        ]
        alias_keys = {
            normalize_entity_alias(alias)
            for concept in parsed.entity_concepts
            for alias in self.entities.aliases_for(concept.value)
        }
        caption = str(row.get("caption") or "")
        caption_fold = caption.casefold()
        caption_tokens = set(_tokenize(caption))

        location_blob = " ".join(
            str(value)
            for value in (
                row.get("public_label"),
                row.get("city"),
                row.get("neighborhood"),
                row.get("state"),
                row.get("country"),
                row.get("generated_project_label"),
                *(
                    item.get("market_label")
                    for item in row.get("markets", [])
                    if item.get("market_label")
                ),
                *(
                    item.get("market_id")
                    for item in row.get("markets", [])
                    if item.get("market_id")
                ),
            )
            if value
        )
        location_tokens = set(_tokenize(location_blob))

        entity_tokens = {
            token
            for concept in parsed.entity_concepts
            for token in concept.tokens
            if token not in _STOPWORDS
        }

        for concept in parsed.concepts:
            concept_matched = False
            if concept.kind == "entity":
                if concept.value in entity_ids:
                    points = (
                        SCORE_EXACT_CANONICAL_ENTITY
                        if concept.match_mode == "exact_label"
                        else SCORE_CANONICAL_ENTITY_ALIAS
                    )
                    kind = (
                        "exact_canonical_entity"
                        if concept.match_mode == "exact_label"
                        else "canonical_entity_alias"
                    )
                    label = next(
                        (
                            str(item.get("canonical_label") or concept.value)
                            for item in subjects
                            if item.get("canonical_entity_id") == concept.value
                        ),
                        concept.value,
                    )
                    contributions.append(
                        ScoreContribution(
                            kind=kind,
                            label=label,
                            points=points,
                            detail=concept.matched_text,
                        )
                    )
                    concept_matched = True
                    entity_hit = True
                else:
                    raw_hit = next(
                        (
                            raw
                            for raw in raw_names
                            if raw
                            and normalize_entity_alias(raw)
                            in alias_keys
                            | {normalize_entity_alias(concept.matched_text)}
                        ),
                        None,
                    )
                    if raw_hit:
                        contributions.append(
                            ScoreContribution(
                                kind="raw_named_subject",
                                label=raw_hit,
                                points=SCORE_RAW_NAMED_SUBJECT,
                                detail=concept.matched_text,
                            )
                        )
                        concept_matched = True
                        entity_hit = True
            elif concept.kind == "tag":
                tag_row = tags_by_id.get(concept.value)
                if tag_row is not None:
                    strength = str(tag_row.get("strength") or "context").casefold()
                    if strength == "primary":
                        points = SCORE_PRIMARY_TAG
                        kind = "exact_primary_tag"
                    elif strength == "secondary":
                        points = SCORE_SECONDARY_TAG
                        kind = "exact_secondary_tag"
                    else:
                        points = SCORE_CONTEXT_TAG
                        kind = "generic_context_tag"
                    if concept.value in _GENERIC_TAGS and strength != "primary":
                        points = min(points, SCORE_GENERIC_META)
                        kind = "generic_context_metadata"
                    contributions.append(
                        ScoreContribution(
                            kind=kind,
                            label=concept.value,
                            points=points,
                            detail=concept.matched_text,
                        )
                    )
                    concept_matched = True
                # Controlled tags are deterministic filters: do not treat a caption
                # mention of the label as a substitute for the tag being present.
            elif concept.kind == "text":
                token = concept.value
                if token in entity_tokens and not any(
                    item.value in entity_ids for item in parsed.entity_concepts
                ):
                    # Do not treat entity-name fragments as caption evidence when the
                    # clip does not carry that canonical/raw entity.
                    matched_flags.append(False)
                    continue
                hit_raw = next(
                    (
                        raw
                        for raw in raw_names
                        if raw
                        and (
                            token in _tokenize(raw)
                            or normalize_entity_alias(raw) == token
                        )
                    ),
                    None,
                )
                if hit_raw:
                    contributions.append(
                        ScoreContribution(
                            kind="raw_named_subject",
                            label=hit_raw,
                            points=SCORE_RAW_NAMED_SUBJECT * 0.6,
                            detail=token,
                        )
                    )
                    concept_matched = True
                if not concept_matched and token in caption_tokens:
                    contributions.append(
                        ScoreContribution(
                            kind="caption_token",
                            label=token,
                            points=SCORE_CAPTION_TOKEN,
                        )
                    )
                    concept_matched = True
                if not concept_matched and token in location_tokens:
                    contributions.append(
                        ScoreContribution(
                            kind="market_location",
                            label=token,
                            points=SCORE_MARKET_LOCATION,
                        )
                    )
                    concept_matched = True
                if (
                    not concept_matched
                    and len(concept.matched_text) >= 4
                    and concept.matched_text.casefold() in caption_fold
                ):
                    contributions.append(
                        ScoreContribution(
                            kind="caption_phrase",
                            label=concept.matched_text,
                            points=SCORE_CAPTION_PHRASE,
                        )
                    )
                    concept_matched = True

            matched_flags.append(concept_matched)

        # Entity-centric queries: suppress weak incidental caption-only matches.
        if parsed.entity_concepts and not parsed.tag_concepts and not entity_hit:
            independent_text_hit = any(
                flag
                for concept, flag in zip(parsed.concepts, matched_flags)
                if concept.kind == "text"
                and not set(concept.tokens) <= entity_tokens
                and flag
            )
            if not independent_text_hit:
                return None

        matched_count = sum(1 for flag in matched_flags if flag)
        if matched_count == 0:
            return None

        score = sum(item.points for item in contributions)
        total = len(parsed.concepts)
        if total > 1:
            if matched_count == total:
                bonus = MULTI_CONCEPT_ALL_BASE + MULTI_CONCEPT_ALL_PER * (total - 1)
                contributions.append(
                    ScoreContribution(
                        kind="multi_concept_all",
                        label=f"{matched_count}/{total}",
                        points=bonus,
                    )
                )
                score += bonus
            else:
                score *= 0.35 + 0.65 * (matched_count / total)
                contributions.append(
                    ScoreContribution(
                        kind="multi_concept_partial",
                        label=f"{matched_count}/{total}",
                        points=0.0,
                        detail="partial concept coverage penalty applied",
                    )
                )

        return ScoredClip(
            row=row,
            score=score,
            contributions=contributions,
            matched_concept_count=matched_count,
            total_concept_count=total,
        )

    def rank(
        self,
        rows: Iterable[dict[str, Any]],
        query: str,
        *,
        limit: int = 50,
        explain: bool = False,
    ) -> list[dict[str, Any]]:
        parsed = self.parse_query(query)
        if not parsed.concepts:
            result = [dict(row) for row in rows][:limit]
            for row in result:
                row["search_score"] = 0.0
                row["search_rank"] = 0.0
                if explain:
                    row["search_explain"] = {
                        "score": 0.0,
                        "matched_concepts": 0,
                        "total_concepts": 0,
                        "contributions": [],
                        "parsed_concepts": [],
                    }
            return result

        scored: list[ScoredClip] = []
        for row in rows:
            item = self.score_row(row, parsed)
            if item is not None and item.score > 0:
                scored.append(item)
        scored.sort(
            key=lambda item: (
                -item.score,
                -item.matched_concept_count,
                str(item.row.get("stock_clip_id") or ""),
            )
        )
        result: list[dict[str, Any]] = []
        for item in scored[:limit]:
            payload = dict(item.row)
            payload["search_score"] = item.score
            # Higher is better (replaces BM25's lower-is-better convention).
            payload["search_rank"] = item.score
            if explain:
                explanation = item.explain()
                explanation["parsed_concepts"] = [
                    {
                        "kind": concept.kind,
                        "value": concept.value,
                        "matched_text": concept.matched_text,
                        "match_mode": concept.match_mode,
                    }
                    for concept in parsed.concepts
                ]
                payload["search_explain"] = explanation
            result.append(payload)
        return result


def _tokenize(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(text or "")]

