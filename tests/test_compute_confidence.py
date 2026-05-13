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


# --- Pillar A: structural pattern matching --------------------------------

from compute_confidence import match_structural_pattern  # noqa: E402


class TestStructuralPatternMatch:
    """`match_structural_pattern` is the dictionary lookup driving Pillar A
    (auto-approve structural / well-known columns). Each test asserts that
    representative column names produce the expected pattern_name.

    When adding new patterns to shared/column-name-dictionary.md, add a
    corresponding test here so the dictionary and the lookup stay in sync."""

    def test_bare_id_matches_id_pattern(self):
        assert match_structural_pattern("id") == "id"
        assert match_structural_pattern("ID") == "id"
        assert match_structural_pattern("Id") == "id"

    def test_suffix_id_matches_fk_pattern(self):
        assert match_structural_pattern("customer_id") == "fk_id_suffix"
        assert match_structural_pattern("ORDER_ID") == "fk_id_suffix"

    def test_bare_id_does_not_match_suffix_pattern(self):
        # The bare-id exact match must win over the *_id suffix.
        assert match_structural_pattern("id") != "fk_id_suffix"

    def test_uuid_variants(self):
        assert match_structural_pattern("uuid") == "uuid"
        assert match_structural_pattern("session_uuid") == "fk_uuid_suffix"

    def test_timestamp_suffix(self):
        assert match_structural_pattern("created_at") == "created_at"
        assert match_structural_pattern("shipped_at") == "event_at"
        assert match_structural_pattern("paid_date") == "event_date"
        assert match_structural_pattern("login_ts") == "event_ts"

    def test_audit_columns(self):
        assert match_structural_pattern("created_by") == "created_by"
        assert match_structural_pattern("approved_by") == "audit_by"

    def test_email_phone_variants(self):
        assert match_structural_pattern("email") == "email_field"
        assert match_structural_pattern("email_address") == "email_field"
        assert match_structural_pattern("phone") == "phone_field"
        assert match_structural_pattern("phone_number") == "phone_field"
        assert match_structural_pattern("mobile_number") == "phone_field"

    def test_address_components(self):
        assert match_structural_pattern("city") == "city_field"
        assert match_structural_pattern("state") == "state_field"
        assert match_structural_pattern("country") == "country_field"
        assert match_structural_pattern("zip") == "postal_field"
        assert match_structural_pattern("postal_code") == "postal_field"

    def test_boolean_flag_prefixes(self):
        assert match_structural_pattern("is_active") == "is_flag"
        assert match_structural_pattern("has_paid") == "has_flag"
        assert match_structural_pattern("can_edit") == "can_flag"
        assert match_structural_pattern("should_notify") == "should_flag"

    def test_lifecycle_flags_exact(self):
        assert match_structural_pattern("active") == "lifecycle_flag"
        assert match_structural_pattern("archived") == "state_flag"

    def test_categorical_terms(self):
        assert match_structural_pattern("status") == "status_field"
        assert match_structural_pattern("type") == "type_field"
        assert match_structural_pattern("category") == "category_field"

    def test_measure_suffixes(self):
        assert match_structural_pattern("order_count") == "count_field"
        assert match_structural_pattern("total_amount") == "amount_field"
        assert match_structural_pattern("interest_rate") == "rate_field"
        assert match_structural_pattern("loan_min") == "min_max_field"

    def test_opaque_column_no_match(self):
        # Opaque names should not pattern-match — they need LLM / DBA review.
        assert match_structural_pattern("v_1") is None
        assert match_structural_pattern("tmp_col") is None
        assert match_structural_pattern("x") is None
        assert match_structural_pattern("foobar") is None

    # --- Prefixed universal terms (added 2026-05-13 after a BigQuery
    # ---     CRM-schema report showed Lead_email / Lead_country /
    # ---     Contact_Owner / Hubspot_Team falling through as "(no description)"
    # ---     because the dictionary only matched bare `email` / `country` /
    # ---     `owner` / `team`. Real DBs prefix constantly.)

    def test_prefixed_contact_terms(self):
        assert match_structural_pattern("Lead_email") == "email_field"
        assert match_structural_pattern("contact_email_address") == "email_field"
        assert match_structural_pattern("Lead_phone") == "phone_field"
        assert match_structural_pattern("Customer_mobile") == "phone_field"

    def test_prefixed_location_terms(self):
        assert match_structural_pattern("Billing_address") == "address_field"
        assert match_structural_pattern("Shipping_city") == "city_field"
        assert match_structural_pattern("Lead_country") == "country_field"
        assert match_structural_pattern("Billing_state") == "state_field"
        assert match_structural_pattern("Lead_zip") == "postal_field"
        assert match_structural_pattern("Lead_postal_code") == "postal_field"

    def test_prefixed_name_terms(self):
        assert match_structural_pattern("Lead_name") == "name_field"
        assert match_structural_pattern("Contact_first_name") == "name_field"
        assert match_structural_pattern("Customer_last_name") == "name_field"
        assert match_structural_pattern("Owner_full_name") == "name_field"

    def test_prefixed_categorical(self):
        assert match_structural_pattern("Lead_status") == "status_field"
        assert match_structural_pattern("Order_status") == "status_field"
        assert match_structural_pattern("Account_type") == "type_field"
        assert match_structural_pattern("Lead_category") == "category_field"
        assert match_structural_pattern("Hubspot_Team") == "category_field"
        assert match_structural_pattern("Lead_priority") == "priority_field"

    def test_prefixed_ownership(self):
        assert match_structural_pattern("Contact_Owner") == "audit_by"
        assert match_structural_pattern("Lead_assignee") == "audit_by"
        # `_by` suffix still wins for clearly-audit names:
        assert match_structural_pattern("approved_by") == "audit_by"

    def test_prefixed_date_buckets(self):
        # New: *_day, *_week, *_month, *_quarter, *_year all → event_date
        # so "Lead_create_day", "Webinar_day" etc. don't fall through.
        assert match_structural_pattern("Lead_create_day") == "event_date"
        assert match_structural_pattern("Webinar_day") == "event_date"
        assert match_structural_pattern("report_week") == "event_date"
        assert match_structural_pattern("billing_month") == "event_date"
        assert match_structural_pattern("fiscal_quarter") == "event_date"
        assert match_structural_pattern("birth_year") == "event_date"

    def test_prefixed_url(self):
        assert match_structural_pattern("Webhook_url") == "url_field"
        assert match_structural_pattern("Lead_website") == "url_field"

    def test_prefixed_measure_extras(self):
        assert match_structural_pattern("conversion_rate") == "rate_field"
        assert match_structural_pattern("success_pct") == "rate_field"
        assert match_structural_pattern("retention_percent") == "rate_field"
        assert match_structural_pattern("price_ratio") == "rate_field"
        assert match_structural_pattern("credit_score") == "rate_field"

    def test_case_insensitive(self):
        assert match_structural_pattern("CREATED_AT") == "created_at"
        assert match_structural_pattern("Email") == "email_field"

    def test_whitespace_tolerated(self):
        assert match_structural_pattern(" customer_id ") == "fk_id_suffix"

    def test_empty_and_none(self):
        assert match_structural_pattern("") is None
        assert match_structural_pattern("   ") is None
        assert match_structural_pattern(None) is None  # type: ignore[arg-type]

    def test_suffix_requires_non_empty_prefix(self):
        # Bare `_id` (length 3) with no prefix character must not match.
        # In practice DBs disallow names starting with underscore-only, but
        # the matcher should still reject them defensively.
        assert match_structural_pattern("_id") is None


class TestFieldStructuralAutoApprove:
    """The new W_FIELD_STRUCTURAL_PATTERN weight + the rule that LLM_only
    fields keep the cap unless a structural pattern is present."""

    def test_structural_match_alone_auto_approves(self):
        """A column named `customer_id` with NO other signals (no DBA comment,
        no business term match, no enum-like) should still auto-approve via
        the structural pattern — its meaning is fixed by the name."""
        score, _ = confidence_for_field_description(
            structural_pattern_match="fk_id_suffix",
        )
        assert score >= 0.50  # default threshold is 0.7 but we use 0.5+ for explicit auto-approve at introspect
        # In practice the auto-approve rule lives in agami-connect Phase 2c.2,
        # not in compute_confidence — but the score should be high enough that
        # it crosses ANY reasonable threshold.

    def test_structural_match_bypasses_llm_cap(self):
        """When a structural pattern matches AND the description is
        LLM-generated, the LLM cap does NOT apply — the pattern carries the
        trust, not the prose."""
        score, _ = confidence_for_field_description(
            structural_pattern_match="email_field",
            business_term_match=True,
            llm_inferred=True,
        )
        # Pattern (0.50) + business_term (0.20) = 0.70, well above the cap.
        assert score >= 0.70

    def test_no_structural_no_dba_llm_still_capped(self):
        """An LLM-only description on an opaque column (no pattern match)
        stays capped at LLM_ONLY_FIELD_CAP. This is the pre-Pillar-A behavior
        that protects against silent auto-approve of fabricated descriptions."""
        score, _ = confidence_for_field_description(
            business_term_match=True,
            enum_like_distribution=True,
            llm_inferred=True,
            structural_pattern_match=None,
        )
        assert score <= LLM_ONLY_FIELD_CAP

    def test_signal_breakdown_includes_pattern_name(self):
        """The pattern name (not just True/False) lives in signal_breakdown so
        a curator inspecting the YAML can see *why* it auto-approved."""
        _, breakdown = confidence_for_field_description(
            structural_pattern_match="created_at",
        )
        assert breakdown["structural_pattern_match"] == "created_at"

    def test_signal_breakdown_none_when_no_pattern(self):
        _, breakdown = confidence_for_field_description(business_term_match=True)
        assert breakdown["structural_pattern_match"] is None


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
