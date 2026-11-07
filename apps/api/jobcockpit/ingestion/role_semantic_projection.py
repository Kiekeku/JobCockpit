from __future__ import annotations

import re
from dataclasses import dataclass

from jobcockpit.ingestion.parser_models import (
    CapabilityMention,
    RequirementExpression,
    RoleClaim,
    RoleEvidence,
)
from jobcockpit.ingestion.capability_ontology import (
    technology_entity_type,
    technology_source_match,
)
from jobcockpit.ingestion.role_parser import (
    _extract_role_capabilities,
    _extract_tech_stack,
    _role_capability_source_match,
)
from jobcockpit.ingestion.role_semantics import (
    TextFragment,
    _clause_signals,
    _expression_delimiter_depths,
    _is_high_confidence_unsectioned_evidence,
    _logical_and_matches,
    _logical_or_matches,
    _unsectioned_clause_has_non_candidate_subject,
    derive_role_semantic_lists,
    lexical_tokens,
    parse_role_document,
)


_NON_CAPABILITY_ENTITY_HEADS = {
    "abilities",
    "ability",
    "applicant",
    "applicants",
    "background",
    "candidate",
    "candidates",
    "degree",
    "education",
    "eligibility",
    "experience",
    "experiences",
    "familiarity",
    "knowledge",
    "preferred",
    "proficiency",
    "qualification",
    "qualifications",
    "required",
    "requirement",
    "requirements",
    "skill",
    "skills",
    "understanding",
    "year",
    "years",
}
_CAPABILITY_GOVERNOR_PREFIXES = (
    ("background", "in"),
    ("build",),
    ("code", "in"),
    ("competence", "in"),
    ("develop",),
    ("design",),
    ("experience", "in"),
    ("experience", "with"),
    ("expertise", "in"),
    ("exposure", "to"),
    ("familiarity", "with"),
    ("implement",),
    ("knowledge", "of"),
    ("know",),
    ("knows",),
    ("maintain",),
    ("mastery", "of"),
    ("operate",),
    ("proficiency", "in"),
    ("proficiency", "with"),
    ("program", "in"),
    ("such", "as"),
    ("understanding", "of"),
    ("use",),
    ("uses",),
    ("using",),
    ("work", "with"),
)
_CAPABILITY_MODAL_SUFFIXES = (
    ("are", "preferred"),
    ("are", "required"),
    ("is", "preferred"),
    ("is", "required"),
    ("preferred",),
    ("required",),
)
_CAPABILITY_QUALIFICATION_HEADS = {
    "background",
    "experience",
    "expertise",
    "familiarity",
    "knowledge",
    "proficiency",
    "skill",
    "skills",
    "understanding",
}
_CAPABILITY_COORDINATORS = {"and", "or"}


@dataclass(frozen=True)
class RoleSemanticProjection:
    evidence: list[RoleEvidence]
    responsibilities: list[str]
    must_have_requirements: list[str]
    nice_to_have_requirements: list[str]
    required_capabilities: list[str]
    preferred_capabilities: list[str]
    responsibility_capabilities: list[str]
    environment_capabilities: list[str]
    capabilities: list[str]
    tech_stack: list[str]
    candidate_text: str
    ambiguity_fields: list[str]


@dataclass(frozen=True)
class _ProviderEntityCandidate:
    value: str
    tokens: tuple[str, ...]
    canonical: list[str]
    start: int
    end: int


@dataclass(frozen=True)
class _EntityGroup:
    members: tuple[_ProviderEntityCandidate, ...]

    @property
    def start(self) -> int:
        return self.members[0].start

    @property
    def end(self) -> int:
        return self.members[-1].end


def project_role_semantics(posting_text: str) -> RoleSemanticProjection | None:
    document = parse_role_document(posting_text)
    if not document.evidence:
        return None
    return project_role_evidence(
        document.evidence,
        unsectioned_text=document.unsectioned_text,
    )


def project_validated_role_evidence(
    posting_text: str,
    evidence: list[RoleEvidence],
) -> RoleSemanticProjection | None:
    if not evidence:
        return None
    document = parse_role_document(posting_text)
    if not document.evidence and not document.source_fragments:
        return None

    enriched = [item.model_copy(deep=True) for item in document.evidence]
    unmatched = set(range(len(enriched)))
    consumed_provider_spans: set[tuple[int, int]] = set()
    for provider_item in evidence:
        provider_text = _normalized_evidence_text(provider_item.evidence_text)
        if not provider_text or _normalized_evidence_text(provider_item.text) != provider_text:
            return None
        matched_index = _provider_grounded_match_index(
            provider_item,
            enriched,
            unmatched,
            provider_text,
        )
        if matched_index is None:
            provider_only = _provider_only_grounded_evidence(
                posting_text,
                provider_item,
                document.source_fragments,
            )
            if provider_only is None:
                return None
            provider_start = provider_only.evidence_start
            provider_end = provider_only.evidence_end
            if provider_start is None or provider_end is None:
                return None
            provider_span = (provider_start, provider_end)
            if _span_overlaps_evidence(provider_span, enriched) or any(
                _spans_overlap(provider_span, consumed_span)
                for consumed_span in consumed_provider_spans
            ):
                return None
            consumed_provider_spans.add(provider_span)
            enriched.append(provider_only)
            continue
        deterministic_item = enriched[matched_index]
        provider_start = deterministic_item.evidence_start
        provider_end = deterministic_item.evidence_end
        if provider_start is None or provider_end is None:
            return None
        provider_span = (provider_start, provider_end)
        if any(
            _spans_overlap(provider_span, consumed_span)
            for consumed_span in consumed_provider_spans
        ):
            return None
        consumed_provider_spans.add(provider_span)
        if _provider_semantics_agree(provider_item, deterministic_item):
            deterministic_item.confidence = provider_item.confidence
            deterministic_item.entities = list(provider_item.entities)
            _merge_provider_claim_entities(deterministic_item, provider_item)
        else:
            alternatives = _grounded_provider_claims(provider_item, deterministic_item)
            if not alternatives:
                return None
            deterministic_item.alternative_claims = alternatives
            if "provider_semantic_disagreement" not in deterministic_item.quality_flags:
                deterministic_item.quality_flags.append("provider_semantic_disagreement")
        unmatched.remove(matched_index)

    return project_role_evidence(
        enriched,
        unsectioned_text=document.unsectioned_text,
    )


def _span_overlaps_evidence(
    span: tuple[int, int],
    evidence: list[RoleEvidence],
) -> bool:
    return any(
        item.evidence_start is not None
        and item.evidence_end is not None
        and _spans_overlap(span, (item.evidence_start, item.evidence_end))
        for item in evidence
    )


def _spans_overlap(
    left: tuple[int, int],
    right: tuple[int, int],
) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _provider_only_grounded_evidence(
    posting_text: str,
    provider_item: RoleEvidence,
    source_fragments: tuple[tuple[str, TextFragment], ...],
) -> RoleEvidence | None:
    evidence_text = provider_item.evidence_text.strip()
    if not evidence_text or provider_item.text.strip() != evidence_text:
        return None
    span = _provider_only_span(posting_text, provider_item, evidence_text)
    if span is None:
        return None
    start, end = span
    source_section = next(
        (
            section
            for section, fragment in source_fragments
            if fragment.start <= start and end <= fragment.end
        ),
        None,
    )
    if source_section is None or source_section != provider_item.source_section:
        return None
    if source_section not in {
        "unsectioned",
        "responsibilities",
        "qualifications",
        "preferred_qualifications",
        "role_overview",
        "capabilities",
    }:
        return None
    signals = _clause_signals(evidence_text)
    high_confidence_unsectioned = bool(
        source_section == "unsectioned"
        and _is_high_confidence_unsectioned_evidence(evidence_text, signals)
    )
    if (
        source_section == "unsectioned"
        and signals.has_modality
        and not high_confidence_unsectioned
    ):
        return None
    if (
        source_section == "unsectioned"
        and _unsectioned_clause_has_non_candidate_subject(evidence_text, signals)
    ):
        return None
    supported_entities = _provider_capabilities_for_evidence(
        provider_item.entities,
        evidence_text,
        claims=provider_item.claims,
        source_section=provider_item.source_section,
        require_explicit_scope=(
            source_section == "unsectioned" and not high_confidence_unsectioned
        ),
    )
    if len(supported_entities) != len(set(provider_item.entities)):
        return None
    if (
        source_section == "unsectioned"
        and not high_confidence_unsectioned
        and not supported_entities
    ):
        return None
    grounded_item = provider_item.model_copy(deep=True)
    grounded_item.text = posting_text[start:end]
    grounded_item.evidence_text = posting_text[start:end]
    grounded_item.evidence_start = start
    grounded_item.evidence_end = end
    grounded_item.entities = supported_entities
    grounded_claims = _grounded_provider_claims(provider_item, grounded_item)
    if len(grounded_claims) != len(provider_item.claims or [None]):
        return None
    grounded_item.claims = grounded_claims
    grounded_item.alternative_claims = []
    if "provider_only_grounded_evidence" not in grounded_item.quality_flags:
        grounded_item.quality_flags.append("provider_only_grounded_evidence")
    for index, claim in enumerate(grounded_item.claims):
        claim.claim_id = f"role-claim-{claim.source_start}-{index}"
        claim.coordination_group = claim.claim_id
        claim.normalized_text = None
        claim.decision_trace = [
            f"source_section:{grounded_item.source_section}",
            f"kind:{claim.kind}",
            f"modality:{claim.modality}",
            f"polarity:{claim.polarity}",
            f"coordination:{claim.coordination}",
        ]
        if claim.condition_type is not None:
            claim.decision_trace.append(f"condition:{claim.condition_type}")
        claim.decision_trace.append("source:provider_only_grounded")
        if "provider_only_grounded_evidence" not in claim.quality_flags:
            claim.quality_flags.append("provider_only_grounded_evidence")
    if provider_item.requirement_expression is None:
        grounded_item.requirement_expression = None
    else:
        grounded_expression = _grounded_provider_requirement_expression(
            provider_item.requirement_expression,
            grounded_item,
            grounded_item.claims,
        )
        if grounded_expression is None:
            return None
        grounded_item.requirement_expression = grounded_expression
    return grounded_item


def _provider_only_span(
    posting_text: str,
    provider_item: RoleEvidence,
    evidence_text: str,
) -> tuple[int, int] | None:
    if provider_item.evidence_start is not None or provider_item.evidence_end is not None:
        if provider_item.evidence_start is None or provider_item.evidence_end is None:
            return None
        start = provider_item.evidence_start
        end = provider_item.evidence_end
        if start < 0 or end <= start or posting_text[start:end] != evidence_text:
            return None
        return start, end
    matches = list(re.finditer(re.escape(evidence_text), posting_text))
    if len(matches) != 1:
        return None
    return matches[0].start(), matches[0].end()


def _grounded_provider_requirement_expression(
    expression: RequirementExpression,
    evidence: RoleEvidence,
    claims: list[RoleClaim],
) -> RequirementExpression | None:
    evidence_start = evidence.evidence_start
    evidence_end = evidence.evidence_end
    if evidence_start is None or evidence_end is None:
        return None
    if (
        expression.source_start != evidence_start
        or expression.source_end != evidence_end
    ):
        return None
    return _grounded_provider_requirement_node(
        expression,
        evidence_text=evidence.evidence_text,
        evidence_start=evidence_start,
        evidence_end=evidence_end,
        parent_start=evidence_start,
        parent_end=evidence_end,
        claims=claims,
    )


def _grounded_provider_requirement_node(
    node: RequirementExpression,
    *,
    evidence_text: str,
    evidence_start: int,
    evidence_end: int,
    parent_start: int,
    parent_end: int,
    claims: list[RoleClaim],
) -> RequirementExpression | None:
    start = node.source_start
    end = node.source_end
    if (
        start is None
        or end is None
        or start < evidence_start
        or end > evidence_end
        or start < parent_start
        or end > parent_end
        or end <= start
    ):
        return None
    relative_start = start - evidence_start
    relative_end = end - evidence_start
    grounded_text = evidence_text[relative_start:relative_end]
    if node.text is None or node.text != grounded_text:
        return None

    if node.operator == "claim":
        if node.children:
            return None
        exact_claims = [
            claim
            for claim in claims
            if claim.source_start == start
            and claim.source_end == end
            and claim.text == grounded_text
        ]
        if not _provider_claim_leaf_connectors_are_exact(
            grounded_text,
            exact_claims,
        ):
            return None
        return RequirementExpression(
            node_id=f"role-requirement-{start}-{end}-claim",
            operator="claim",
            claim_id=(
                exact_claims[0].claim_id if len(exact_claims) == 1 else None
            ),
            text=grounded_text,
            source_start=start,
            source_end=end,
        )

    if len(node.children) < 2:
        return None
    grounded_children: list[RequirementExpression] = []
    previous_end = start
    for child in node.children:
        grounded_child = _grounded_provider_requirement_node(
            child,
            evidence_text=evidence_text,
            evidence_start=evidence_start,
            evidence_end=evidence_end,
            parent_start=start,
            parent_end=end,
            claims=claims,
        )
        if (
            grounded_child is None
            or grounded_child.source_start is None
            or grounded_child.source_end is None
            or grounded_child.source_start < previous_end
        ):
            return None
        grounded_children.append(grounded_child)
        previous_end = grounded_child.source_end
    if not _provider_requirement_connectors_are_exact(
        node.operator,
        grounded_text,
        node_start=start,
        children=grounded_children,
    ):
        return None
    return RequirementExpression(
        node_id=f"role-requirement-{start}-{end}-{node.operator}",
        operator=node.operator,
        text=grounded_text,
        source_start=start,
        source_end=end,
        children=grounded_children,
    )


def _provider_claim_leaf_connectors_are_exact(
    grounded_text: str,
    exact_claims: list[RoleClaim],
) -> bool:
    has_and = bool(_logical_and_matches(grounded_text))
    has_or = bool(_logical_or_matches(grounded_text))
    if not has_and and not has_or:
        return True
    if len(exact_claims) != 1 or (has_and and has_or):
        return False
    coordination = exact_claims[0].coordination
    if has_and:
        return coordination == "all_of"
    alternative_cue = bool(
        re.search(
            r"\bor\s+equivalent\b|\bequivalent\s+(?:education|experience)\b",
            grounded_text,
            re.IGNORECASE,
        )
    )
    return coordination == ("alternative" if alternative_cue else "any_of")


def _provider_requirement_connectors_are_exact(
    operator: str,
    node_text: str,
    *,
    node_start: int,
    children: list[RequirementExpression],
) -> bool:
    child_spans = [
        (child.source_start - node_start, child.source_end - node_start)
        for child in children
        if child.source_start is not None and child.source_end is not None
    ]
    if len(child_spans) != len(children):
        return False

    delimiter_depths = _expression_delimiter_depths(node_text)
    expression_depth = delimiter_depths[child_spans[0][0]]
    top_level_and = [
        match.span()
        for match in _logical_and_matches(node_text)
        if delimiter_depths[match.start()] == expression_depth
    ]
    top_level_or = [
        match.span()
        for match in _logical_or_matches(node_text)
        if delimiter_depths[match.start()] == expression_depth
    ]
    gaps = [
        (left[1], right[0])
        for left, right in zip(child_spans, child_spans[1:])
    ]

    def contained_in_gap(span: tuple[int, int]) -> bool:
        return any(start <= span[0] and span[1] <= end for start, end in gaps)

    def contained_in_child(span: tuple[int, int]) -> bool:
        return any(
            start <= span[0] and span[1] <= end for start, end in child_spans
        )

    if any(
        not (contained_in_gap(span) or contained_in_child(span))
        for span in [*top_level_and, *top_level_or]
    ):
        return False

    prefix = node_text[: child_spans[0][0]]
    suffix = node_text[child_spans[-1][1] :]
    if prefix.rstrip().endswith(","):
        return False
    if suffix.lstrip().startswith(",") and lexical_tokens(suffix.lstrip()[1:]):
        return False

    has_and = False
    has_or = False
    has_comma = False
    for gap_start, gap_end in gaps:
        gap = node_text[gap_start:gap_end]
        gap_tokens = set(lexical_tokens(gap))
        if gap_tokens - {"also", "and", "but", "or"}:
            return False
        gap_and = any(
            gap_start <= start and end <= gap_end for start, end in top_level_and
        ) or bool(re.search(r"\bbut\s+also\b", gap, re.IGNORECASE))
        gap_or = any(
            gap_start <= start and end <= gap_end for start, end in top_level_or
        )
        gap_comma = "," in gap
        if not (gap_and or gap_or or gap_comma):
            return False
        has_and = has_and or gap_and
        has_or = has_or or gap_or
        has_comma = has_comma or gap_comma

    alternative_cue = bool(
        re.search(
            r"\bor\s+equivalent\b|\bequivalent\s+(?:education|experience)\b",
            node_text,
            re.IGNORECASE,
        )
    )
    if operator == "all_of":
        return not has_or and (has_and or has_comma)
    if operator == "alternative":
        return has_or and not has_and and alternative_cue
    if operator == "any_of":
        return has_or and not has_and and not alternative_cue
    return False


def project_role_evidence(
    evidence: list[RoleEvidence],
    *,
    unsectioned_text: str = "",
) -> RoleSemanticProjection:
    enriched: list[RoleEvidence] = []
    for original in evidence:
        item = original.model_copy(deep=True)
        is_environment = any(claim.kind == "environment" for claim in item.claims)
        detected_entities = (
            _technologies_for_texts(
                [item.text],
                assume_capability_context=True,
            )
            if is_environment
            else _capabilities_for_texts([item.text])
        )
        supported_entities = _provider_capabilities_for_evidence(
            item.entities,
            item.evidence_text,
            claims=item.claims,
            source_section=item.source_section,
        )
        item.entities = _merge_unique(detected_entities, supported_entities)
        _enrich_claim_entities(item)
        item.entities = _merge_unique(item.entities, _claim_entity_names(item.claims))
        if (
            item.kind != "context"
            or item.entities
            or any(claim.kind == "environment" for claim in item.claims)
            or item.alternative_claims
        ):
            enriched.append(item)

    responsibilities, must_have, nice_to_have = derive_role_semantic_lists(enriched)
    required_claims = _claims_matching(
        enriched,
        kinds={"qualification", "eligibility"},
        modalities={"mandatory", "expected"},
        polarities={"positive"},
    )
    preferred_claims = _claims_matching(
        enriched,
        kinds={"qualification", "eligibility"},
        modalities={"preferred"},
        polarities={"positive"},
    )
    responsibility_claims = _claims_matching(
        enriched,
        kinds={"responsibility"},
        polarities={"positive"},
    )
    environment_claims = _claims_matching(
        enriched,
        kinds={"environment"},
        polarities={"positive"},
    )
    required_texts = [claim.text for claim in required_claims]
    preferred_texts = [claim.text for claim in preferred_claims]
    responsibility_texts = [claim.text for claim in responsibility_claims]
    environment_texts = [claim.text for claim in environment_claims]

    required_capabilities = _merge_unique(
        _capabilities_for_texts(required_texts),
        _claim_entity_names(required_claims),
    )
    preferred_capabilities = _merge_unique(
        _capabilities_for_texts(preferred_texts),
        _claim_entity_names(preferred_claims),
    )
    responsibility_capabilities = _merge_unique(
        _capabilities_for_texts(responsibility_texts),
        _claim_entity_names(responsibility_claims),
    )
    environment_capabilities = _merge_unique(
        _technologies_for_texts(
            environment_texts,
            assume_capability_context=True,
        ),
        _claim_entity_names(environment_claims),
    )
    categorized_capabilities = _merge_unique(
        required_capabilities,
        preferred_capabilities,
        responsibility_capabilities,
        environment_capabilities,
    )
    categorized_texts = [
        *required_texts,
        *preferred_texts,
        *responsibility_texts,
        *environment_texts,
    ]
    candidate_text = "\n".join(
        item for item in [unsectioned_text, *categorized_texts] if item
    )
    global_capabilities = _capabilities_for_texts(
        [unsectioned_text, *categorized_texts]
    )
    capabilities = _merge_unique(categorized_capabilities, global_capabilities)
    guarded_technology_texts = [
        item
        for item in (
            unsectioned_text,
            *required_texts,
            *preferred_texts,
            *responsibility_texts,
        )
        if item
    ]
    technology_candidates = _merge_unique(
        _technologies_for_texts(guarded_technology_texts),
        environment_capabilities,
    )
    tech_stack = _ordered_technology_names(technology_candidates)
    ambiguity_fields = (
        ["role.evidence"]
        if any(item.quality_flags or item.alternative_claims for item in enriched)
        else []
    )
    return RoleSemanticProjection(
        evidence=enriched,
        responsibilities=responsibilities,
        must_have_requirements=must_have,
        nice_to_have_requirements=nice_to_have,
        required_capabilities=required_capabilities,
        preferred_capabilities=preferred_capabilities,
        responsibility_capabilities=responsibility_capabilities,
        environment_capabilities=environment_capabilities,
        capabilities=capabilities,
        tech_stack=tech_stack,
        candidate_text=candidate_text,
        ambiguity_fields=ambiguity_fields,
    )


def _claims_matching(
    evidence: list[RoleEvidence],
    *,
    kinds: set[str],
    modalities: set[str] | None = None,
    polarities: set[str] | None = None,
) -> list[RoleClaim]:
    return [
        claim
        for item in evidence
        for claim in item.claims
        if claim.kind in kinds and (modalities is None or claim.modality in modalities)
        and (polarities is None or claim.polarity in polarities)
    ]


def _enrich_claim_entities(item: RoleEvidence) -> None:
    for claim in item.claims:
        detected = (
            _technologies_for_texts(
                [claim.text],
                assume_capability_context=True,
            )
            if claim.kind == "environment"
            else _capabilities_for_texts([claim.text])
        )
        existing = _claim_entity_names([claim])
        for capability in _merge_unique(detected, existing):
            if capability in existing:
                continue
            technology_match = technology_source_match(
                capability,
                claim.text,
                assume_capability_context=claim.kind == "environment",
            )
            role_match = (
                None
                if technology_match is not None
                else _role_capability_source_match(capability, claim.text)
            )
            if technology_match is not None:
                surface_text = technology_match.surface_text
                start = technology_match.start
                end = technology_match.end
                entity_type = technology_match.entity_type
            elif role_match is not None:
                surface_text, start, end = role_match
                entity_type = "unknown"
            else:
                start, end = _entity_character_span(capability, claim.text)
                surface_text = claim.text[start:end] if start is not None and end is not None else capability
                entity_type = technology_entity_type(capability)
            claim.entities.append(
                CapabilityMention(
                    surface_text=surface_text,
                    canonical_name=capability,
                    entity_type=entity_type,
                    source_start=(
                        claim.source_start + start
                        if claim.source_start is not None and start is not None
                        else None
                    ),
                    source_end=(
                        claim.source_start + end
                        if claim.source_start is not None and end is not None
                        else None
                    ),
                    confidence=0.95,
                )
            )
        claim.entities.sort(
            key=lambda mention: (
                mention.source_start is None,
                mention.source_start if mention.source_start is not None else 0,
            )
        )


def _entity_character_span(
    capability: str,
    evidence_text: str,
) -> tuple[int | None, int | None]:
    match = re.search(re.escape(capability), evidence_text, re.IGNORECASE)
    if match is None:
        return None, None
    return match.start(), match.end()


def _claim_entity_names(claims: list[RoleClaim]) -> list[str]:
    names: list[str] = []
    for claim in claims:
        for mention in claim.entities:
            value = (mention.canonical_name or mention.surface_text).strip()
            if value and value not in names:
                names.append(value)
    return names


def _provider_item_is_grounded(
    provider_item: RoleEvidence,
    deterministic_item: RoleEvidence,
    provider_text: str,
) -> bool:
    if _normalized_evidence_text(deterministic_item.evidence_text) != provider_text:
        return False
    if (
        provider_item.evidence_start is not None
        and provider_item.evidence_start != deterministic_item.evidence_start
    ):
        return False
    if (
        provider_item.evidence_end is not None
        and provider_item.evidence_end != deterministic_item.evidence_end
    ):
        return False
    return True


def _provider_grounded_match_index(
    provider_item: RoleEvidence,
    deterministic_evidence: list[RoleEvidence],
    unmatched: set[int],
    provider_text: str,
) -> int | None:
    grounded = [
        index
        for index in sorted(unmatched)
        if _provider_item_is_grounded(
            provider_item,
            deterministic_evidence[index],
            provider_text,
        )
    ]
    if not grounded:
        return None

    same_section = [
        index
        for index in grounded
        if deterministic_evidence[index].source_section == provider_item.source_section
    ]
    if len(same_section) == 1:
        return same_section[0]
    if len(same_section) > 1:
        return None
    return grounded[0] if len(grounded) == 1 else None


def _provider_semantics_agree(
    provider_item: RoleEvidence,
    deterministic_item: RoleEvidence,
) -> bool:
    if provider_item.claims:
        return _claim_signatures(provider_item.claims) == _claim_signatures(
            deterministic_item.claims
        )
    return (
        provider_item.source_section == deterministic_item.source_section
        and provider_item.kind == deterministic_item.kind
        and provider_item.modality == deterministic_item.modality
    )


def _claim_signatures(claims: list[RoleClaim]) -> tuple[tuple[str, ...], ...]:
    return tuple(sorted(_claim_signature(claim) for claim in claims))


def _claim_signature(claim: RoleClaim) -> tuple[str, ...]:
    """Identity shared by semantic agreement and order-independent enrichment."""
    return (
        claim.kind,
        claim.modality,
        claim.owner,
        claim.polarity,
        _normalized_evidence_text(claim.text),
        _normalized_evidence_text(claim.condition_text or ""),
        claim.condition_type or "",
        claim.coordination,
    )


def _grounded_provider_claims(
    provider_item: RoleEvidence,
    deterministic_item: RoleEvidence,
) -> list[RoleClaim]:
    candidates = provider_item.claims or [
        _claim_from_legacy_evidence(provider_item, deterministic_item)
    ]
    grounded: list[RoleClaim] = []
    for original in candidates:
        claim = original.model_copy(deep=True)
        claim_span = _grounded_text_span(
            claim.text,
            deterministic_item.evidence_text,
        )
        if claim_span is None:
            return []
        claim.text = deterministic_item.evidence_text[
            claim_span[0] : claim_span[1]
        ]
        condition = (claim.condition_text or "").strip()
        if condition and not _claim_text_is_grounded(condition, claim.text):
            return []
        claim.condition_text = condition or None
        claim.source_start = (
            deterministic_item.evidence_start + claim_span[0]
            if deterministic_item.evidence_start is not None
            else None
        )
        claim.source_end = (
            deterministic_item.evidence_start + claim_span[1]
            if deterministic_item.evidence_start is not None
            else None
        )
        claim.entities = _grounded_claim_mentions(
            claim.entities,
            evidence_text=claim.text,
            evidence_start=claim.source_start,
            claim=claim,
            source_section=deterministic_item.source_section,
        )
        grounded.append(claim)
    return grounded


def _claim_from_legacy_evidence(
    provider_item: RoleEvidence,
    deterministic_item: RoleEvidence,
) -> RoleClaim:
    kind = {
        "responsibility": "responsibility",
        "requirement": "qualification",
        "context": "company_context",
    }[provider_item.kind]
    modality = {
        "required": "mandatory",
        "preferred": "preferred",
        "descriptive": "descriptive",
        "non_requirement": "optional",
    }[provider_item.modality]
    mentions = [
        CapabilityMention(surface_text=value, canonical_name=value)
        for value in _provider_capabilities_for_evidence(
            provider_item.entities,
            provider_item.evidence_text,
            source_section=provider_item.source_section,
        )
    ]
    return RoleClaim(
        kind=kind,
        modality=modality,
        source_start=deterministic_item.evidence_start,
        source_end=deterministic_item.evidence_end,
        text=deterministic_item.evidence_text,
        entities=mentions,
        confidence=provider_item.confidence,
    )


def _claim_text_is_grounded(claim_text: str, evidence_text: str) -> bool:
    normalized_claim = _normalized_evidence_text(claim_text)
    normalized_evidence = _normalized_evidence_text(evidence_text)
    return bool(normalized_claim and normalized_claim in normalized_evidence)


def _grounded_text_span(
    value: str,
    evidence_text: str,
) -> tuple[int, int] | None:
    words = value.strip().split()
    if not words:
        return None
    match = re.search(
        r"\s+".join(re.escape(word) for word in words),
        evidence_text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return match.start(), match.end()


def _grounded_claim_mentions(
    mentions: list[CapabilityMention],
    *,
    evidence_text: str,
    evidence_start: int | None,
    claim: RoleClaim,
    source_section: str,
) -> list[CapabilityMention]:
    grounded: list[CapabilityMention] = []
    for mention in mentions:
        surface = mention.surface_text.strip()
        surface_match = (
            re.search(re.escape(surface), evidence_text, re.IGNORECASE)
            if surface
            else None
        )
        if surface_match is None:
            continue
        canonical = _provider_capabilities_for_evidence(
            [surface],
            evidence_text,
            claims=[claim],
            source_section=source_section,
        )
        if len(canonical) != 1:
            continue
        canonical_name = canonical[0]
        grounded.append(
            CapabilityMention(
                surface_text=surface_match.group(0),
                canonical_name=canonical_name,
                entity_type=(
                    technology_entity_type(canonical_name)
                    if technology_entity_type(canonical_name) != "unknown"
                    else mention.entity_type
                ),
                source_start=(
                    evidence_start + surface_match.start()
                    if evidence_start is not None
                    else None
                ),
                source_end=(
                    evidence_start + surface_match.end()
                    if evidence_start is not None
                    else None
                ),
                confidence=mention.confidence,
            )
        )
    return grounded


def _merge_provider_claim_entities(
    deterministic_item: RoleEvidence,
    provider_item: RoleEvidence,
) -> None:
    if not provider_item.claims:
        for claim in deterministic_item.claims:
            provider_mentions = [
                CapabilityMention(
                    surface_text=value,
                    canonical_name=value,
                    confidence=provider_item.confidence,
                )
                for value in provider_item.entities
            ]
            supported = _grounded_claim_mentions(
                provider_mentions,
                evidence_text=claim.text,
                evidence_start=claim.source_start,
                claim=claim,
                source_section=deterministic_item.source_section,
            )
            existing = _claim_entity_names([claim])
            for mention in supported:
                value = mention.canonical_name or mention.surface_text
                if value in existing:
                    continue
                claim.entities.append(mention)
                existing.append(value)
        return
    if len(provider_item.claims) != len(deterministic_item.claims):
        return
    provider_claims_by_signature: dict[tuple[str, ...], list[RoleClaim]] = {}
    for provider_claim in provider_item.claims:
        provider_claims_by_signature.setdefault(
            _claim_signature(provider_claim),
            [],
        ).append(provider_claim)

    for deterministic_claim in deterministic_item.claims:
        matching_claims = provider_claims_by_signature.get(
            _claim_signature(deterministic_claim),
            [],
        )
        if not matching_claims:
            return
        provider_claim = matching_claims.pop(0)
        deterministic_claim.entities = _grounded_claim_mentions(
            provider_claim.entities,
            evidence_text=deterministic_claim.text,
            evidence_start=deterministic_claim.source_start,
            claim=deterministic_claim,
            source_section=deterministic_item.source_section,
        )


def _normalized_evidence_text(value: str) -> str:
    return " ".join(value.split())


def _capabilities_for_texts(
    texts: list[str],
    *,
    assume_capability_context: bool = False,
) -> list[str]:
    fragments = [item for item in texts if item]
    if not fragments:
        return []
    tech_stack = _technologies_for_texts(
        fragments,
        assume_capability_context=assume_capability_context,
    )
    role_capabilities = _merge_unique(
        *[_extract_role_capabilities(fragment, []) for fragment in fragments]
    )
    return _merge_unique(tech_stack, role_capabilities)


def _technologies_for_texts(
    texts: list[str],
    *,
    assume_capability_context: bool = False,
) -> list[str]:
    fragments = [item for item in texts if item]
    if not fragments:
        return []
    detected = _merge_unique(
        *[
            _extract_tech_stack(
                fragment,
                assume_capability_context=assume_capability_context,
            )
            for fragment in fragments
        ]
    )
    return _ordered_technology_names(detected)


def _ordered_technology_names(values: list[str]) -> list[str]:
    if not values:
        return []
    ontology_order = _extract_tech_stack(
        "\n".join(values),
        assume_capability_context=True,
    )
    return _merge_unique(ontology_order, values)


def _entities_for(
    evidence: list[RoleEvidence],
    *,
    kind: str,
    modality: str | None = None,
) -> list[str]:
    return _merge_unique(
        *[
            item.entities
            for item in evidence
            if item.kind == kind and (modality is None or item.modality == modality)
        ]
    )


def _entity_is_supported(entity: str, evidence_text: str) -> bool:
    entity_tokens = lexical_tokens(str(entity))
    evidence_tokens = lexical_tokens(evidence_text)
    if not entity_tokens:
        return False
    width = len(entity_tokens)
    return any(
        evidence_tokens[index : index + width] == entity_tokens
        for index in range(len(evidence_tokens) - width + 1)
    )


def _provider_capabilities_for_evidence(
    entities: list[str],
    evidence_text: str,
    *,
    claims: list[RoleClaim] | None = None,
    source_section: str | None = None,
    require_explicit_scope: bool = False,
) -> list[str]:
    evidence_tokens = lexical_tokens(evidence_text)
    candidates: list[_ProviderEntityCandidate] = []
    for entity in entities:
        value = str(entity).strip()
        if not value or not _entity_is_supported(value, evidence_text):
            continue
        tokens = lexical_tokens(value)
        canonical = _capabilities_for_texts([value])
        if not canonical and not _is_capability_entity_shape(tokens):
            continue
        for occurrence in _token_occurrences(tokens, evidence_tokens):
            candidates.append(
                _ProviderEntityCandidate(
                    value=value,
                    tokens=tokens,
                    canonical=canonical,
                    start=occurrence,
                    end=occurrence + len(tokens),
                )
            )

    semantic_capability_scope = bool(
        source_section == "capabilities"
        and any(
            claim.kind in {"qualification", "responsibility", "environment"}
            for claim in claims or []
        )
    )
    scoped_entities = {
        member.value
        for group in _coordinate_entity_groups(candidates, evidence_tokens)
        if semantic_capability_scope
        or _entity_group_has_capability_scope(group, evidence_tokens)
        for member in group.members
    }
    capabilities: list[str] = []
    for candidate in candidates:
        if candidate.canonical:
            if require_explicit_scope and candidate.value not in scoped_entities:
                continue
            capabilities = _merge_unique(capabilities, candidate.canonical)
        elif candidate.value in scoped_entities:
            capabilities = _merge_unique(capabilities, [candidate.value])
    return capabilities


def _is_capability_entity_shape(tokens: tuple[str, ...]) -> bool:
    if not tokens or len(tokens) > 6:
        return False
    if any(token in _NON_CAPABILITY_ENTITY_HEADS for token in tokens):
        return False
    return any(any(character.isalpha() for character in token) for token in tokens)


def _token_occurrences(
    tokens: tuple[str, ...],
    evidence_tokens: tuple[str, ...],
) -> tuple[int, ...]:
    width = len(tokens)
    return tuple(
        index
        for index in range(len(evidence_tokens) - width + 1)
        if evidence_tokens[index : index + width] == tokens
    )


def _coordinate_entity_groups(
    candidates: list[_ProviderEntityCandidate],
    evidence_tokens: tuple[str, ...],
) -> list[_EntityGroup]:
    ordered = sorted(candidates, key=lambda item: (item.start, -item.end))
    groups: list[list[_ProviderEntityCandidate]] = []
    for candidate in ordered:
        if groups and candidate.start < groups[-1][-1].end:
            continue
        if not groups:
            groups.append([candidate])
            continue
        previous = groups[-1][-1]
        linking_tokens = evidence_tokens[previous.end : candidate.start]
        if all(token in _CAPABILITY_COORDINATORS for token in linking_tokens):
            groups[-1].append(candidate)
        else:
            groups.append([candidate])
    return [_EntityGroup(members=tuple(group)) for group in groups]


def _entity_group_has_capability_scope(
    group: _EntityGroup,
    evidence_tokens: tuple[str, ...],
) -> bool:
    prefix = evidence_tokens[: group.start]
    suffix = evidence_tokens[group.end :]
    has_governor = any(
        prefix[-len(phrase) :] == phrase for phrase in _CAPABILITY_GOVERNOR_PREFIXES
    )
    has_modality = any(
        suffix[: len(phrase)] == phrase for phrase in _CAPABILITY_MODAL_SUFFIXES
    )
    has_noun_compound = bool(
        suffix
        and suffix[0] in _CAPABILITY_QUALIFICATION_HEADS
        and (
            any(token in {"preferred", "required"} for token in suffix[1:6])
            or any(token in {"year", "years", "month", "months"} for token in prefix[-6:])
        )
    )
    has_capability_bearing_suffix = bool(
        suffix and suffix[0] in {"development", "skill", "skills"}
    )
    has_standalone_qualification_nominal = bool(
        len(suffix) == 1 and suffix[0] in _CAPABILITY_QUALIFICATION_HEADS
    )
    return (
        has_governor
        or has_modality
        or has_noun_compound
        or has_capability_bearing_suffix
        or has_standalone_qualification_nominal
    )


def _merge_unique(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in merged:
                merged.append(item)
    return merged
