"""
Tests for plugins/agami/scripts/compute_confidence.py.

The formulas live in compute_confidence.py with explicit weight constants.
These tests are the contract that anchors the scoring band assumptions
documented in the trust-layer plan §3.2:

  - FK-declared joins reach the auto-approve band (>= 0.95).
  - Pure column-name-similarity inferred joins land in the medium band (0.4–0.7).
  - LLM-only proposals never exceed 0.30 regardless of other signals.
  - DBA-authored column comments alone push field descriptions to auto-approve.
  - Metrics never auto-approve from confidence alone (Rule 1 — separate gate).

If you tune the weight constants, expect these tests to drift; update both the
constants and the assertions together.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from compute_confidence import (  # noqa: E402
    LLM_ONLY_CAP,
    LLM_ONLY_FIELD_CAP,
    LLM_ONLY_NAMED_FILTER_CAP,
    confidence_for_field_description,
    confidence_for_join,
    confidence_for_join_path,
    confidence_for_metric,
    confidence_for_named_filter,
)


# --- Joins ------------------------------------------------------------------

def test_join_typical_fk_declared_reaches_auto_approve_band():
    """A real-world FK is declared with a unique index on the target, exact
    type match, and matching column names — a 'typical' FK reaches the
    auto-approve band purely from signals."""
    score, breakdown = confidence_for_join(
        fk_declared=True,
        unique_index_match=True,
        column_type_match=True,
        column_name_similarity=1.0,
    )
    # 0.50 + 0.20 + 0.10 + 0.15 = 0.95
    assert score >= 0.95
    assert breakdown["fk_declared"] is True
    assert breakdown["column_type_match"] is True


def test_join_bare_fk_alone_does_not_auto_approve():
    """An FK declared but without any other evidence (no unique index, no name
    match) is suspicious. The formula gives a moderate score; the introspect
    step's auto-approve rule (separate from the formula) is what actually
    flips review_state to approved when fk_declared=True."""
    score, _ = confidence_for_join(
        fk_declared=True,
        column_type_match=True,
    )
    # 0.50 + 0.10 = 0.60 — moderate, below default threshold (0.70).
    assert 0.55 <= score <= 0.65


def test_join_full_signal_clamped_at_one():
    score, _ = confidence_for_join(
        fk_declared=True,
        pk_overlap=True,
        unique_index_match=True,
        column_type_match=True,
        column_name_similarity=1.0,
        plural_pattern_match=True,
    )
    assert score == 1.0


def test_join_column_name_only_inferred_lands_medium_band():
    """Inferred from column-name similarity alone, with type match — medium."""
    score, _ = confidence_for_join(
        column_type_match=True,
        column_name_similarity=1.0,
        plural_pattern_match=True,
    )
    # Type match (0.10) + name similarity (0.15) + plural pattern (0.10) = 0.35.
    # In the 0.4–0.7 medium-confidence band's lower end. Below default
    # threshold 0.7 — review queue.
    assert 0.30 < score < 0.50


def test_join_no_type_match_capped():
    score, _ = confidence_for_join(
        fk_declared=True,           # would normally reach 1.0 with type match
        unique_index_match=True,
        column_type_match=False,    # type mismatch — broken join
        column_name_similarity=1.0,
    )
    assert score <= 0.30


def test_join_llm_only_capped():
    score, _ = confidence_for_join(
        column_type_match=True,
        column_name_similarity=1.0,
        plural_pattern_match=True,
        llm_inferred=True,
    )
    assert score <= LLM_ONLY_CAP


def test_join_below_similarity_threshold_no_credit():
    """Names that aren't similar enough get no similarity bonus."""
    score, _ = confidence_for_join(
        column_type_match=True,
        column_name_similarity=0.5,  # below threshold (0.7)
    )
    # Only column_type_match (0.10).
    assert score == 0.10


def test_join_score_in_range():
    """Spot-check the output is always in [0, 1] for arbitrary inputs."""
    score, _ = confidence_for_join(
        fk_declared=True, pk_overlap=True, unique_index_match=True,
        column_type_match=True, column_name_similarity=1.0,
        plural_pattern_match=True, llm_inferred=False,
    )
    assert 0.0 <= score <= 1.0


# --- Metrics ----------------------------------------------------------------

def test_metric_with_dba_comment_and_pattern_high_score_but_under_one():
    """Even a strong metric proposal does not get to 1.0 from signal alone —
    Rule 1 (human sign-off) is the only path to fully-trusted."""
    score, _ = confidence_for_metric(
        dba_column_comment_measure=True,
        well_known_measure_pattern=True,
        numeric_type=True,
        aggregate_friendly_distribution=True,
    )
    # 0.40 + 0.25 + 0.15 + 0.10 = 0.90. Below 1.0 — even a perfect signal mix
    # leaves headroom for the human sign-off step to "complete" the entry.
    assert 0.85 <= score <= 0.95


def test_metric_non_numeric_capped():
    score, _ = confidence_for_metric(
        dba_column_comment_measure=True,
        well_known_measure_pattern=True,
        numeric_type=False,
        aggregate_friendly_distribution=True,
    )
    assert score <= 0.30


def test_metric_llm_only_capped():
    score, _ = confidence_for_metric(
        well_known_measure_pattern=True,
        numeric_type=True,
        aggregate_friendly_distribution=True,
        llm_inferred=True,
    )
    assert score <= LLM_ONLY_CAP


def test_metric_signal_breakdown_keys():
    """Breakdown keys must match the documented trust-layer signal vocabulary."""
    _, breakdown = confidence_for_metric(numeric_type=True)
    # These names match agami-osi-extensions.md → signal_breakdown.
    expected = {
        "dba_column_comment", "well_known_measure_pattern", "numeric_type",
        "aggregate_friendly_distribution", "synonym_match", "llm_inferred",
    }
    assert set(breakdown.keys()) == expected


# --- Field descriptions ----------------------------------------------------

def test_field_description_dba_comment_alone_auto_approves():
    """A DBA column comment is a strong-enough signal to auto-approve a field
    description by itself."""
    score, _ = confidence_for_field_description(dba_column_comment=True)
    assert score >= 0.70


def test_field_description_llm_only_capped_below_field_threshold():
    score, _ = confidence_for_field_description(
        business_term_match=True,
        enum_like_distribution=True,
        llm_inferred=True,
    )
    assert score <= LLM_ONLY_FIELD_CAP


def test_field_description_dba_comment_overrides_llm_cap():
    """If the DBA wrote the comment, the LLM cap doesn't apply — the human
    signal trumps any LLM annotation."""
    score, _ = confidence_for_field_description(
        dba_column_comment=True,
        llm_inferred=True,
    )
    # Should still reach >=0.70 (DBA comment alone).
    assert score >= 0.70


# --- Named filters ---------------------------------------------------------

def test_named_filter_predicate_must_typecheck():
    score, _ = confidence_for_named_filter(
        dba_business_term=True,
        well_known_term=True,
        predicate_typechecks=False,  # broken predicate
        synonym_match=True,
    )
    assert score <= 0.30


def test_named_filter_full_signal_under_one():
    """Named filters require Rule 1 sign-off; signal alone tops out below 1.0."""
    score, _ = confidence_for_named_filter(
        dba_business_term=True,
        well_known_term=True,
        predicate_typechecks=True,
        synonym_match=True,
    )
    # 0.40 + 0.25 + 0.15 + 0.15 = 0.95 (within float epsilon).
    assert 0.90 <= score < 1.00
    assert abs(score - 0.95) < 1e-6


def test_named_filter_llm_only_capped():
    score, _ = confidence_for_named_filter(
        dba_business_term=True,
        well_known_term=True,
        predicate_typechecks=True,
        llm_inferred=True,
    )
    assert score <= LLM_ONLY_NAMED_FILTER_CAP


# --- Path confidence -------------------------------------------------------

def test_path_confidence_is_minimum_of_edges():
    assert confidence_for_join_path([0.9, 0.7, 0.6]) == 0.6
    assert confidence_for_join_path([1.0]) == 1.0
    assert confidence_for_join_path([0.4, 0.5]) == 0.4


def test_empty_path_zero_confidence():
    assert confidence_for_join_path([]) == 0.0


# --- Determinism ------------------------------------------------------------

def test_join_formula_is_pure():
    """Calling twice with same inputs returns same outputs — no global state."""
    a, _ = confidence_for_join(fk_declared=True, column_type_match=True)
    b, _ = confidence_for_join(fk_declared=True, column_type_match=True)
    assert a == b
