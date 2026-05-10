#!/usr/bin/env python3
"""
Confidence formulas for trust-layer entries.

One pure function per entity type. Each returns (confidence, signal_breakdown):
- confidence: float in [0.0, 1.0]
- signal_breakdown: dict matching the signal keys documented in
  plugins/agami/shared/agami-osi-extensions.md → Trust-layer extensions →
  agami.signal_breakdown.

Per the plan, weights are explicit top-level constants for easy tuning. The
formulas are starting points — calibrate from real introspect data.

No I/O, no DB. Stdlib only.
"""

from __future__ import annotations


# --- Universal caps --------------------------------------------------------

# When the introspect step's only basis for the proposal is the LLM (i.e., no
# DB-side signal fired and no DBA-authored evidence exists), cap the score so
# pure hallucination cannot reach the auto-approve band.
LLM_ONLY_CAP: float = 0.30

# When a join's source/target column types don't match exactly, the join
# couldn't possibly be valid — treat the entry as low-confidence regardless of
# any other signals.
JOIN_NO_TYPE_MATCH_CAP: float = 0.30

# When a metric's source column is not a numeric type, aggregate operations
# would error at runtime. Cap the score so we never auto-approve such a
# proposal.
METRIC_NON_NUMERIC_CAP: float = 0.30

# When a named-filter's predicate doesn't type-check against the referenced
# column, the filter is broken at SQL time — cap as if it were LLM-only.
NAMED_FILTER_NO_TYPECHECK_CAP: float = 0.30


# --- Join (relationship) weights -------------------------------------------

W_JOIN_FK_DECLARED: float = 0.50
W_JOIN_PK_OVERLAP: float = 0.20
W_JOIN_UNIQUE_INDEX_MATCH: float = 0.20
W_JOIN_TYPE_MATCH: float = 0.10
W_JOIN_NAME_SIMILARITY: float = 0.15  # only awarded when jaccard ≥ threshold
W_JOIN_NAME_SIMILARITY_THRESHOLD: float = 0.70
W_JOIN_PLURAL_PATTERN: float = 0.10


def confidence_for_join(
    *,
    fk_declared: bool = False,
    pk_overlap: bool = False,
    unique_index_match: bool = False,
    column_type_match: bool = False,
    column_name_similarity: float = 0.0,
    plural_pattern_match: bool = False,
    llm_inferred: bool = False,
) -> tuple[float, dict]:
    """Confidence for an OSI relationship (join).

    Inputs are observations about the source DB metadata. The introspect step
    fills these in; the formula combines them into a single score.

    A typical FK-declared join with type-match scores 1.0 (clamped from 1.05).
    A column-name-only inferred join with type-match scores ~0.35.
    """
    score = 0.0
    if fk_declared:
        score += W_JOIN_FK_DECLARED
    if pk_overlap:
        score += W_JOIN_PK_OVERLAP
    if unique_index_match:
        score += W_JOIN_UNIQUE_INDEX_MATCH
    if column_type_match:
        score += W_JOIN_TYPE_MATCH
    if column_name_similarity >= W_JOIN_NAME_SIMILARITY_THRESHOLD:
        score += W_JOIN_NAME_SIMILARITY
    if plural_pattern_match:
        score += W_JOIN_PLURAL_PATTERN

    # Hard cap: type mismatch makes the join broken regardless of any other
    # signal. (FK-declared joins with type mismatch are vanishingly rare —
    # this guards the heuristic path.)
    if not column_type_match:
        score = min(score, JOIN_NO_TYPE_MATCH_CAP)

    if llm_inferred:
        score = min(score, LLM_ONLY_CAP)

    score = max(0.0, min(1.0, score))

    breakdown = {
        "fk_declared": fk_declared,
        "pk_overlap": pk_overlap,
        "unique_index_match": unique_index_match,
        "column_type_match": column_type_match,
        "column_name_similarity": column_name_similarity,
        "plural_pattern_match": plural_pattern_match,
        "llm_inferred": llm_inferred,
    }
    return score, breakdown


# --- Metric weights --------------------------------------------------------

W_METRIC_DBA_COMMENT_MEASURE: float = 0.40
W_METRIC_WELL_KNOWN_PATTERN: float = 0.25
W_METRIC_NUMERIC_TYPE: float = 0.15
W_METRIC_AGGREGATE_FRIENDLY: float = 0.10
W_METRIC_SYNONYM_MATCH: float = 0.10


def confidence_for_metric(
    *,
    dba_column_comment_measure: bool = False,
    well_known_measure_pattern: bool = False,
    numeric_type: bool = False,
    aggregate_friendly_distribution: bool = False,
    synonym_match: bool = False,
    llm_inferred: bool = False,
) -> tuple[float, dict]:
    """Confidence for an OSI metric.

    The score informs how prominently the metric appears in the review
    dashboard, but it does NOT bypass Rule 1 — every metric requires human
    sign-off before being usable in answers, regardless of confidence.
    """
    score = 0.0
    if dba_column_comment_measure:
        score += W_METRIC_DBA_COMMENT_MEASURE
    if well_known_measure_pattern:
        score += W_METRIC_WELL_KNOWN_PATTERN
    if numeric_type:
        score += W_METRIC_NUMERIC_TYPE
    if aggregate_friendly_distribution:
        score += W_METRIC_AGGREGATE_FRIENDLY
    if synonym_match:
        score += W_METRIC_SYNONYM_MATCH

    if not numeric_type:
        score = min(score, METRIC_NON_NUMERIC_CAP)

    if llm_inferred:
        score = min(score, LLM_ONLY_CAP)

    score = max(0.0, min(1.0, score))

    breakdown = {
        "dba_column_comment": dba_column_comment_measure,
        "well_known_measure_pattern": well_known_measure_pattern,
        "numeric_type": numeric_type,
        "aggregate_friendly_distribution": aggregate_friendly_distribution,
        "synonym_match": synonym_match,
        "llm_inferred": llm_inferred,
    }
    return score, breakdown


# --- Field description weights --------------------------------------------

W_FIELD_DBA_COLUMN_COMMENT: float = 0.70
W_FIELD_BUSINESS_TERM_DICT: float = 0.20
W_FIELD_ENUM_LIKE_DISTRIBUTION: float = 0.15
LLM_ONLY_FIELD_CAP: float = 0.40  # field descriptions are lower-stakes than joins/metrics


def confidence_for_field_description(
    *,
    dba_column_comment: bool = False,
    business_term_match: bool = False,
    enum_like_distribution: bool = False,
    llm_inferred: bool = False,
) -> tuple[float, dict]:
    """Confidence for a field-level description / enum / type mapping.

    A column comment authored by the DBA is the strongest signal — it's by
    construction human-authored and trustworthy, so a single comment lifts the
    confidence above the auto-approve threshold by itself.
    """
    score = 0.0
    if dba_column_comment:
        score += W_FIELD_DBA_COLUMN_COMMENT
    if business_term_match:
        score += W_FIELD_BUSINESS_TERM_DICT
    if enum_like_distribution:
        score += W_FIELD_ENUM_LIKE_DISTRIBUTION

    if llm_inferred and not dba_column_comment:
        score = min(score, LLM_ONLY_FIELD_CAP)

    score = max(0.0, min(1.0, score))

    breakdown = {
        "dba_column_comment": dba_column_comment,
        "business_term_match": business_term_match,
        "enum_like_distribution": enum_like_distribution,
        "llm_inferred": llm_inferred,
    }
    return score, breakdown


# --- Named filter weights -------------------------------------------------

W_NF_DBA_BUSINESS_TERM: float = 0.40
W_NF_WELL_KNOWN_TERM: float = 0.25
W_NF_PREDICATE_TYPECHECKS: float = 0.15
W_NF_SYNONYM_MATCH: float = 0.15
LLM_ONLY_NAMED_FILTER_CAP: float = 0.35


def confidence_for_named_filter(
    *,
    dba_business_term: bool = False,
    well_known_term: bool = False,
    predicate_typechecks: bool = False,
    synonym_match: bool = False,
    llm_inferred: bool = False,
) -> tuple[float, dict]:
    """Confidence for a named filter (e.g., 'active_customer').

    Like metrics, named filters always require Rule 1 sign-off before runtime
    use — confidence informs review priority but never bypasses review.
    """
    score = 0.0
    if dba_business_term:
        score += W_NF_DBA_BUSINESS_TERM
    if well_known_term:
        score += W_NF_WELL_KNOWN_TERM
    if predicate_typechecks:
        score += W_NF_PREDICATE_TYPECHECKS
    if synonym_match:
        score += W_NF_SYNONYM_MATCH

    if not predicate_typechecks:
        score = min(score, NAMED_FILTER_NO_TYPECHECK_CAP)

    if llm_inferred:
        score = min(score, LLM_ONLY_NAMED_FILTER_CAP)

    score = max(0.0, min(1.0, score))

    breakdown = {
        "dba_business_term": dba_business_term,
        "well_known_term": well_known_term,
        "predicate_typechecks": predicate_typechecks,
        "synonym_match": synonym_match,
        "llm_inferred": llm_inferred,
    }
    return score, breakdown


# --- Path confidence (multi-hop join paths) --------------------------------

def confidence_for_join_path(edge_confidences: list[float]) -> float:
    """Confidence of a multi-hop join path = minimum confidence of its edges.

    A 3-hop path is only as trustworthy as its weakest link. The query planner
    uses this to prefer shorter, FK-cleaner paths over equally-typed but
    edge-heuristic alternatives.
    """
    if not edge_confidences:
        return 0.0
    return min(edge_confidences)
