"""Shared model-assembly + on-disk writer for building the semantic model.

These are the dialect-agnostic, source-agnostic pieces used by the introspection
engine (`introspect.py`): infer grain/cardinality, derive column_groups on deep
tables, flag sensitive columns, propose a subject-area split, extract cross-area
edges, and write the canonical on-disk tree. Kept here (not in introspect.py) so
the assembly logic is independently testable and reusable.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Optional

import yaml

from .models import (
    DEEP_TABLE_COLUMN_THRESHOLD,
    Column,
    CrossSubjectAreaRelationship,
    Datasource,
    Relationship,
    SubjectArea,
    Table,
    TableRef,
    bare_name,
)

# Strongly-PII column-name patterns — sensitive regardless of table.
STRONG_PII_RE = re.compile(
    r"(dob|birth|phone|mobile|email|address|aadhaar|aadhar|\bpan\b|\bssn\b|passport|"
    r"pincode|\bzip\b|account_?no|card_?no|salary|income)",
    re.IGNORECASE,
)
# Weakly-PII patterns — only sensitive inside a PII-ish table (a "name" column on
# a product table is not PII; on a person table it is).
WEAK_PII_RE = re.compile(r"(\bname\b|first_?name|last_?name|full_?name|surname)", re.IGNORECASE)
# Broader "might be PII" tokens for the SUSPECTED tier — a review aid only, never auto-flags.
# General privacy concepts (not a customer's schema): identity, contact, location, demographics.
_EXTRA_PII_RE = re.compile(
    r"(gender|\bsex\b|ip_?address|\bip\b|latitude|longitude|\bgeo\b|national_?id|tax_?id|"
    r"licen[cs]e|user_?name|nationality|ethnicit|religion|marital|emergency_?contact|"
    r"next_?of_kin|date_?of_?birth|maiden)", re.IGNORECASE)


def suspected_pii(column_name: str) -> bool:
    """A BROADER 'might be PII' heuristic than `detect_sensitive` — surfaces columns the strict
    flag may have MISSED (e.g. `first_name` in a non-PII-named table like `sys_user`, which the
    table-gated weak rule skips), so a reviewer can confirm. A review aid; never auto-marks
    sensitive. General privacy vocabulary, not vendor-specific."""
    return bool(STRONG_PII_RE.search(column_name) or WEAK_PII_RE.search(column_name)
                or _EXTRA_PII_RE.search(column_name))
# Back-compat alias (some callers import SENSITIVE_RE).
SENSITIVE_RE = STRONG_PII_RE

# sizing thresholds mirror validator.SIZING_WARN
SINGLE_AREA_MAX = 24


# Numeric columns whose NAME indicates a monetary value, so curation can offer a currency
# `unit` without callers hand-rolling (and mis-rolling) a regex. Names are split into whole
# words on `_`, other punctuation and camelCase, so a bare `count` never matches inside
# `discount`: `discount_amount` / `member_discount` ARE money; `order_count` / `discount_rate`
# are not.
_MONEY_WORDS = frozenset({
    "amount", "amt", "price", "cost", "fee", "revenue", "sales", "salary", "wage", "income",
    "payment", "charge", "balance", "deposit", "withdrawal", "refund", "discount", "subtotal",
    "total", "spend", "spent", "budget", "invoice", "mrr", "arr", "gmv", "ltv", "aov", "paid",
    "due", "owed", "credit", "debit",
})
# Words that name a currency outright. ISO codes beyond these count only after the first word
# (`gross_usd`, `fare_try`), since a few double as English words (`try_count`, `php_version`).
_CURRENCY_WORDS = {"usd": "USD", "dollar": "USD", "eur": "EUR", "euro": "EUR", "gbp": "GBP",
                   "inr": "INR", "rupee": "INR", "jpy": "JPY", "yen": "JPY"}
# Words that flip a money-ish name back to NON-money: a rate / count / id / key / score, or a
# non-money unit (minutes, hours, weight, points). Keys matter in warehouses: `invoice_key` and
# `payment_key` name join keys, not money. `total_minutes` and `points_balance` are not money.
_NOT_MONEY_WORDS = frozenset({
    "rate", "pct", "percent", "percentage", "ratio", "count", "cnt", "qty", "quantity", "num",
    "number", "nbr", "id", "key", "sk", "fk", "flag", "year", "age", "day", "month", "score",
    "rank", "code", "status", "duration", "weight", "kg", "lb", "gram", "point", "pts", "units",
})
# Time words exclude only in the plural: `total_seconds` is a duration, `second_payment_amount`
# is the second payment.
_NOT_MONEY_PLURALS = frozenset({"seconds", "secs", "minutes", "mins", "hours", "hrs"})
_SCALE_WORDS = {"thousand": "thousands", "million": "millions", "billion": "billions",
                "lakh": "lakhs", "crore": "crores"}
_PERIOD_WORDS = frozenset({"hour", "day", "week", "month", "quarter", "year"})
_PERIOD_ADJECTIVES = {"hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month",
                      "quarterly": "quarter", "yearly": "year", "annual": "year"}


def name_words(column_name: str) -> list[str]:
    """`revenueUSDMillions` / `revenue_usd_millions` -> ["revenue", "usd", "millions"]."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", column_name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return [w for w in re.split(r"[^A-Za-z0-9]+", s.lower()) if w]


def _in(word: str, vocab) -> Optional[str]:
    """The vocabulary entry `word` is, allowing a plural `s` (`amounts`, `minutes`)."""
    if word in vocab:
        return word
    if word.endswith("s") and word[:-1] in vocab:
        return word[:-1]
    return None


def _per_period(words: list[str]) -> tuple[list[str], Optional[str]]:
    """Drop a `per <x>` pair (`revenue_per_day`, `cost_per_kg`) — it says what the money is
    per, not that it isn't money — and return the period when `<x>` is one."""
    out, period, i = [], None, 0
    while i < len(words):
        if words[i] == "per" and i + 1 < len(words):
            period = period or _in(words[i + 1], _PERIOD_WORDS)
            i += 2
            continue
        out.append(words[i])
        i += 1
    return out, period


def name_currency(column_name: str) -> Optional[str]:
    """The ISO code a column's name states (`gross_usd`, `fx_dollar`, `amountEUR`), or None."""
    from .units import CURRENCY_SYMBOLS

    for i, w in enumerate(name_words(column_name)):
        hit = _in(w, _CURRENCY_WORDS)
        if hit:
            return _CURRENCY_WORDS[hit]
        if i and w.upper() in CURRENCY_SYMBOLS:
            return w.upper()
    return None


def detect_money_column(column_name: str) -> bool:
    """A column whose name looks monetary (amount/price/revenue/discount/… or a currency such as
    `_usd`) and carries no rate/count/id/score or non-money unit word — so `discount_amount` and
    `balance_usd` are money while `discount_rate`, `order_count`, `total_minutes`, `credit_hours`
    and `points_balance` are not. Name-only; the caller restricts this to numeric columns."""
    words, _ = _per_period(name_words(column_name))
    if any(_in(w, _NOT_MONEY_WORDS) or w in _NOT_MONEY_PLURALS for w in words):
        return False
    return name_currency(column_name) is not None or any(_in(w, _MONEY_WORDS) for w in words)


def suggest_unit(column_name: str) -> Optional[str]:
    """The unit a money column's name spells out, in the units.py grammar:
    `revenue_usd_millions` -> `USD_millions`, `cost_usd_per_day` -> `USD/day`. None when the
    name states no currency (the caller then asks which currency it is)."""
    code = name_currency(column_name)
    if not code or not detect_money_column(column_name):
        return None
    words, period = _per_period(name_words(column_name))
    period = period or next((_PERIOD_ADJECTIVES[w] for w in words if w in _PERIOD_ADJECTIVES), None)
    scale = next((_SCALE_WORDS[_in(w, _SCALE_WORDS)] for w in words if _in(w, _SCALE_WORDS)), None)
    return code + (f"_{scale}" if scale else "") + (f"/{period}" if period else "")


# --- aggregation class (the Phase-1 column-intrinsic measure semantics) ------

# Only these column types can be MEASURES at all; everything else (string, date,
# timestamp, boolean, json, …) is a grouping dimension.
_NUMERIC_TYPES = frozenset({"integer", "decimal", "float"})

# Identifier / code / calendar / flag tokens → the column is a DIMENSION, never a measure.
# Deliberately conservative (omit ambiguous num/number/day/week/month) — a miss falls to a
# safe class downstream, a false dimension would wrongly block a real measure.
_DIMENSION_RE = re.compile(
    r"(^|_)(id|guid|uuid|key|code|cd|no|zip|zipcode|pincode|pin|year|fiscal|quarter|"
    r"status|state|type|category|flag|version|is|has)s?(_|$)",
    re.IGNORECASE,
)
# Rate / ratio / per-unit / index tokens → AVERAGEABLE (AVG/MIN/MAX ok, SUM is meaningless).
# Checked BEFORE additive so `avg_balance`, `discount_rate`, `cost_per_unit`, `unit_price`
# resolve to averageable rather than being summed.
_AVERAGEABLE_RE = re.compile(
    r"(^|_)(rate|ratio|pct|percent|percentage|avg|average|mean|median|per|price|"
    r"score|rating|temperature|temp|index|margin|share|utilization|util|occupancy)s?(_|$)",
    re.IGNORECASE,
)
# Quantity / money / volume tokens → ADDITIVE (SUM is meaningful). Includes semi-additive
# STOCKS (balance, inventory, headcount) — they ARE summable; the time exception is declared
# on the metric as `non_additive_dimensions` (Phase 2 / #3), not here.
_ADDITIVE_RE = re.compile(
    r"(^|_)(amount|amt|total|subtotal|sum|qty|quantity|count|cnt|revenue|sales|cost|"
    r"spend|spent|value|volume|gmv|charge|fee|tax|discount|paid|due|gross|net|"
    r"profit|income|expense|balance|deposit|withdrawal|refund|credit|debit|payment|"
    r"inventory|stock|headcount|distance|weight|duration|bytes|size)s?(_|$)"
    # a bare `units` is often a per-entity reading (`reading_units`); only a flow settles it
    r"|(^|_)units_(sold|shipped|ordered|delivered|received|returned|purchased|produced)(_|$)"
    r"|(^|_)(sold|shipped|ordered|delivered|received|returned|purchased|produced)_units(_|$)",
    re.IGNORECASE,
)


def classify_aggregation(column_name: str, column_type: str, is_key: bool = False) -> str:
    """Heuristic column-intrinsic aggregation class — one of additive / averageable /
    dimension / unknown (see models.Aggregation). Name + type only; advisory, refined by
    the curator, and `unknown` is never enforced against. Order matters: keys and
    non-numeric types are dimensions; rate-ish names are averageable; money/quantity names
    are additive; anything else stays `unknown` (safe — no enforcement)."""
    if is_key:
        return "dimension"
    if column_type not in _NUMERIC_TYPES:
        return "dimension"
    # money detection reads camelCase / kebab words, so these checks must too, or `unitPrice`
    # would skip the averageable test and be called additive
    spelled = (column_name, "_".join(name_words(column_name)))
    if any(_DIMENSION_RE.search(n) for n in spelled):
        return "dimension"
    if any(_AVERAGEABLE_RE.search(n) for n in spelled):
        return "averageable"
    if detect_money_column(column_name) or _ADDITIVE_RE.search(column_name):
        return "additive"
    return "unknown"


# Universal coded-FORMAT columns whose meaning is an objective standard, not a guess — recognized
# from NAME signal + VALUE shape together (both required, so it's confident, never inferred from a
# name alone). Deterministic; fills a description the LLM would otherwise leave `ai_unknown`, on
# ANY database (no platform assumptions). General world facts, not vendor-specific.
_CCY_NAME_RE = re.compile(r"(^|_)(currency|ccy|curr)(_|$)", re.IGNORECASE)
_COUNTRY_NAME_RE = re.compile(r"(^|_)(country|nation)(_code)?(_|$)", re.IGNORECASE)
_LOCALE_NAME_RE = re.compile(r"(^|_)(locale|lang|language)(_|$)", re.IGNORECASE)
_TZ_NAME_RE = re.compile(r"(^|_)(timezone|tz|time_?zone)(_|$)", re.IGNORECASE)
_3UP_RE = re.compile(r"^[A-Z]{3}$")
_2UP_RE = re.compile(r"^[A-Z]{2}$")
_IANA_TZ_RE = re.compile(r"^[A-Za-z]+/[A-Za-z_]+")
_LOCALE_VAL_RE = re.compile(r"^[a-z]{2}([_-][A-Za-z]{2,4})?$")


def canonical_description(column_name: str, values: list) -> Optional[str]:
    """A deterministic description for a UNIVERSAL coded format (currency / country / timezone /
    locale), recognized from the column NAME plus the SHAPE of its sampled values — both required,
    so it never guesses from a name alone. Returns None when there's no confident match. General
    world standards, applicable to any database."""
    vals = [str(v).strip() for v in values if str(v).strip() != ""][:50]
    if not vals:
        return None

    def most(rx) -> bool:  # ≥80% of sampled values fit the shape (tolerate a few odd rows)
        return sum(1 for v in vals if rx.match(v)) >= max(1, int(0.8 * len(vals)))

    if _CCY_NAME_RE.search(column_name) and most(_3UP_RE):
        return "ISO 4217 currency code (3-letter, e.g. USD, EUR, INR)."
    if _COUNTRY_NAME_RE.search(column_name) and most(_2UP_RE):
        return "ISO 3166-1 alpha-2 country code (2-letter, e.g. US, IN, GB)."
    if _TZ_NAME_RE.search(column_name) and most(_IANA_TZ_RE):
        return "IANA time-zone identifier (e.g. America/New_York, Asia/Kolkata)."
    if _LOCALE_NAME_RE.search(column_name) and most(_LOCALE_VAL_RE):
        return "Language / locale code (e.g. en, en_US, pt-BR)."
    return None


def detect_sensitive(table_name: str, column_name: str) -> bool:
    """Strongly-PII column names (email/phone/dob/ssn/address/…) are sensitive
    regardless of table. Weakly-PII names (name/first_name/…) are sensitive only
    inside a PII-ish table, to avoid flagging e.g. a product `name`."""
    if STRONG_PII_RE.search(column_name):
        return True
    table_pii = table_name.upper() == "PII" or bool(STRONG_PII_RE.search(table_name)) \
        or bool(WEAK_PII_RE.search(table_name))
    return bool(WEAK_PII_RE.search(column_name)) and table_pii


# Role groups — assigned from STRUCTURAL column signals (PK, foreign_key, choice_field,
# aggregation class, type) plus a couple of universal name patterns. General, not vendor-
# specific. A wide table's columns are mostly references / coded values / flags / measures /
# timestamps, so naming those roles collapses the long tail of single-prefix columns that the
# old prefix-only grouping dumped into `misc`. Order = priority; each column lands in exactly
# ONE group (the explorer relies on one-group-per-column). Roles that match nothing fall
# through to prefix-token grouping, then to `misc`.
_FLAG_COL_RE = re.compile(r"^(is_|has_).+|.+_flag$", re.IGNORECASE)
# Audit / lifecycle bookkeeping: who/when a row was touched. Suffix-anchored on `_at/_on/_by`
# (and the created/updated/modified/deleted prefixes) so it catches `created_at`/`opened_by`/
# `closed_on` WITHOUT grabbing business dates like `due_date`/`order_date` (those fall to the
# `dates` role instead).
_AUDIT_COL_RE = re.compile(r"_(at|on|by)$|^(created|updated|modified|deleted)(_|$)", re.IGNORECASE)

# One-line gloss per RECOGNIZED role group. Prefix-token groups (and any unknown key) get a
# generated line from `column_group_descriptions`. No vendor specifics — these describe a role.
_ROLE_GROUP_DESCRIPTIONS: dict[str, str] = {
    "identity": "Primary keys and unique identifiers for this table.",
    "references": "Foreign-key columns that link to other tables.",
    "codes": "Coded / enumerated values drawn from a fixed set of choices.",
    "flags": "Boolean indicator columns (true/false conditions).",
    "audit": "Bookkeeping of when a row was created or changed, and by whom.",
    "measures": "Numeric columns that can be aggregated (summed or averaged).",
    "dates": "Date / time columns other than audit timestamps.",
    "misc": "Columns with no shared prefix or recognized role.",
}


def _role_of(c: Column, reference_columns: set[str]) -> Optional[str]:
    """The role group for a column from its structural signals, or None to defer to prefix
    grouping. First match wins (so a coded FK files under `references`, a boolean flag under
    `flags`, etc.) — keeps every column in exactly one group. `reference_columns` (column names
    that are the FROM side of a join) lets the caller mark references even though the join lives
    on a Relationship, not on `column.foreign_key` — which the introspection pipeline never sets."""
    if c.primary_key or c.name.upper() == "ID":
        return "identity"
    if c.foreign_key is not None or c.name in reference_columns:
        return "references"
    if c.choice_field:
        return "codes"
    if c.type == "boolean" or _FLAG_COL_RE.match(c.name):
        return "flags"
    if _AUDIT_COL_RE.search(c.name):
        return "audit"
    if c.aggregation in ("additive", "averageable"):
        return "measures"
    if c.type in ("timestamp", "date"):
        return "dates"
    return None


def derive_column_groups(
    columns: list[Column], *, reference_columns: Optional[set[str]] = None
) -> dict[str, list[str]]:
    """Group deep-table columns ROLE-FIRST (references / codes / flags / audit / measures /
    dates / identity from structural signals), then by name prefix for whatever's left, then
    fold remaining single-prefix columns into `misc`. Every column lands in exactly one group
    (no orphans — the validator enforces this on deep tables). General, not vendor-specific.

    `reference_columns` (optional) names the columns that are the FROM side of a join — passed
    in once relationships are known so FK columns group under `references` instead of scattering
    through `misc` (joins live on Relationship objects, not on `column.foreign_key`)."""
    ref_cols = reference_columns or set()
    role_groups: dict[str, list[str]] = defaultdict(list)
    prefix_groups: dict[str, list[str]] = defaultdict(list)
    for c in columns:
        role = _role_of(c, ref_cols)
        if role is not None:
            role_groups[role].append(c.name)
        else:
            prefix_groups[c.name.split("_")[0].lower()].append(c.name)
    final: dict[str, list[str]] = dict(role_groups)
    misc: list[str] = []
    for g, cols in prefix_groups.items():
        if len(cols) == 1:
            misc.extend(cols)
        else:
            final[g] = cols
    if misc:
        final.setdefault("misc", []).extend(misc)
    return final


def column_group_descriptions(groups: dict[str, list[str]]) -> dict[str, str]:
    """One-line gloss per group name: recognized role groups get a fixed description, prefix-
    token groups get a generated 'Columns named <token>…' line. General; the result is stored
    on the table so the explorer/MCP can explain a group instead of showing a bare key."""
    out: dict[str, str] = {}
    for g in groups:
        out[g] = _ROLE_GROUP_DESCRIPTIONS.get(g) or f"Columns named {g}* (grouped by prefix)."
    return out


def maybe_column_groups(
    columns: list[Column], *, reference_columns: Optional[set[str]] = None
) -> dict[str, list[str]]:
    """column_groups only on deep tables; narrow tables get none."""
    if len(columns) >= DEEP_TABLE_COLUMN_THRESHOLD:
        return derive_column_groups(columns, reference_columns=reference_columns)
    return {}


def infer_cardinality(
    from_table: Optional[Table],
    to_table: Optional[Table],
    from_cols: list[str],
    to_cols: list[str],
) -> str:
    """Infer join cardinality from each side's keys: its grain (primary key) and unique keys.

    A side is "one" when the join's columns are exactly one of its keys; a multi-column key counts
    only when the join uses all of it. A nullable unique column still counts: NULL never joins.
    Tables, not names, so two same-named tables in two schemas don't lend each other a key.

    both sides keyed  -> one_to_one
    to side keyed     -> many_to_one
    from side keyed   -> one_to_many
    neither           -> many_to_one (default; review)
    """
    from_one = from_table is not None and from_table.unique_on(from_cols)
    to_one = to_table is not None and to_table.unique_on(to_cols)
    if from_one and to_one:
        return "one_to_one"
    if from_one:
        return "one_to_many"
    return "many_to_one"


# ---------------------------------------------------------------------------
# Subject-area proposal (prefix-family clustering; one table per area)
# ---------------------------------------------------------------------------


def _singularize(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def cluster_by_family(names: list[str]) -> dict[str, str]:
    """Map each table name -> an area key, grouping by first-token (singularized)
    then merging prefix-families (one key being a prefix of another)."""
    raw_key: dict[str, str] = {}
    for name in names:
        bare = bare_name(name)
        token = _singularize(re.split(r"[_\s]", bare)[0].lower())
        raw_key[name] = token or "misc"

    canonical: dict[str, str] = {}
    for k in sorted(set(raw_key.values()), key=len):
        target = k
        for root in canonical.values():
            if k.startswith(root) or root.startswith(k):
                target = min(root, k, key=len)
                break
        canonical[k] = target

    def resolve(k: str) -> str:
        seen: set[str] = set()
        while canonical.get(k, k) != k and k not in seen:
            seen.add(k)
            k = canonical[k]
        return k

    return {name: resolve(raw_key[name]) for name in names}


def make_table_ref(conn: str, table: Table) -> TableRef:
    expose = list(table.column_groups.keys()) if table.column_groups else None
    return TableRef(
        storage_connection=conn,
        schema=table.schema_name,
        table=table.name,
        expose_column_groups=expose,
    )


_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _area_key(schema: str) -> str:
    """A filesystem-safe area name from a schema name. Windows reserves its device names at every
    path level, so a schema called `con` or `aux` gets a suffix rather than a directory nobody can open."""
    key = re.sub(r"[^a-z0-9_]+", "_", schema.lower()).strip("_") or "misc"
    return f"{key}_" if key in _WINDOWS_RESERVED else key


def _rel_in_area(r: Relationship, keys: set, bare: set) -> bool:
    """Is this relationship internal to an area? Match endpoints by (schema, table) when the
    relationship carries schemas — so a join from billing.products and one from crm.products
    don't both match an area just because the bare name `products` is present. Schemaless
    relationships (SQLite / legacy) fall back to bare-name membership."""
    def here(table: str, schema) -> bool:
        return (schema, table) in keys if schema is not None else table in bare
    return (here(bare_name(r.from_table), r.from_schema)
            and here(bare_name(r.to_table), r.to_schema))


def make_area(name: str, tables: list[Table], rels: list[Relationship], conn: str) -> SubjectArea:
    table_names = {t.name for t in tables}
    keys = {(t.schema_name, t.name) for t in tables}
    area_rels = [r for r in rels if _rel_in_area(r, keys, table_names)]
    return SubjectArea(
        name=name,
        description=f"Auto-proposed subject area covering: {', '.join(sorted(table_names))}.",
        tables=[make_table_ref(conn, t) for t in tables],
        tables_defined=tables,
        relationships=area_rels,
    )


def propose_subject_areas(
    tables: list[Table], rels: list[Relationship], conn: str, profile: str
) -> tuple[list[SubjectArea], list[str]]:
    """Return (areas, notes).

    A DB spanning **2+ schemas** is split **one area per schema** — the schemas are the
    natural domains, and (critically) this keeps same-named tables in different schemas
    (e.g. `billing.products` vs `crm.products`) in separate area dirs so neither is lost to
    a bare-name write collision. A single-schema DB keeps the old behavior: one area when
    small, else prefix-family clustering.
    """
    notes: list[str] = []
    schemas = sorted({t.schema_name for t in tables if t.schema_name})
    if len(schemas) >= 2:
        by_area: dict[str, list[Table]] = defaultdict(list)
        for t in tables:
            by_area[_area_key(t.schema_name) if t.schema_name else profile.lower()].append(t)
        areas = [make_area(a, members, rels, conn) for a, members in sorted(by_area.items())]
        notes.append(
            f"{len(tables)} tables across {len(schemas)} schemas -> {len(areas)} subject areas "
            "(one per schema); cross-schema joins become cross_subject_area_relationships."
        )
        return areas, notes
    if len(tables) <= SINGLE_AREA_MAX:
        notes.append(f"{len(tables)} tables -> single subject area {profile.lower()!r}")
        return [make_area(profile.lower(), tables, rels, conn)], notes
    mapping = cluster_by_family([t.name for t in tables])
    by_area = defaultdict(list)
    for t in tables:
        by_area[mapping[t.name]].append(t)
    areas = [make_area(a, members, rels, conn) for a, members in sorted(by_area.items())]
    notes.append(
        f"{len(tables)} tables -> {len(areas)} areas by prefix-family (one owner each); "
        "cross-area joins become cross_subject_area_relationships. PROPOSAL — review."
    )
    return areas, notes


def extract_cross_area_relationships(
    areas: list[SubjectArea], rels: list[Relationship]
) -> list[CrossSubjectAreaRelationship]:
    # Key by (schema, name) so two same-named tables in different schemas resolve to their
    # own area; keep a bare-name fallback for schemaless rels / tables.
    area_of: dict[tuple, str] = {}
    bare_of: dict[str, str] = {}
    for sa in areas:
        for t in sa.tables_defined:
            area_of[(t.schema_name, t.name)] = sa.name
            bare_of.setdefault(t.name, sa.name)
    intra_ids = {id(r) for sa in areas for r in sa.relationships}
    out: list[CrossSubjectAreaRelationship] = []
    for r in rels:
        if id(r) in intra_ids:
            continue
        ft, tt = bare_name(r.from_table), bare_name(r.to_table)
        fa = area_of.get((r.from_schema, ft)) or bare_of.get(ft)
        ta = area_of.get((r.to_schema, tt)) or bare_of.get(tt)
        if fa and ta and fa != ta:
            data = r.model_dump(exclude_none=True, by_alias=True)
            data.update(from_subject_area=fa, to_subject_area=ta, executable="same_engine")
            # The `setdefault("for_questions_about", [])` that stood here was already a no-op: the
            # field defaults to `[]` rather than None, so `exclude_none` keeps it and the dump
            # always carried it. Dropping the dead key from what gets WRITTEN is `_model_dump`'s
            # job, not this one — see the exclusion there.
            out.append(CrossSubjectAreaRelationship(**data))
    return out


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100)


def _model_dump(model) -> dict:
    """Serialize a model for writing to disk.

    `for_questions_about` is excluded because it is deprecated and inert: nothing populates it and
    nothing serves it (see the field's note on `Relationship`), so writing it only spreads a dead
    key into every new model on disk. It defaults to `[]` rather than None, so `exclude_none` does
    not drop it and the exclusion has to be named. The field stays DECLARED — models already
    carrying `for_questions_about: []` must keep loading under `extra="forbid"` — so this stops it
    spreading without breaking anything already written. Pydantic ignores an `exclude` key the
    model does not have, so the one call site serves every model type here.
    """
    return model.model_dump(exclude_none=True, by_alias=True,
                            exclude={"for_questions_about"})


@dataclass
class WriteReport:
    out_dir: str
    dry_run: bool
    files_written: list[str] = field(default_factory=list)
    # Files whose content was already on disk and were left as they are (#472).
    files_unchanged: list[str] = field(default_factory=list)


def _meaningful(value: Any) -> Any:
    """`value` with the null and empty-list entries of every mapping dropped, for comparing two dumps.

    The serializer writes an empty default (`indexes: []`) that an older writer left out, and the two
    mean the same thing to the loader. An empty MAPPING is kept: `choice_field: {}` is a declared enum
    with no labels yet, which is not the same as no `choice_field`. Only mapping entries are dropped:
    an empty item in a LIST is a value someone wrote. The cost: a file that still carries an empty key the current serializer
    omits (`ambiguities: []`) is no longer scrubbed by a refresh, since its data is unchanged.
    """
    if isinstance(value, dict):
        return {
            k: _meaningful(v) for k, v in value.items() if v is not None and v != []
        }
    if isinstance(value, list):
        return [_meaningful(v) for v in value]
    return value


def _same_content(path: Path, content: str) -> bool:
    """Whether the file at `path` already says what `content` says, compared as parsed YAML (#472).

    A refresh of one table re-dumped every file in the model, and a file whose values had not changed
    still came out with its keys reordered, an empty default added or its lines re-wrapped, so a
    one-table refresh was a whole-model diff and the real change was lost in it. Equal data means the
    file is left alone, which also keeps its line endings and formatting. A file that does not parse is
    never equal: rewriting it is the fix.
    """
    if not path.is_file():
        return False
    try:
        old = yaml.safe_load(path.read_text(encoding="utf-8"))
        new = yaml.safe_load(content)
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return False
    return _meaningful(old) == _meaningful(new)


def ensure_org_id(out: Path, existing: Optional[str] = None, *, dry_run: bool = False) -> str:
    """Idempotent, deployment-scoped resolution of the org identity (F14 / ACE-056; relocated by
    F15 / ACE-067). Returns, in order: ``existing`` (an id the caller already resolved); the org_id
    already persisted at ``out/datasource.yaml`` (preserved across re-introspect — the immutability guarantee);
    else the DEPLOYMENT record's id (``organization.yaml`` at ``out.parent``), minting the record on
    first use.

    F15 relocated the id's home from the profiles up into the one root record, which is now the
    deployment-scoped consistency mechanism — so the old "adopt a sibling profile's id" scan is GONE:
    a second profile reads the same id straight from the record instead of scanning siblings. Pure-local
    (a uuid4 minted on-box, no coordinator — the only option under F14's no-egress invariant). On
    ``dry_run`` nothing is persisted: a preview reads an existing record or surfaces a throwaway id."""
    from uuid import uuid4  # local generation only — no egress (F14 invariant)

    from . import loader, org_record

    resolved = existing or loader.load_org_id(out)
    if resolved:
        return resolved
    if dry_run:
        # Preview: never mint/persist a record. Use an existing one if present, else a throwaway id.
        record = org_record.load_org_record(out.parent)
        return record.org_id if record is not None else uuid4().hex
    return org_record.ensure_org_record(out.parent).org_id


def _previous_doc(out: Path) -> dict:
    """The `datasource.yaml` already in `out`, or {} when there is none (#327).

    Read the same way `ensure_org_id` reads the previous file's id, and for the same reason: a
    re-introspect or a spec re-apply rebuilds the model from what it knows, and the database knows
    nothing a human wrote. An unreadable file keeps today's behaviour rather than failing the write."""
    import yaml

    path = out / "datasource.yaml"
    if not path.exists():
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a malformed previous file must not block writing a new one
        return {}
    return doc if isinstance(doc, dict) else {}


def _previous_description(out: Path) -> str:
    value = _previous_doc(out).get("description")
    return value.strip() if isinstance(value, str) else ""


# Datasource-level enrichment a rewrite must not drop: the glossary (`sm set-terminology`), the
# declared ambiguities (#473), and the cross-area entities and metrics kept in this file. Carried over as written, because the loader
# merges the latter with their own sidecar files and re-serializing the merged list would duplicate them.
_CARRIED_DATASOURCE_KEYS = ("key_terminology", "ambiguities", "cross_subject_area_entities",
                            "cross_subject_area_metrics")


def _carried_datasource_fields(org: Datasource, out: Path) -> dict:
    prev = _previous_doc(out)
    carried = {k: prev[k] for k in _CARRIED_DATASOURCE_KEYS if prev.get(k)}
    if org.key_terminology:
        carried["key_terminology"] = dict(org.key_terminology)
    if org.ambiguities:
        carried["ambiguities"] = [a.model_dump(exclude_defaults=True) for a in org.ambiguities]
    return carried


def item_file_stem(name: str) -> str:
    """The file name (without .yaml) an entity or metric is stored under. One rule for every writer:
    `write_tree` used the display name (`Net Sales.yaml`) while `sm add` and `curate`
    used this slug, so rewriting a tree after `sm add` duplicated every item and broke lookup by name."""
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "unnamed"


def write_tree(
    org: Datasource,
    out: Path,
    *,
    examples_by_area: Optional[dict[str, list[dict]]] = None,
    dry_run: bool = False,
) -> WriteReport:
    """Write the canonical on-disk tree (datasource.yaml + datasources + subject_areas +
    optional prompt_examples). Returns the list of files (would-be on dry_run). A file whose content
    is already on disk is left untouched and listed under `files_unchanged` (#472)."""
    examples_by_area = examples_by_area or {}
    rep = WriteReport(out_dir=str(out), dry_run=dry_run)

    def write(rel: str, content: str) -> None:
        if _same_content(out / rel, content):
            rep.files_unchanged.append(rel)
            return
        rep.files_written.append(rel)
        if dry_run:
            return
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    # F14/F15: the deployment identity is the first key. `ensure_org_id` reads the *previous* datasource.yaml
    # (about to be overwritten) so a re-introspect/curate/snapshot preserves the minted id rather than
    # re-minting — the immutability guarantee — and otherwise takes it from the deployment record
    # (minting the record on first use). Resolved once, honoring dry_run so a preview persists nothing.
    org_id = ensure_org_id(out, org.org_id, dry_run=dry_run)
    write("datasource.yaml", _dump({
        "org_id": org_id,
        "datasource": org.datasource,
        "version": org.version,
        # The introspector builds a Datasource with no description, so writing `org.description` as-is
        # wiped the one line an agent routes a question by on every re-introspect (#327). Keep the
        # previous file's line unless the caller supplies a new one.
        "description": org.description or _previous_description(out),
        "fiscal_year_start_month": org.fiscal_year_start_month,
        "storage_connections": [
            {"name": sc.name, "ref": f"datasources/{sc.name}/storage.yaml"}
            for sc in org.storage_connections
        ],
        "subject_areas": [f"subject_areas/{sa.name}" for sa in org.subject_areas],
        "cross_subject_area_relationships": [
            _model_dump(r) for r in org.cross_subject_area_relationships
        ],
        **_carried_datasource_fields(org, out),
    }))
    for sc in org.storage_connections:
        write(f"datasources/{sc.name}/storage.yaml", _dump(_model_dump(sc)))
    for sa in org.subject_areas:
        base = f"subject_areas/{sa.name}"
        write(f"{base}/subject_area.yaml", _dump({
            "name": sa.name,
            "description": sa.description,
            "default_time_window": sa.default_time_window,
            "tables": [_model_dump(tr) for tr in sa.tables],
        }))
        for t in sa.tables_defined:
            write(f"{base}/tables/{t.name}.yaml", _dump(_model_dump(t)))
        for e in sa.entities:
            write(f"{base}/entities/{item_file_stem(e.name)}.yaml", _dump(_model_dump(e)))
        for mm in sa.metrics:
            write(f"{base}/metrics/{item_file_stem(mm.name)}.yaml", _dump(_model_dump(mm)))
        if sa.relationships:
            write(f"{base}/relationships.yaml",
                  _dump({"relationships": [_model_dump(r) for r in sa.relationships]}))
        if sa.name in examples_by_area:
            write(f"prompt_examples/{sa.name}/examples.yaml",
                  _dump({"examples": examples_by_area[sa.name]}))
    if not dry_run:
        # Stamp the model_version pin for the freshly-written model (best-effort).
        from . import org_record, snapshot
        snapshot.write_snapshot(out)
        # Keep the deployment record's datasource list in sync with what's actually on disk — this
        # profile was just written, so rebuild the list from the profile dirs (auto-maintained, no drift).
        org_record.refresh_datasources(out.parent)
    return rep


# Structural patterns for DERIVED metrics — general (English start/end + flag vocabulary), not
# vendor-specific. A flag is a flag and two timestamps are two timestamps on any schema.
_START_NAME_RE = re.compile(
    r"(^|_)(opened|created|started|start|begin|requested|received|logged|reported)(_|$)", re.I)
_END_NAME_RE = re.compile(
    r"(^|_)(closed|resolved|ended|end|finished|completed|fulfilled|approved|delivered)(_|$)", re.I)
_FLAG_NAME_RE = re.compile(r"^(is_|has_).+|.+_flag$", re.I)
_TS_TYPES = {"timestamp", "date"}
# Audit columns record when a row was written or changed, not when the thing it describes began or
# ended, so a duration pairs one only when no other column can stand in for it (#514).
_AUDIT_NAME_RE = re.compile(
    r"^(sys_.+|.+_by|.*modified.*|(created|updated|inserted|last_updated)(_(at|on|date|time|ts))?"
    r"|date_(created|updated))$", re.I)
_PLANNED_NAME_RE = re.compile(r"(^|_)(planned|scheduled|expected|estimated|target)(_|$)", re.I)
_ACTUAL_NAME_RE = re.compile(r"(^|_)actual(_|$)", re.I)
# Below this many rows a rate or an average is too thin to define a metric on (#514).
TINY_TABLE_ROWS = 50


def _flag(code: str, why: str) -> dict:
    return {"flag": code, "why": why}


def _duration_family(name: str) -> str:
    """planned / actual / "" — a duration pairs a start and an end of the SAME family, never a
    planned start with an actual end."""
    if _PLANNED_NAME_RE.search(name):
        return "planned"
    if _ACTUAL_NAME_RE.search(name):
        return "actual"
    return ""


def _prefer_non_audit(cols: list[Column]) -> tuple[list[Column], bool]:
    """The non-audit columns, or the audit ones (and True) when they are all there is."""
    plain = [c for c in cols if not _AUDIT_NAME_RE.match(c.name)]
    return (plain, False) if plain else (cols, bool(cols))


# How many other pairings an ambiguous_pairing flag names; a wide table can have dozens.
_LISTED_PAIRS = 3


def _others(pairs: list[tuple[Column, Column]], this: tuple[Column, Column]) -> str:
    rest = (f"{a.name}→{b.name}" for a, b in pairs if (a, b) != this)
    shown = list(itertools.islice(rest, _LISTED_PAIRS))
    more = len(pairs) - 1 - len(shown)
    return ", ".join(shown) + (f" … and {more} more" if more else "")


def _duration_pairs(ts_cols: list[Column]) -> list[tuple[Column, Column, str, list[dict]]]:
    """(start, end, family, flags) for every start/end pairing worth offering."""
    out = []
    for fam in ("", "actual", "planned"):
        mine = [c for c in ts_cols if _duration_family(c.name) == fam]
        starts, s_audit = _prefer_non_audit([c for c in mine if _START_NAME_RE.search(c.name)])
        ends, e_audit = _prefer_non_audit([c for c in mine if _END_NAME_RE.search(c.name)])
        pairs = [(s, e) for s in starts for e in ends if s is not e]
        for s, e in pairs:
            flags = []
            if len(pairs) > 1:
                flags.append(_flag("ambiguous_pairing", f"{len(pairs)} start/end pairings fit; "
                                   f"also offered: {_others(pairs, (s, e))}"))
            if s_audit or e_audit:
                which = ", ".join(c.name for c, a in ((s, s_audit), (e, e_audit)) if a)
                flags.append(_flag("audit_column", f"{which} looks like an audit column (when the "
                                   "row was written), the only candidate for this pairing"))
            out.append((s, e, fam, flags))
    return out


def flag_columns(table: Table) -> list[Column]:
    """The columns a share-of-true rate could be proposed on: boolean, or an integer named like a
    flag (`is_*`/`has_*`/`*_flag`), not a key, and described (an `ai_unknown` one is skipped)."""
    return [c for c in table.columns
            if not c.primary_key and c.description_source != "ai_unknown"
            and (c.type == "boolean" or (c.type == "integer" and bool(_FLAG_NAME_RE.match(c.name))))]


def flag_rate_name(table: str, column: str) -> str:
    return f"{table}_{column}_rate"


def suggest_metrics(table: Table, dialect, *, max_per_table: int = 10,
                    flag_rates: bool | Collection[str] = False) -> list[dict]:
    """Per-table reusable measures inferred STRUCTURALLY — all general, no vendor patterns:
      • a RATE for a boolean / `is_*`/`has_*`/`*_flag` column (the `made_sla` / `reopen`
        pattern — fraction true among the rows where the flag is set) — only when asked:
        `flag_rates=True` for every flag column, or a collection of column names
        (case-insensitive) for those. Proposed for every flag column they were most of the
        suggested metrics on a large onboarding while the business defined one (#530);
      • an AVG DURATION for each start+end timestamp pair (the `avg_resolution_time` pattern —
        `AVG(end − start)` via the dialect's day-difference form for the columns' types), leaving
        out rows where the end is before the start. A start pairs only with an end of the same
        planned/actual family, and an audit column only when nothing else fits.
    Returns proposed/unreviewed Metric dicts for curate.write_items; the user signs them off in
    bulk in the explorer. Capped at max_per_table. Each one pairs columns or picks a condition —
    a choice the engine could get wrong — so none of them is auto-approved.

    Each dict also carries `_proposal`, which is not a Metric field and is popped before writing:
    `flags` (why a person should look before it is kept — an ambiguous or audit pairing, a table
    under TINY_TABLE_ROWS rows), `columns` (what the binding reads) and `row_expr` (the per-row
    value the binding averages, which a test run checks for a constant result).

    A plain COUNT(*) / SUM(col) / AVG(col) is never proposed. `runtime._check_aggregation_semantics`
    enforces a column's `aggregation` class on every statement with or without a named metric, and
    everything such a metric could carry beyond its name already reaches the agent on the table it
    opens: the column's `unit` (which the formatter also carries through SUM/AVG), the table's and
    column's `caveats`, and the table's `grain`. So `{t}_total_{c}` adds a name and nothing else;
    proposed per table and per column it was most of a wide model's catalogue (137 of 175, #404),
    every entry listed by its own name (#406). A curator can still add any plain metric by hand.

    The generator writes `calculation` only. `description` — the one-line label the metric index
    shows and search matches — says what the metric is FOR, which is judgement it cannot derive; the
    agami-connect skill writes it (#406).

    Columns agami couldn't read (`description_source == "ai_unknown"`) are SKIPPED — we don't
    propose a metric on a column we can't explain (its prose would just restate opaque SQL).
    Re-run after such a column is described and it becomes eligible (write_items is incremental,
    so no duplicates)."""
    t = table.name
    st = dialect.name
    out: list[dict] = []
    flags_here = flag_columns(table)
    wanted = (None if flag_rates is True
              else {n.lower() for n in flag_rates} if flag_rates else set())
    for c in flags_here:
        if wanted is not None and c.name.lower() not in wanted:
            continue
        row = dialect.flag_rate_expr(dialect.ident(c.name), boolean=c.type == "boolean")
        out.append({"name": flag_rate_name(t, c.name),
                    "calculation": f"Fraction of {t} where {c.name} is true, among the rows "
                                   f"where {c.name} is set (rows where it is NULL are left out)",
                    "bindings": {st: f"AVG({row})"},
                    "source_tables": [t],
                    "_proposal": {"flags": [], "columns": [c.name], "row_expr": row}})
    ts_cols = [c for c in table.columns
               if not c.primary_key and c.description_source != "ai_unknown"
               and c.type in _TS_TYPES]
    pairs = _duration_pairs(ts_cols)
    for s, e, fam, flags in pairs:
        days = dialect.duration_days_expr(dialect.ident(s.name), dialect.ident(e.name),
                                          start_type=s.type, end_type=e.type)
        guard = dialect.duration_guard(dialect.ident(s.name), dialect.ident(e.name),
                                       start_type=s.type, end_type=e.type)
        row = f"CASE WHEN {guard} THEN {days} END"
        same_family = [p for p in pairs if p[2] == fam]
        name = (f"{t}_avg_days_{s.name}_to_{e.name}" if len(same_family) > 1
                else f"{t}_avg_{fam + '_' if fam else ''}duration_days")
        out.append({"name": name,
                    "calculation": f"Average days from {s.name} to {e.name} in {t}; rows where "
                                   f"{e.name} is before {s.name} are left out",
                    "bindings": {st: f"AVG({row})"},
                    "source_tables": [t], "unit": "days",
                    "_proposal": {"flags": flags, "columns": [s.name, e.name], "row_expr": row}})
    # Clamped, not floored at 1: `0` honestly means none, but a negative cap must not fall through
    # to Python's negative slice, where `-1` would quietly return every proposal except the last.
    out = out[: max(0, max_per_table)]
    rows = table.performance_hints.estimated_row_count if table.performance_hints else None
    for m in out:
        m["confidence"] = "proposed"
        m["review_state"] = "unreviewed"
        m["primary_table"] = t  # every suggested metric is single-table — anchor it there
        if rows is not None and 0 <= rows < TINY_TABLE_ROWS:
            m["_proposal"]["flags"].append(_flag(
                "low_confidence", f"{t} has about {rows} rows (under {TINY_TABLE_ROWS})"))
    return out


__all__ = [
    "SENSITIVE_RE", "detect_sensitive", "detect_money_column", "suggest_unit", "name_currency",
    "name_words",
    "derive_column_groups", "column_group_descriptions", "maybe_column_groups",
    "infer_cardinality", "cluster_by_family", "make_area", "make_table_ref",
    "propose_subject_areas", "extract_cross_area_relationships",
    "suggest_metrics", "flag_columns", "flag_rate_name", "suspected_pii", "canonical_description",
    "write_tree", "WriteReport",
]
