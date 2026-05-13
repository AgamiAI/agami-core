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
# Structural / well-known column-name patterns (id / *_id / created_at /
# email / phone / ...). See shared/column-name-dictionary.md for the full
# list. A match by itself clears the default 0.7 threshold and triggers
# auto-approve — these are columns whose meaning is fixed by the name
# across every DB on Earth.
W_FIELD_STRUCTURAL_PATTERN: float = 0.50
LLM_ONLY_FIELD_CAP: float = 0.40  # field descriptions are lower-stakes than joins/metrics


# --- Structural pattern matching ------------------------------------------
#
# `match_structural_pattern(column_name)` returns a short pattern name
# (e.g., "created_at", "fk_id_suffix", "email_field") if the column matches
# one of the dictionary entries in shared/column-name-dictionary.md, else
# `None`. Matching is case-insensitive. Order matters — more-specific
# patterns are checked first so e.g. bare `id` doesn't match `*_id`.
#
# Keep this list in sync with shared/column-name-dictionary.md. Changes
# here should add a corresponding row in that doc and a test in
# tests/test_compute_confidence.py TestStructuralPatternMatch.

_EXACT_NAME_PATTERNS: dict[str, str] = {
    # Identity (bare)
    "id":   "id",
    "uuid": "uuid",
    "guid": "guid",
    # Timestamps
    "created_at":   "created_at",
    "updated_at":   "updated_at",
    "deleted_at":   "deleted_at",
    "inserted_at":  "inserted_at",
    "modified_at":  "modified_at",
    "dob":          "dob",
    "date_of_birth": "dob",
    "birth_date":   "dob",
    # Audit
    "created_by": "created_by",
    "updated_by": "updated_by",
    "deleted_by": "deleted_by",
    "version":    "version",
    "revision":   "revision",
    "etag":       "etag",
    # Lifecycle flags
    "enabled":   "lifecycle_flag",
    "disabled":  "lifecycle_flag",
    "active":    "lifecycle_flag",
    "inactive":  "lifecycle_flag",
    "archived":  "state_flag",
    "deleted":   "state_flag",
    "hidden":    "state_flag",
    "published": "state_flag",
    "draft":     "state_flag",
    # Contact / location
    "name":           "name_field",
    "full_name":      "name_field",
    "first_name":     "name_field",
    "last_name":      "name_field",
    "display_name":   "name_field",
    "email":          "email_field",
    "email_address":  "email_field",
    "phone":          "phone_field",
    "phone_number":   "phone_field",
    "mobile":         "phone_field",
    "mobile_number":  "phone_field",
    "telephone":      "phone_field",
    "address":        "address_field",
    "street":         "address_field",
    "street_address": "address_field",
    "address_line_1": "address_field",
    "address_line_2": "address_field",
    "city":           "city_field",
    "town":           "city_field",
    "state":          "state_field",
    "province":       "state_field",
    "region":         "state_field",
    "country":        "country_field",
    "country_code":   "country_field",
    "zip":            "postal_field",
    "zipcode":        "postal_field",
    "zip_code":       "postal_field",
    "postal_code":    "postal_field",
    "lat":            "latitude_field",
    "latitude":       "latitude_field",
    "lng":            "longitude_field",
    "lon":            "longitude_field",
    "long":           "longitude_field",
    "longitude":      "longitude_field",
    "url":            "url_field",
    "website":        "url_field",
    "link":           "url_field",
    "href":           "url_field",
    # Text / metadata
    "description": "description_field",
    "desc":        "description_field",
    "title":       "title_field",
    "headline":    "title_field",
    "notes":       "notes_field",
    "comments":    "notes_field",
    "remark":      "notes_field",
    "remarks":     "notes_field",
    "slug":        "slug_field",
    "handle":      "slug_field",
    "permalink":   "slug_field",
    "tag":         "tag_field",
    "tags":        "tag_field",
    "label":       "tag_field",
    "labels":      "tag_field",
    "metadata":    "metadata_field",
    "meta":        "metadata_field",
    "attributes":  "metadata_field",
    "properties":  "metadata_field",
    # Categorical
    "status":   "status_field",
    "type":     "type_field",
    "kind":     "type_field",
    "category": "category_field",
    "group":    "category_field",
    "priority": "priority_field",
    "severity": "priority_field",
    # Measure / currency
    "count":         "count_field",
    "quantity":      "count_field",
    "qty":           "count_field",
    "amount":        "amount_field",
    "total":         "total_field",
    "subtotal":      "total_field",
    "grand_total":   "total_field",
    "average":       "avg_field",
    "rate":          "rate_field",
    "currency":      "currency_field",
    "currency_code": "currency_field",
    "locale":        "locale_field",
    "language":      "locale_field",
    "lang":          "locale_field",
    "timezone":      "timezone_field",
    "tz":            "timezone_field",
}

# Suffix patterns: column ends with the suffix AND has at least one char before.
# Listed in priority order — first match wins. Keep more-specific suffixes
# earlier (e.g. `_id` before generic `_at`).
_SUFFIX_PATTERNS: list[tuple[str, str]] = [
    ("_id",        "fk_id_suffix"),
    ("_uuid",      "fk_uuid_suffix"),
    ("_guid",      "fk_guid_suffix"),
    # Time / event suffixes
    ("_at",        "event_at"),
    ("_date",      "event_date"),
    ("_time",      "event_time"),
    ("_timestamp", "event_timestamp"),
    ("_ts",        "event_ts"),
    ("_day",       "event_date"),
    ("_week",      "event_date"),
    ("_month",     "event_date"),
    ("_quarter",   "event_date"),
    ("_year",      "event_date"),
    # Audit
    ("_by",        "audit_by"),
    # Measure / count
    ("_count",     "count_field"),
    ("_amount",    "amount_field"),
    ("_total",     "total_field"),
    ("_avg",       "avg_field"),
    ("_mean",      "avg_field"),
    ("_min",       "min_max_field"),
    ("_max",       "min_max_field"),
    ("_rate",      "rate_field"),
    ("_pct",       "rate_field"),
    ("_percent",   "rate_field"),
    ("_ratio",     "rate_field"),
    ("_score",     "rate_field"),
    # Universal contact / location terms — prefixed forms catch
    # `Lead_email`, `Contact_phone`, `Billing_city`, etc.
    #
    # Order matters: more-specific composites BEFORE their parts.
    # `contact_email_address` should match email_field (its primary semantic),
    # not address_field — so `_email_address` is listed before `_address`.
    ("_email_address", "email_field"),
    ("_email",        "email_field"),
    ("_phone_number", "phone_field"),
    ("_phone",        "phone_field"),
    ("_mobile",       "phone_field"),
    ("_address",      "address_field"),
    ("_city",         "city_field"),
    ("_state",        "state_field"),
    ("_province",     "state_field"),
    ("_region",       "state_field"),
    ("_country",      "country_field"),
    ("_zip",          "postal_field"),
    ("_zipcode",      "postal_field"),
    ("_postal_code",  "postal_field"),
    ("_url",          "url_field"),
    ("_website",      "url_field"),
    ("_link",         "url_field"),
    # Names
    ("_name",         "name_field"),
    ("_first_name",   "name_field"),
    ("_last_name",    "name_field"),
    ("_full_name",    "name_field"),
    ("_display_name", "name_field"),
    # Universal categorical
    ("_status",       "status_field"),
    ("_type",         "type_field"),
    ("_kind",         "type_field"),
    ("_category",     "category_field"),
    ("_priority",     "priority_field"),
    ("_severity",     "priority_field"),
    # Ownership / team / group
    ("_owner",        "audit_by"),
    ("_assignee",     "audit_by"),
    ("_team",         "category_field"),
    ("_group",        "category_field"),
    ("_org",          "category_field"),
    ("_department",   "category_field"),
    # Description / notes
    ("_description",  "description_field"),
    ("_notes",        "notes_field"),
    ("_comments",     "notes_field"),
    ("_remarks",      "notes_field"),
    # Locale / config
    ("_locale",       "locale_field"),
    ("_language",     "locale_field"),
    ("_lang",         "locale_field"),
    ("_timezone",     "timezone_field"),
    ("_currency",     "currency_field"),
]

# Prefix patterns: column starts with the prefix AND has at least one char after.
_PREFIX_PATTERNS: list[tuple[str, str]] = [
    ("is_",     "is_flag"),
    ("has_",    "has_flag"),
    ("can_",    "can_flag"),
    ("should_", "should_flag"),
    ("id_",     "fk_id_prefix"),
]


def match_structural_pattern(column_name: str) -> str | None:
    """Return the short pattern name (e.g., 'created_at', 'fk_id_suffix',
    'email_field') if `column_name` matches one of the dictionary entries
    in shared/column-name-dictionary.md, else None.

    Matching is case-insensitive. Exact-name matches win over suffix /
    prefix matches; suffix matches win over prefix matches when both apply.
    """
    if not column_name:
        return None
    lower = column_name.strip().lower()
    if not lower:
        return None
    # Exact name first.
    if lower in _EXACT_NAME_PATTERNS:
        return _EXACT_NAME_PATTERNS[lower]
    # Suffix matches (require non-empty prefix before the underscore).
    for suffix, name in _SUFFIX_PATTERNS:
        if lower.endswith(suffix) and len(lower) > len(suffix):
            return name
    # Prefix matches (require non-empty suffix after the underscore).
    for prefix, name in _PREFIX_PATTERNS:
        if lower.startswith(prefix) and len(lower) > len(prefix):
            return name
    return None


def confidence_for_field_description(
    *,
    dba_column_comment: bool = False,
    business_term_match: bool = False,
    enum_like_distribution: bool = False,
    llm_inferred: bool = False,
    structural_pattern_match: str | None = None,
) -> tuple[float, dict]:
    """Confidence for a field-level description / enum / type mapping.

    Three paths to auto-approve (≥ 0.70):
      1. `dba_column_comment=True` — DBA-authored comment lifts to 0.70+ alone.
      2. `structural_pattern_match` is non-None — the column name matches a
         well-known dictionary pattern (id / *_id / created_at / email / ...);
         the meaning is fixed by the name and reviewing is busywork.
      3. Multiple weaker signals stack (business_term + enum_like + ...).

    LLM-only descriptions stay capped at LLM_ONLY_FIELD_CAP unless one of the
    stronger signals above is present.
    """
    score = 0.0
    if dba_column_comment:
        score += W_FIELD_DBA_COLUMN_COMMENT
    if business_term_match:
        score += W_FIELD_BUSINESS_TERM_DICT
    if enum_like_distribution:
        score += W_FIELD_ENUM_LIKE_DISTRIBUTION
    if structural_pattern_match:
        score += W_FIELD_STRUCTURAL_PATTERN

    # LLM-only cap: applies when neither DBA comment nor a structural pattern
    # is present. Structural patterns are mechanical (name-based), so an
    # LLM-generated description on a structurally-matched column is still
    # auto-approve-worthy — the pattern carries the trust, not the prose.
    if llm_inferred and not dba_column_comment and not structural_pattern_match:
        score = min(score, LLM_ONLY_FIELD_CAP)

    score = max(0.0, min(1.0, score))

    breakdown = {
        "dba_column_comment": dba_column_comment,
        "business_term_match": business_term_match,
        "enum_like_distribution": enum_like_distribution,
        "llm_inferred": llm_inferred,
        "structural_pattern_match": structural_pattern_match,
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
