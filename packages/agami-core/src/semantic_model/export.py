"""Export core (F19 / ACE-167): what a semantic model carries to another agent's format.

`sm export` writes a reviewed semantic model into the files a Snowflake, Databricks or Ossie agent
loads. Each target's format lives in its own module (`export_<target>.py`); this module makes the
four decisions every target shares, once, so no writer can make them differently:

* **Eligibility.** Only approved items travel by default, a column flagged `sensitive` stays home,
  and anything whose SQL names a column or table left home stays with it. Prose that mentions one
  travels blank, and the report never prints a sensitive column's name.
* **Binding resolution.** A metric carries the binding a person wrote for the target's engine,
  expanded and parsed in that dialect. SQL is never translated between engines.
* **Stable ids**, from `export_id`, so a re-export diffs cleanly.
* **The report**: every item dropped or weakened, with one closed reason code.

A writer reads only the `ExportView` built here, never the loaded `Datasource`, so the policy above
has one place to hold. The export reads files and writes files: it opens no database connection.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import re
import shutil
import tempfile
from dataclasses import InitVar, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping, NamedTuple, Optional, get_args, get_origin

from . import derived as D
from .golden import GoldenBounds, GoldenItem, load_golden_datasets
from .loader import list_prompt_examples, load_datasource
from .models import Datasource, SubjectArea, Table, bare_name
from .sql_dialect import DialectUnresolved, sqlglot_dialect

Outcome = Literal["dropped", "degraded"]
Reason = Literal[
    "not_approved", "sensitive", "depends_on_excluded", "no_binding_for_target",
    "sql_not_for_target", "unparseable_for_target", "no_home_in_target", "carried_as_text",
    "detail_lost", "target_rule", "target_limit", "name_collision", "spans_subject_areas",
]
REASONS: tuple[str, ...] = get_args(Reason)

# Enforcement happens inside agami at query time and has no counterpart in a file another agent
# loads, so every report says plainly that it stayed behind.
DOES_NOT_TRAVEL = (
    "pre-query checks (table, column, sensitive-column and join scope)",
    "answer receipts",
    "audit log rows",
    "who signed off an item (an item travels as approved, never with its signer)",
)

# A table's default filter is descriptive in agami: the query writer adds it only when a question
# calls for it. Every target says so in these words, which name both halves (optional, and not part
# of any metric's definition), since an agent reading "apply unless the question says otherwise"
# may take it as always on.
DEFAULT_FILTER_NOTE = ("An optional filter, declared in agami as a default filter: use it only when "
                       "the question asks for this restriction. It is not applied automatically, "
                       "and metric definitions are not filtered by it.")

# Kept out of the report on purpose: the report is an emitted file, and a sensitive column's name
# must not reach one. The column is named by its export id instead.
_WITHHELD = ("a column flagged sensitive; its name is withheld from this report. `sm sensitive` "
             "lists it, and --include-sensitive carries it")
_LEFT_HOME = "names a column or table this export leaves out"

# Month names written out, for every writer that names the fiscal year's first month: `calendar`
# follows the process locale, and an export writes the same bytes on every machine.
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December")

# Kinds the model holds with no review state. The contract counts them approved for eligibility,
# so they travel; but nobody recorded a review of them, so no target may mark one published or
# certified (`ExportView.reviewed`).
NO_REVIEW_STATE = ("ambiguity", "terminology")
_BLANKED = "mentioned a column or table this export leaves out, so it travels blank"

# Each writer slice adds its module name here; importing the module registers its target.
_TARGET_MODULES: tuple[str, ...] = (
    "export_snowflake",
    "export_databricks",
    "export_databricks_genie",
    "export_databricks_pages",
    "export_databricks_uc",
    "export_ossie",
)


class ExportRefused(Exception):
    """The export cannot run as asked. Raised before anything is written; the message names the fix."""


@dataclass(frozen=True)
class LossEntry:
    """One item that did not carry over whole: `dropped`, or carried with less (`degraded`)."""

    area: str
    kind: str
    name: str
    outcome: Outcome
    reason: Reason
    detail: str

    def __post_init__(self) -> None:
        # The codes are a contract with every writer and every reader of the report.
        if self.reason not in REASONS or self.outcome not in ("dropped", "degraded"):
            raise ValueError(f"not a loss code: {self.outcome}/{self.reason}")

    def to_dict(self) -> dict[str, str]:
        return {"area": self.area, "kind": self.kind, "name": self.name, "outcome": self.outcome,
                "reason": self.reason, "detail": self.detail}


def dropped(area: str, kind: str, name: str, reason: Reason, detail: str) -> LossEntry:
    return LossEntry(area, kind, name, "dropped", reason, detail)


def degraded(area: str, kind: str, name: str, reason: Reason, detail: str) -> LossEntry:
    return LossEntry(area, kind, name, "degraded", reason, detail)


def qualified_name(area: str, kind: str, name: str) -> str:
    """`<subject area>/<kind>/<name>`: what ids are minted from and items are ordered by."""
    return f"{area}/{kind}/{name}"


def export_id(target: str, kind: str, qualified: str) -> str:
    """32 lowercase hex characters, the same for the same inputs in any process or run."""
    return hashlib.sha256(f"{target}\n{kind}\n{qualified}".encode("utf-8")).hexdigest()[:32]


class Location(NamedTuple):
    database: str
    schema: str


def parse_location(text: str) -> Location:
    parts = text.split(".")
    if len(parts) != 2 or not all(p.strip() for p in parts):
        raise ExportRefused(f"--location takes <database>.<schema>, got {text!r}")
    return Location(parts[0].strip(), parts[1].strip())


class PhysicalName(NamedTuple):
    database: Optional[str]
    schema: Optional[str]
    table: str


def physical_key(p: PhysicalName) -> str:
    """One physical table's identity across subject areas, `database.schema.table` casefolded:
    the export's `--location` and the model spell one table one way, so case is the only variation
    to absorb."""
    return ".".join((p.database or "", p.schema or "", p.table)).casefold()


@dataclass(frozen=True)
class ExportOptions:
    targets: tuple[str, ...]
    areas: tuple[str, ...] = ()
    include_unreviewed: bool = False
    include_sensitive: bool = False
    location: Optional[Location] = None
    # Flags a writer slice adds for its own target (`--ossie-version`), by argparse dest.
    target_args: Mapping[str, Any] = field(default_factory=dict)

    def to_report(self) -> dict[str, Any]:
        return {"targets": list(self.targets), "areas": list(self.areas),
                "include_unreviewed": self.include_unreviewed,
                "include_sensitive": self.include_sensitive,
                "location": ".".join(self.location) if self.location else None,
                "target_args": {k: self.target_args[k] for k in sorted(self.target_args)}}


@dataclass(frozen=True)
class ExportTable:
    table: Table               # a copy: excluded columns, and filters that do not travel, removed
    physical: PhysicalName     # where the target finds it
    # The names of this table's columns left home as `sensitive` (never one left home as not
    # approved), in the model's order. A target that reads columns from its own catalog, as Genie
    # does, still shows such a column to its agent, and hiding it there means naming it. Held off
    # the dataclass fields on purpose, so a whole-view dump (`asdict`, a debug writer) never
    # carries them; `dataclasses.replace` resets them to none, the safe direction.
    withheld: InitVar[tuple[str, ...]] = ()
    withheld_count: int = field(init=False, default=0)

    def __post_init__(self, withheld: tuple[str, ...]) -> None:
        object.__setattr__(self, "_withheld", tuple(withheld))
        object.__setattr__(self, "withheld_count", len(withheld))

    @property
    def withheld_columns(self) -> tuple[str, ...]:
        """The sensitive column names this table left home. A writer may emit them ONLY under an
        explicit opt-in flag of its own (to hide them, as `column_configs: [{column_name,
        exclude: true}]`), and otherwise must not let one reach any file or loss entry: by default
        no emitted byte names a sensitive column. `withheld_count` says how many without naming
        them, for a writer that reports the columns its target's catalog still shows."""
        return self._withheld  # type: ignore[attr-defined]


@dataclass(frozen=True)
class ExportMetric:
    metric: Any                # a `Metric` copy, signer removed
    sql: Mapping[str, str]     # engine -> binding expanded and parsed in that engine's dialect
    # engine -> the same binding as written, before `{metric}` references were expanded, for the
    # engines in `sql` only. A writer reads a metric's shape here (which metric it builds on),
    # never from the model's raw bindings, which hold engines the core did not admit.
    template: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportRelationship:
    # `<from>-><to>`, then `#2`, `#3` for further joins between the same two tables in file order.
    # Columns stay out of it, since a key column may be the sensitive one.
    name: str
    relationship: Any          # a `Relationship` copy, signer removed


@dataclass(frozen=True)
class ExportQuestion:
    """A prompt example (`kind` "example") or golden item (`kind` "golden"). `sql` is None when it
    was written for another engine, or was never written.

    The fields after `tables` are a golden item's answer-key detail, empty for an example. Prose
    among them (`tags`, `chart_type`, `data_shape`, `validation_notes`) travels blank when it
    mentions a name left home; `must_filter` naming one keeps the whole item home. `detail_fields`
    names, in `GOLDEN_DETAIL_FIELDS` spelling, the fields the item holds that the report lists as
    `detail_lost` unless the writer declares them carried (`WriterOutput.carried_golden_fields`)."""

    kind: str
    name: str
    question: str
    sql: Optional[str]
    tables: tuple[str, ...] = ()
    match: Optional[str] = None                  # golden match level: exact, values, shape, ...
    bounds: Optional[GoldenBounds] = None        # min/max rows and values, for `bounded`
    must_filter: tuple[str, ...] = ()            # what a correct answer has to filter on
    tags: tuple[str, ...] = ()
    chart_type: Optional[str] = None
    data_shape: Optional[str] = None
    validation_notes: Optional[str] = None
    detail_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportView:
    """The eligible part of one subject area for one target. Items are ordered by name."""

    target: str
    engines: tuple[str, ...]           # the engines the target carries metric SQL for
    source_engine: str                 # the engine the semantic model's SQL is written for
    sql_engine: Optional[str]          # the engine of carried non-metric SQL; None: none carried
    datasource: str
    description: str
    area: str
    area_description: str
    tables: tuple[ExportTable, ...]
    metrics: tuple[ExportMetric, ...]
    relationships: tuple[ExportRelationship, ...]
    entities: tuple[Any, ...]          # `Entity` copies
    examples: tuple[ExportQuestion, ...]
    golden: tuple[ExportQuestion, ...]
    ambiguities: tuple[Any, ...]       # `Ambiguity` models
    terminology: tuple[tuple[str, str], ...]
    unreviewed: frozenset[tuple[str, str]] = frozenset()   # (kind, name) carried by the flag
    fiscal_year_start_month: int = 1           # 1..12, the datasource's
    default_time_window: Optional[str] = None  # the area's, as written ("last 90 days"); prose
    # (kind, name, signed_off_at) for each carried approved item with a recorded time, never who.
    signed: frozenset[tuple[str, str, str]] = frozenset()
    # `physical_key` of a table an earlier area of this run also carries -> that area (`claimed_by`).
    claimed: Mapping[str, str] = field(default_factory=dict)

    def id(self, kind: str, name: str) -> str:
        return export_id(self.target, kind, qualified_name(self.area, kind, name))

    def approved(self, kind: str, name: str) -> bool:
        """Whether a carried item is approved: false only for one `--include-unreviewed` carried.
        Kinds with no review state (ambiguity, terminology, default_filter) read as approved."""
        return (kind, name) not in self.unreviewed

    def reviewed(self, kind: str, name: str) -> bool:
        """Whether a person recorded a review that approved the item: `approved`, and false for a
        kind with no review state (`NO_REVIEW_STATE`). What a writer marks published or certified
        asks this, not `approved`, so a term nobody reviewed is never shown as certified."""
        return kind not in NO_REVIEW_STATE and self.approved(kind, name)

    def claimed_by(self, physical: PhysicalName) -> Optional[str]:
        """The subject area, earlier in this run's order, whose view also carries the physical
        table `physical`; None when this view is the first. A writer that changes the table itself
        (Unity Catalog DDL) leaves it to that area, so two areas never write one statement twice."""
        return self.claimed.get(physical_key(physical))

    def signed_off_at(self, kind: str, name: str) -> Optional[str]:
        """When a carried, approved item was signed off, as the model records it; None when it is
        carried unreviewed or no time is recorded. Never who signed: that stays in agami. Kinds
        and names as `approved` takes them (a column is `table.column`, a relationship its view
        name, a golden item `dataset:id`); a golden item's time is its `confirmed_by.at`."""
        return next((at for k, n, at in self.signed if k == kind and n == name), None)

    @property
    def withheld_count(self) -> int:
        """How many sensitive columns of the carried tables were left home, unnamed."""
        return sum(t.withheld_count for t in self.tables)


# A golden item's detail fields, as `uncarried_golden_fields` names them. A writer declares the
# ones it carried for an item in `WriterOutput.carried_golden_fields`.
GOLDEN_DETAIL_FIELDS: tuple[str, ...] = (
    "match", "bounds", "must_filter", "tags", "recorded", "expected.chart_type",
    "expected.data_shape", "expected.validation_notes")


@dataclass(frozen=True)
class WriterOutput:
    files: Mapping[str, bytes]         # path relative to <out>/<target>/ -> content
    losses: tuple[LossEntry, ...] = ()
    # Golden item name (`dataset:id`) -> the `GOLDEN_DETAIL_FIELDS` this target's files carry for
    # it. The core lists the item's other detail fields as `detail_lost`, and none when it carried
    # them all, so the report stays true without each writer rewriting the core's entry.
    carried_golden_fields: Mapping[str, frozenset[str]] = field(default_factory=dict)


Writer = Callable[[ExportView, ExportOptions], WriterOutput]


@dataclass(frozen=True)
class Target:
    name: str
    engines: tuple[str, ...]
    write: Writer
    add_arguments: Optional[Callable[[argparse.ArgumentParser], None]] = None


TARGETS: dict[str, Target] = {}


def register(target: Target) -> None:
    TARGETS[target.name] = target


def targets() -> dict[str, Target]:
    for mod in _TARGET_MODULES:
        importlib.import_module(f".{mod}", __package__)
    return TARGETS


# ---------------------------------------------------------------------------
# Matching an excluded name
# ---------------------------------------------------------------------------


def _mentions(text: str, names: frozenset[str]) -> bool:
    """Whether `text` holds one of `names` as a whole word, in any case. A quoted, bracketed or
    back-ticked spelling, an alias's source and a name in a literal all match: a false match costs
    one item in the report, a miss leaks the name."""
    folded = text.casefold()
    return any(re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", folded) for n in names)


def _dialect(engine: str) -> Optional[str]:
    try:
        return sqlglot_dialect(engine)
    except DialectUnresolved:
        return None


def sql_names_any(sql: str, names: frozenset[str], engine: str,
                  star_tables: frozenset[str] = frozenset()) -> bool:
    """Whether `sql` names one of `names` (casefolded) as a column, table or alias, or projects a
    `*` over one of `star_tables`, which returns their excluded columns without naming them. SQL
    that does not parse counts as naming one: what cannot be read cannot be cleared."""
    if not names:
        return False
    if _mentions(sql, names):
        return True
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel

    try:
        tree = sqlglot.parse_one(sql, dialect=_dialect(engine), error_level=ErrorLevel.RAISE)
    except Exception:
        return True
    if tree is None:
        return True
    # The parser decodes spellings the text match cannot see, such as escapes in a quoted name.
    if any(n.name.casefold() in names for n in tree.find_all(exp.Identifier, exp.Var)):
        return True
    return bool(star_tables) and _star_reads(tree, star_tables)


def _star_reads(tree: Any, tables: frozenset[str]) -> bool:
    """Whether a `*` or `t.*` in a projection, bare or inside a call such as `OBJECT_CONSTRUCT(*)`,
    reads a table in `tables`. Each select is checked against its own FROM, so a star over a
    subquery or CTE is cleared by that subquery's own projection, which is checked in turn. A star
    whose source cannot be told counts as reading one."""
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope

    try:
        scopes = traverse_scope(tree)
    except Exception:
        return True
    for scope in scopes:
        select = scope.expression
        # `EXISTS (SELECT * …)` returns no column, only whether a row exists.
        if not isinstance(select, exp.Select) or isinstance(select.parent, exp.Exists):
            continue
        for star in (s for item in select.expressions for s in item.find_all(exp.Star)):
            if isinstance(star.parent, exp.Count):
                continue  # COUNT(*) counts rows; it returns no column
            qualifier = star.parent.table if isinstance(star.parent, exp.Column) else ""
            aliases = [qualifier] if qualifier else list(scope.selected_sources)
            for alias in aliases or [""]:
                source = scope.sources.get(alias)
                if source is None or (isinstance(source, exp.Table)
                                      and source.name.casefold() in tables):
                    return True
    return False


def excluded_names(org: Datasource, options: ExportOptions) -> frozenset[str]:
    """Every column and table name this export leaves home, casefolded and matched in any table."""
    out: set[str] = set()
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            if t.review_state != "approved" and not options.include_unreviewed:
                out.add(bare_name(t.name).casefold())
            out.update(c.name.casefold() for c in t.columns if _column_left_home(c, options))
    return frozenset(out)


def _column_left_home(c: Any, options: ExportOptions) -> bool:
    return ((c.sensitive and not options.include_sensitive)
            or (c.review_state != "approved" and not options.include_unreviewed))


@dataclass(frozen=True)
class Exclusions:
    """What one run leaves home, decided once for every target."""

    names: frozenset[str]        # column and table names, casefolded, matched in any table
    star_tables: frozenset[str]  # tables with a column left home, which a `*` would return
    secret: frozenset[str]       # sensitive column names, which no report field may print


def exclusions(org: Datasource, options: ExportOptions, engine: str) -> Exclusions:
    """`excluded_names`, plus every table defined by a query that reads one of them. Such a table
    is a second way to the column (`SELECT email AS contact`), so its name joins the excluded
    names, and whatever names it goes with it. Repeated until no table is added, since a table's
    query may read another such table."""
    names = set(excluded_names(org, options))
    tables = [t for sa in org.subject_areas for t in sa.tables_defined]
    stars = frozenset(bare_name(t.name).casefold() for t in tables
                      if any(_column_left_home(c, options) for c in t.columns))
    secret = frozenset(c.name.casefold() for t in tables for c in t.columns
                       if c.sensitive and not options.include_sensitive)
    queries = [t for t in tables if t.source_type == "sql"]
    grew = True
    while grew:
        grew = False
        for t in queries:
            key = bare_name(t.name).casefold()
            if key not in names and sql_names_any(t.sql or "", frozenset(names), engine, stars):
                names.add(key)
                grew = True
    return Exclusions(frozenset(names), stars, secret)


def withheld_name(target: str, area: str, kind: str, name: str, secret: frozenset[str]) -> str:
    """How a report names an item whose own name holds a sensitive column's name: by its export
    id. A column keeps its table when the table's name is clean, as a sensitive column does."""
    if kind in ("column", "choice_field") and "." in name:
        # A column's labels are named by the column's id, so the two entries read as one item.
        cid = export_id(target, "column", qualified_name(area, "column", name))
        table = name.rsplit(".", 1)[0]
        return cid if _mentions(table, secret) else f"{table}.{cid}"
    return export_id(target, kind, qualified_name(area, kind, name))


_WITHHELD_DETAIL = "its detail would print a sensitive column's name, so it is withheld"


def safe_loss(loss: LossEntry, target: str, secret: frozenset[str]) -> LossEntry:
    """`loss` with no field printing a sensitive column's name. The report is an emitted file, and
    an item's name or a writer's detail is text a person wrote (a question, a term, a metric)."""
    name, detail = loss.name, loss.detail
    if _mentions(name, secret):
        name = withheld_name(target, loss.area, loss.kind, name, secret)
    if _mentions(detail, secret):
        detail = _WITHHELD_DETAIL
    return dataclasses.replace(loss, name=name, detail=detail)


# ---------------------------------------------------------------------------
# Physical table names in carried SQL
# ---------------------------------------------------------------------------


_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


def _folding(engine: str) -> Optional[str]:
    """How `engine` folds an unquoted name ("upper", "lower"), or None where it matches names in any
    case, quoted or not (Databricks)."""
    from .dialects import get_dialect

    try:
        return get_dialect(engine).folds_unquoted
    except ValueError:
        return None


def stored_name(name: str, quoted: bool, engine: str) -> str:
    """A name as SQL wrote it, in the form `engine` stores and compares it: a quoted name exactly,
    an unquoted one folded. Where the engine matches in any case, casefolded either way."""
    fold = _folding(engine)
    if fold is None:
        return name.casefold()
    if quoted:
        return name
    return name.upper() if fold == "upper" else name.lower()


def model_stored_name(name: str, engine: str) -> str:
    """A name the model holds, in the form `engine` stores it. The model spells a catalog name the
    way agami's dialect layer does (`dialects.Dialect.ident`): a plain name in the case the engine
    folds to, or one-case on an upper-folding engine, is the unquoted name; any other spelling is
    the stored name exactly. So on Snowflake `orders` is ORDERS, and `"orders"` in SQL is not it."""
    fold = _folding(engine)
    if fold is None:
        return name.casefold()
    if _PLAIN_NAME.match(name) and (name in (name.lower(), name.upper()) if fold == "upper"
                                    else name == name.lower()):
        return name.upper() if fold == "upper" else name.lower()
    return name


def names_table(ref: Any, table: str, home: Optional[PhysicalName], engine: str) -> bool:
    """Whether the parsed table reference `ref` is the model's table `table`, which lives at `home`.
    Its name must be the table's under the engine's case rules, and any schema or catalog it names
    must be the table's own: `archive.orders` is another table, never this `orders`. A reference
    that is not plain names (`IDENTIFIER('orders')`, a table function, four parts) is none."""
    from sqlglot import exp

    parts = {"this": table, "db": home.schema if home else None,
             "catalog": home.database if home else None}
    for arg, want in parts.items():
        ident = ref.args.get(arg)
        if ident is None and arg != "this":
            continue
        if not isinstance(ident, exp.Identifier) or want is None:
            return False
        if stored_name(ident.name, bool(ident.args.get("quoted")), engine) not in \
                _model_spellings(want, engine):
            return False
    return True


def _model_spellings(name: str, engine: str) -> set[str]:
    # A keyword must be quoted to be a name, and agami's dialect layer quotes one as the model
    # writes it (`"order"`), while the engine stores the unquoted spelling folded (`"ORDER"`).
    # Both are the model's table; for any other name only the stored form is.
    from .dialects import get_dialect

    out = {model_stored_name(name, engine)}
    try:
        if get_dialect(engine).reserves(name):
            out.add(name)
    except ValueError:
        pass
    return out


def rewrite_table_refs(sql: str, mapping: Mapping[str, str], engine: str,
                       homes: Mapping[str, PhysicalName]) -> Optional[str]:
    """`sql` with each reference to a model table replaced by `mapping[<its bare name>]` (inserted
    verbatim, so the caller quotes it for its engine), or None when that cannot be done for
    certain: the SQL does not parse in `engine`'s dialect, or no longer parses once rewritten; it
    reads a table `mapping` does not name; two keys of `mapping` are one name to the engine; a
    table reference sits in no scope or is not a plain name (`IDENTIFIER('orders')`); or a column
    is qualified by a schema (`SALES_DATA.orders.amount`), which the new name could break.

    A reference is the model's table only when its name is the table's under the engine's case
    rules (on Snowflake `"orders"` is not ORDERS) and any schema or catalog it names is the one
    `homes[<bare name>]` gives: `archive.orders` and `otherdb.otherschema.orders` are other tables,
    so such SQL is refused rather than pointed at the model's `orders`.

    Names are placed through sqlglot's scopes, so a CTE named like a table shadows it where the
    engine would let it, and a reference already qualified is replaced whole. A column qualified by
    the table's name (`orders.amount`) is left as written: an engine resolves it against the
    qualified table by its last part. The new names are spliced in by token position, never
    regenerated from the tree (test_ace093_byte_identity), so every other byte stays as written."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel
    from sqlglot.optimizer.scope import traverse_scope

    if len({model_stored_name(k, engine) for k in mapping}) != len(mapping):
        return None
    try:
        tree = sqlglot.parse_one(sql, dialect=_dialect(engine), error_level=ErrorLevel.RAISE)
        if tree is None or any(c.args.get("db") for c in tree.find_all(exp.Column)):
            return None
        spans: dict[tuple[int, int], str] = {}
        placed: set[int] = set()
        for scope in traverse_scope(tree):
            for src in scope.sources.values():
                if not isinstance(src, exp.Table) or id(src) in placed:
                    continue
                key = next((k for k in mapping
                            if names_table(src, k, homes.get(k), engine)), None)
                if key is None:
                    return None
                placed.add(id(src))
                spans[token_span(src.args.get("catalog"), src.args.get("db"), src.this)] = \
                    mapping[key]
        ctes = {c.alias_or_name.casefold() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            if id(t) not in placed and not (t.name.casefold() in ctes and not t.db
                                            and isinstance(t.this, exp.Identifier)):
                return None
        out = splice_spans(sql, spans)
        # The caller's names are inserted verbatim; what they make must still be one statement.
        return out if out is not None and parses(out, engine, one_statement=True) else None
    except Exception:  # a shape the scope analysis cannot place: never emit a guess
        return None


def undeclared_reads(sql: str, columns: Mapping[str, Any], engine: str,
                     homes: Mapping[str, PhysicalName]) -> Optional[str]:
    """Why `sql` may read a value the model does not declare, or None when every column it reads
    is one of `columns[<bare table name>]` (the names a table carries) on a table it places for
    certain. Leaving a column out of the model is how an author keeps it out of agami, so anything
    unresolved counts as undeclared: an unqualified name two sources could hold, a column of a table
    function, a `NATURAL` join, a `USING` column one side does not declare, and a `*` anywhere,
    bare, as `t.*` or inside a call (`OBJECT_CONSTRUCT(*)`), since a star reads columns it never
    names. `COUNT(*)` alone is allowed: it counts rows and reads no column."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel
    from sqlglot.optimizer.scope import traverse_scope

    try:
        tree = sqlglot.parse_one(sql, dialect=_dialect(engine), error_level=ErrorLevel.RAISE)
        scopes = traverse_scope(tree)
    except Exception:
        return "its columns cannot be placed for certain"
    if any(not (isinstance(s.parent, exp.Count) and s.parent.this is s)
           for s in tree.find_all(exp.Star)):
        return "it reads *, which returns columns it never names"
    declared = {k: set().union(*(_model_spellings(c, engine) for c in cs)) for k, cs in columns.items()}

    def table_of(src: Any) -> Optional[str]:
        return next((k for k in declared if names_table(src, k, homes.get(k), engine)), None)

    def holds(src: Any, ident: Any) -> bool:
        if isinstance(src, exp.Table):
            key = table_of(src)
            return key is not None and stored_name(
                ident.name, bool(ident.args.get("quoted")), engine) in declared[key]
        # A subquery or CTE, whose own columns are checked in its scope. A table function's
        # output (`FLATTEN`, a UDTF) is not: what it reads is not visible here.
        query = getattr(src, "expression", None)
        return isinstance(query, exp.Query) and ident.name.casefold() in {
            n.casefold() for n in query.named_selects}

    seen: set[int] = set()
    for scope in scopes:
        for join in scope.expression.args.get("joins") or []:
            if join.args.get("kind") == "NATURAL" or join.args.get("method") == "NATURAL":
                return "it joins NATURAL, which reads every column two tables share"
            for ident in join.args.get("using") or []:
                if not all(holds(src, ident) for src in scope.sources.values()):
                    return f"its USING column {ident.name} is not declared on every side"
        for col in scope.columns:
            seen.add(id(col))
            if col.table:
                src, s = None, scope
                while s is not None and src is None:
                    src = next((v for k, v in s.sources.items()
                                if k.casefold() == col.table.casefold()), None)
                    s = s.parent
                if src is None or not holds(src, col.this):
                    return f"it reads {col.table}.{col.name}, which the model does not declare"
                continue
            hits = [src for src in scope.sources.values() if holds(src, col.this)]
            # A select alias may stand in a later clause, but not in WHERE, where an engine reads
            # a column of the same name first, and that column may be one the model leaves out.
            alias = not hits and not col.find_ancestor(exp.Where) and is_select_alias(col, scope.expression)
            if len(hits) != 1 and not alias:
                return (f"its column {col.name} " + ("could come from several sources" if hits
                                                     else "is no column the model declares"))
    for col in tree.find_all(exp.Column):
        # Scopes leave out an ORDER BY or QUALIFY reference to a select alias.
        select = col.find_ancestor(exp.Select)
        if id(col) not in seen and (col.table or select is None or col.name.casefold() not in {
                n.casefold() for n in select.named_selects}):
            return f"its column {col.name} is in no scope"
    return None


def is_select_alias(col: Any, select: Any) -> bool:
    """Whether unqualified `col` names an alias `select` gives one of its outputs, outside that
    output's own expression: `SELECT region, salary` names no alias, so `salary` is a column."""
    from sqlglot import exp

    if not isinstance(select, exp.Select):
        return False
    for item in select.expressions:
        if isinstance(item, exp.Alias) and item.alias.casefold() == col.name.casefold():
            return col.find_ancestor(exp.Alias) is not item
    return False


# ---------------------------------------------------------------------------
# Helpers the target writers share
# ---------------------------------------------------------------------------


def parse_statement(sql: str, engine: str) -> Any:
    """`sql` parsed in `engine`'s dialect; raises when it does not parse. Like sqlglot's
    `parse_one`, a text of several statements gives the first."""
    import sqlglot
    from sqlglot.errors import ErrorLevel

    return sqlglot.parse_one(sql, dialect=_dialect(engine), error_level=ErrorLevel.RAISE)


def parses(sql: str, engine: str, as_expression: bool = False,
           one_statement: bool = False) -> bool:
    """Whether `sql` parses in `engine`'s dialect: as a statement, or, with `as_expression`, as an
    expression (wrapped in a SELECT, since snippets and join conditions are expressions). With
    `one_statement`, a text holding more than one statement does not count."""
    import sqlglot
    from sqlglot.errors import ErrorLevel

    try:
        text = f"SELECT {sql}" if as_expression else sql
        if one_statement:
            trees = sqlglot.parse(text, dialect=_dialect(engine), error_level=ErrorLevel.RAISE)
            return len([t for t in trees if t is not None]) == 1
        return parse_statement(text, engine) is not None
    except Exception:
        return False


def join_sentences(*parts: Optional[str]) -> str:
    """The non-blank `parts`, stripped, as one text separated by single spaces."""
    return " ".join(p.strip() for p in parts if p and p.strip())


def put_nonempty(doc: dict, key: str, value: Any) -> None:
    """`doc[key] = value` unless `value` is None or an empty string, list or dict: a target file
    leaves an empty field out rather than writing it blank."""
    if value not in (None, "", [], {}):
        doc[key] = value


def token_span(*parts: Any) -> tuple[int, int]:
    """The text a run of identifiers covers, quotes included, from the parser's token positions."""
    present = [p for p in parts if p is not None]
    return present[0].meta["start"], present[-1].meta["end"] + 1


def splice_spans(text: str, spans: dict[tuple[int, int], str]) -> Optional[str]:
    """`text` with each (start, end) span replaced, last first so earlier positions hold; None
    when two spans overlap. The repo never regenerates SQL from a parsed tree
    (test_ace093_byte_identity), so a rewrite is a splice: every byte outside the spans stays as
    a person wrote it."""
    end = len(text) + 1
    for (start, stop), new in sorted(spans.items(), reverse=True):
        if stop > end:
            return None
        text, end = text[:start] + new + text[stop:], start
    return text


# ---------------------------------------------------------------------------
# Prose that names an excluded column
# ---------------------------------------------------------------------------

# Fields holding a name, SQL or a key rather than prose. Each is checked where it is used, against
# the excluded names or by the parser, and blanking one would break the item rather than trim it.
# Every other string a model holds, at any depth, is prose and gets the mention check, so a field
# added to a model later is checked without anyone remembering to list it.
NOT_PROSE = frozenset({
    "name", "schema_name", "storage_connection", "sql", "grain", "unique_keys", "default_filters",
    "parent_table", "child_table", "column_groups", "inherits_columns_from", "columns",
    "signed_off_by", "signed_off_at", "signed_off_role", "foreign_key", "value_transform",
    "bindings", "source_tables", "base_metrics", "subject_areas", "primary_table",
    "non_additive_dimensions", "inner_grain", "from_table", "to_table", "from_column",
    "to_column", "from_schema", "to_schema", "on", "maps_to", "key_components", "parent_entity",
    "table", "column", "where", "indexes",
})


def _is_code(annotation: Any) -> bool:
    """A `Literal` field holds one of a closed set of codes, which no person's prose reaches."""
    if get_origin(annotation) is Literal:
        return True
    return any(_is_code(a) for a in get_args(annotation) if a is not type(None))


def scrub_prose(model: Any, names: frozenset[str]) -> tuple[Any, list[str]]:
    """A copy of `model` with every prose field that mentions one of `names` blanked, and the
    fields blanked (`description`, `performance_hints.recommended_filters`). A string goes blank,
    a list entry or a mapping pair that mentions it goes whole."""
    update: dict[str, Any] = {}
    hit: list[str] = []
    for fname, info in type(model).model_fields.items():
        if fname in NOT_PROSE or _is_code(info.annotation):
            continue
        value = getattr(model, fname)
        new, inner = _scrub_value(value, names, None if info.default is None else "")
        if inner is not None:
            update[fname] = new
            hit += [fname] if not inner else [f"{fname}.{x}" for x in inner]
    return (model.model_copy(update=update) if update else model), hit


def _scrub_value(value: Any, names: frozenset[str], blank: Optional[str]) -> tuple[Any, Optional[list[str]]]:
    """(the scrubbed value, None when nothing mentioned a name, else the nested fields blanked)."""
    if isinstance(value, str):
        return (blank, []) if _mentions(value, names) else (value, None)
    if isinstance(value, list):
        kept = [x for x in value if not _entry_mentions(x, names)]
        return (kept, []) if len(kept) < len(value) else (value, None)
    if isinstance(value, dict):
        kept_map = {k: v for k, v in value.items() if not _entry_mentions((k, v), names)}
        return (kept_map, []) if len(kept_map) < len(value) else (value, None)
    if hasattr(type(value), "model_fields"):
        copy, inner = scrub_prose(value, names)
        return (copy, inner) if inner else (value, None)
    return value, None


def _entry_mentions(entry: Any, names: frozenset[str]) -> bool:
    if isinstance(entry, str):
        return _mentions(entry, names)
    if isinstance(entry, tuple):
        return any(_entry_mentions(x, names) for x in entry)
    if hasattr(type(entry), "model_fields"):
        return bool(scrub_prose(entry, names)[1])
    return False


# ---------------------------------------------------------------------------
# Binding resolution
# ---------------------------------------------------------------------------


def _lacks_binding(metric: Any, engine: str, idx: dict, seen: frozenset = frozenset()) -> bool:
    """Whether the metric, or a metric it is built on, has no binding for `engine`."""
    binding = (metric.bindings or {}).get(engine)
    if binding is None:
        return True
    bases = [idx.get(r) for r in D.binding_refs(binding) if r not in seen]
    return any(b is not None and _lacks_binding(b, engine, idx, seen | {metric.name}) for b in bases)


def resolve_binding(metric: Any, engine: str, idx: dict,
                    org: Datasource) -> tuple[Optional[str], Optional[Reason]]:
    """The metric's SQL for `engine`, `{metric}` references expanded and parsed in its dialect; or
    None and the reason it is skipped. Never a translation of another engine's binding."""
    if _lacks_binding(metric, engine, idx):
        return None, "no_binding_for_target"
    import sqlglot
    from sqlglot.errors import ErrorLevel

    try:
        sql = D.resolve_metric_sql(metric, engine, idx, org)
        sqlglot.parse_one(sql, dialect=_dialect(engine), error_level=ErrorLevel.RAISE)
    except Exception:
        return None, "unparseable_for_target"
    return sql, None


def _metric_sql(metric: Any, idx: dict, org: Datasource) -> list[tuple[str, str]]:
    """(engine, SQL) for every binding, expanded where it expands, so a column reached through a
    base metric is seen in the derived one. A binding that does not expand is read as written,
    where its `{metric}` references make it unparseable, so it counts as naming an excluded name."""
    out = []
    for engine, binding in (metric.bindings or {}).items():
        try:
            out.append((engine, D.resolve_metric_sql(metric, engine, idx, org)))
        except Exception:
            out.append((engine, binding))
    return out


# ---------------------------------------------------------------------------
# Building one view
# ---------------------------------------------------------------------------


@dataclass
class _Builder:
    org: Datasource
    sa: SubjectArea
    target: Target
    options: ExportOptions
    excluded: Exclusions
    idx: dict
    source_engine: str
    losses: list[LossEntry] = field(default_factory=list)
    unreviewed: set[tuple[str, str]] = field(default_factory=set)
    signed: set[tuple[str, str, str]] = field(default_factory=set)

    @property
    def names(self) -> frozenset[str]:
        return self.excluded.names

    @property
    def sql_engine(self) -> Optional[str]:
        return self.source_engine if self.source_engine in self.target.engines else None

    def _add(self, loss: LossEntry) -> None:
        self.losses.append(safe_loss(loss, self.target.name, self.excluded.secret))

    def drop(self, kind: str, name: str, reason: Reason, detail: str) -> None:
        self._add(dropped(self.sa.name, kind, name, reason, detail))

    def degrade(self, kind: str, name: str, reason: Reason, detail: str) -> None:
        self._add(degraded(self.sa.name, kind, name, reason, detail))

    def prose(self, kind: str, name: str, model: Any, lost: tuple[str, ...] = ()) -> Any:
        """`model` with its prose that mentions an excluded name blanked, reported once together
        with the `lost` fields that pointed at one."""
        copy, blanked = scrub_prose(model, self.names)
        parts = [f"its {', '.join(lost)} {_LEFT_HOME}"] if lost else []
        if blanked:
            parts.append(f"its {', '.join(blanked)} {_BLANKED}")
        if parts:
            self.degrade(kind, name, "depends_on_excluded", "; ".join(parts))
        return copy

    def text(self, kind: str, name: str, value: str, what: str = "description") -> str:
        """One free-standing prose field (a description of the datasource or area), or ''."""
        if not _mentions(value, self.names):
            return value
        self.degrade(kind, name, "depends_on_excluded", f"its {what} {_BLANKED}")
        return ""

    def admit(self, kind: str, name: str, approved: bool) -> bool:
        if not approved and not self.options.include_unreviewed:
            self.drop(kind, name, "not_approved", "not approved; --include-unreviewed carries it")
            return False
        return True

    def carry(self, kind: str, name: str, approved: bool, signed_off_at: Any = None) -> None:
        if not approved:
            self.unreviewed.add((kind, name))
        elif signed_off_at:
            self.signed.add((kind, name, _timestamp(signed_off_at)))

    def sql_hit(self, sql: str, engine: Optional[str] = None) -> bool:
        return sql_names_any(sql, self.names, engine or self.source_engine, self.excluded.star_tables)

    def _area_tables(self) -> list[Table]:
        # A table referenced from another area belongs to this one too.
        tables = {bare_name(t.name): t for t in self.sa.tables_defined}
        for ref in self.sa.tables:
            for other in self.org.subject_areas:
                t = other.defined_table(ref.table)
                if t is not None:
                    tables.setdefault(bare_name(t.name), t)
        return sorted(tables.values(), key=lambda t: t.name)

    def tables(self) -> list[ExportTable]:
        out = []
        for t in self._area_tables():
            approved = t.review_state == "approved"
            if not self.admit("table", t.name, approved) or not self._query_travels(t):
                continue
            cols = [c for c in (self._column(t, c) for c in t.columns) if c is not None]
            kept = {c.name for c in cols}
            hints = t.performance_hints
            if hints is not None:
                hints = hints.model_copy(update={
                    "indexes": [i for i in hints.indexes if set(i.columns) <= kept]})
            copy = t.model_copy(update={
                # Part of a composite key is not a key: a trimmed grain would claim uniqueness.
                "grain": t.grain if set(t.grain) <= kept else [],
                "unique_keys": [k for k in t.unique_keys if set(k) <= kept],
                "column_groups": {g: [c for c in cs if c in kept] for g, cs in t.column_groups.items()
                                  if set(cs) & kept},
                "performance_hints": hints,
                "default_filters": self._filters(t), "signed_off_by": None, "signed_off_role": None})
            copy = self.prose("table", t.name, copy).model_copy(update={"columns": cols})
            self.carry("table", t.name, approved, t.signed_off_at)
            withheld = tuple(c.name for c in t.columns
                             if c.sensitive and not self.options.include_sensitive)
            out.append(ExportTable(copy, self._physical(t), withheld))
        return out

    def _query_travels(self, t: Table) -> bool:
        """Whether a table defined by a query may travel. The query is SQL like any other: one that
        reads what stays home stays with it, and one for another engine is never translated."""
        if t.source_type != "sql":
            return True
        if self.sql_hit(t.sql or ""):
            self.drop("table", t.name, "depends_on_excluded", f"its query {_LEFT_HOME}")
            return False
        if self.sql_engine is None:
            self.drop("table", t.name, "sql_not_for_target",
                      f"defined by a query written for {self.source_engine}; SQL is never translated")
            return False
        return True

    def _column(self, t: Table, c: Any) -> Any:
        qual = f"{t.name}.{c.name}"
        if c.sensitive and not self.options.include_sensitive:
            # The name goes through `safe_loss`, which names the column by its id.
            self.drop("column", qual, "sensitive", _WITHHELD)
            if c.choice_field:
                self.drop("choice_field", qual, "depends_on_excluded", "labels of a column left out")
            return None
        approved = c.review_state == "approved"
        if not self.admit("column", qual, approved):
            if c.choice_field:
                self.drop("choice_field", qual, "depends_on_excluded", "labels of a column left out")
            return None
        # What points from this column at another column is cleared when that column stays home.
        refs = (("foreign_key", c.foreign_key), ("denormalized_from", c.denormalized_from))
        lost = [f for f, ref in refs if ref and _mentions(" ".join(filter(None, [
            ref.table, ref.column, ref.where, getattr(ref, "discriminator_column", None),
            *(getattr(ref, "target_tables", None) or [])])), self.names)]
        if c.value_transform and self.sql_hit(c.value_transform):
            lost.append("value_transform")
        self.carry("column", qual, approved, c.signed_off_at)
        # A placeholder's meaning is signed off on its own, and its signer stays home too.
        marks = [p.model_copy(update={"signed_off_by": None, "signed_off_role": None})
                 for p in c.placeholders]
        update = {**dict.fromkeys(lost), "signed_off_by": None, "signed_off_role": None,
                  "placeholders": marks}
        return self.prose("column", qual, c.model_copy(update=update), tuple(lost))

    def _filters(self, t: Table) -> list[str]:
        out = []
        for i, f in enumerate(t.default_filters, 1):
            name = f"{t.name}#{i}"
            if self.sql_hit(f.replace("{alias}", bare_name(t.name))):
                self.drop("default_filter", name, "depends_on_excluded", _LEFT_HOME)
            elif self.sql_engine is None:
                self.drop("default_filter", name, "sql_not_for_target",
                          f"written for {self.source_engine}; SQL is never translated")
            else:
                out.append(f)
        return out

    def _physical(self, t: Table) -> PhysicalName:
        loc = self.options.location
        if self.sql_engine is None and loc is not None:
            return PhysicalName(loc.database, loc.schema, bare_name(t.name))
        return PhysicalName(loc.database if loc else None,
                            t.schema_name or (loc.schema if loc else None), bare_name(t.name))

    def metrics(self) -> list[ExportMetric]:
        out = []
        for m in sorted(self.sa.metrics, key=lambda m: m.name):
            approved = m.review_state == "approved"
            if not self.admit("metric", m.name, approved):
                continue
            resolved: dict[str, str] = {}
            misses: list[tuple[str, Reason]] = []
            for engine in self.target.engines:
                sql, reason = resolve_binding(m, engine, self.idx, self.org)
                if sql is not None:
                    resolved[engine] = sql
                else:
                    misses.append((engine, reason))
            named = {*m.non_additive_dimensions, *m.inner_grain, *m.source_tables}
            if resolved and ({n.casefold() for n in named} & self.names or any(
                    self.sql_hit(sql, engine) for engine, sql in _metric_sql(m, self.idx, self.org))):
                self.drop("metric", m.name, "depends_on_excluded", _LEFT_HOME)
                continue
            details = [_BINDING_DETAIL[reason].format(engine=engine) for engine, reason in misses]
            if not resolved:
                # One metric left home is one entry, however many of the target's engines missed.
                self.drop("metric", m.name, _miss_reason(misses), "; ".join(details))
                continue
            for (_, reason), detail in zip(misses, details):
                # Another engine's SQL still carries the metric on a target with several engines.
                self.degrade("metric", m.name, reason, detail)
            self.carry("metric", m.name, approved, m.signed_off_at)
            copy = m.model_copy(update={"signed_off_by": None, "signed_off_role": None})
            written = {e: (m.bindings or {})[e] for e in resolved if e in (m.bindings or {})}
            out.append(ExportMetric(self.prose("metric", m.name, copy), resolved, written))
        return out

    def relationships(self) -> list[ExportRelationship]:
        out, seen = [], {}
        for r in self.sa.relationships:
            base = f"{r.from_table}->{r.to_table}"
            seen[base] = seen.get(base, 0) + 1
            name = base if seen[base] == 1 else f"{base}#{seen[base]}"
            approved = r.review_state == "approved"
            if not self.admit("relationship", name, approved):
                continue
            ends = {x.casefold() for x in (r.from_column, r.to_column, bare_name(r.from_table),
                                            bare_name(r.to_table)) if x}
            if ends & self.names or (r.on and self.sql_hit(r.on)):
                self.drop("relationship", name, "depends_on_excluded", _LEFT_HOME)
            elif r.on and self.sql_engine is None:
                self.drop("relationship", name, "sql_not_for_target",
                          f"its on: is written for {self.source_engine}; SQL is never translated")
            else:
                self.carry("relationship", name, approved, r.signed_off_at)
                copy = r.model_copy(update={"signed_off_by": None, "signed_off_role": None})
                out.append(ExportRelationship(name, self.prose("relationship", name, copy)))
        return sorted(out, key=lambda r: r.name)

    def entities(self) -> list[Any]:
        out = []
        for e in sorted(self.sa.entities, key=lambda e: e.name):
            approved = e.review_state == "approved"
            if not self.admit("entity", e.name, approved):
                continue
            maps = [mp for mp in e.maps_to if not _mentions(f"{mp.table} {mp.column}", self.names)
                    and not (mp.default_filter and self.sql_hit(mp.default_filter))]
            if e.maps_to and not maps:
                self.drop("entity", e.name, "depends_on_excluded", f"every mapping {_LEFT_HOME}")
                continue
            if len(maps) < len(e.maps_to):
                self.degrade("entity", e.name, "depends_on_excluded", f"a mapping {_LEFT_HOME}")
            keys = [k for k in e.key_components if k.casefold() not in self.names]
            self.carry("entity", e.name, approved, e.signed_off_at)
            out.append(self.prose("entity", e.name, e.model_copy(update={
                "maps_to": maps, "key_components": keys, "signed_off_by": None,
                "signed_off_role": None})))
        return out

    def question(self, kind: str, name: str, question: str, sql: Optional[str], approved: bool,
                 prose: list[str], tables: tuple[str, ...],
                 signed_off_at: Any = None) -> Optional[ExportQuestion]:
        if not self.admit(kind, name, approved):
            return None
        if _mentions(" ".join([question, *prose]), self.names) or (sql and self.sql_hit(sql)):
            self.drop(kind, name, "depends_on_excluded", _LEFT_HOME)
            return None
        if sql and self.sql_engine is None:
            self.degrade(kind, name, "sql_not_for_target",
                         f"carried as its question only; its SQL is for {self.source_engine}")
            sql = None
        self.carry(kind, name, approved, signed_off_at)
        return ExportQuestion(kind, name, question, sql, tables)

    def examples(self, raw: list[dict]) -> list[ExportQuestion]:
        out = []
        for ex in sorted((e for e in raw if isinstance(e, dict) and e.get("question")),
                         key=lambda e: str(e["question"])):
            approved = ex.get("status") == "confirmed" or ex.get("review_state") == "approved"
            tables = tuple(str(t) for t in ex.get("tables") or [])
            prose = [str(x) for x in (*tables, *(ex.get("columns") or []))]
            q = self.question("example", str(ex["question"]), str(ex["question"]), ex.get("sql"),
                              approved, prose, tables, ex.get("signed_off_at"))
            if q:
                out.append(q)
        return out

    def golden(self, items: list[tuple[str, GoldenItem]]) -> list[ExportQuestion]:
        out = []
        for dataset, item in sorted(items, key=lambda p: f"{p[0]}:{p[1].id}"):
            want = item.expected
            q = self.question("golden", f"{dataset}:{item.id}", item.query, want.sql,
                              want.sql_confirmed, [*want.tables_used, *item.must_filter],
                              tuple(want.tables_used),
                              item.confirmed_by.at if item.confirmed_by else None)
            if q:
                out.append(self._golden_detail(q, item))
        return out

    def _golden_detail(self, q: ExportQuestion, item: GoldenItem) -> ExportQuestion:
        """`q` with the item's answer-key detail. `must_filter` was cleared with the question; the
        prose fields are checked here and travel blank when one mentions a name left home."""
        want = item.expected
        texts = {"expected.chart_type": want.chart_type, "expected.data_shape": want.data_shape,
                 "expected.validation_notes": want.validation_notes}
        blanked = [k for k, v in texts.items() if v and _mentions(v, self.names)]
        tags = tuple(t for t in item.tags if not _mentions(t, self.names))
        if len(tags) < len(item.tags):
            blanked.append("tags")
        if blanked:
            self.degrade("golden", q.name, "depends_on_excluded", f"its {', '.join(blanked)} {_BLANKED}")
        kept = {k: (None if k in blanked else v) for k, v in texts.items()}
        return dataclasses.replace(
            q, match=item.match, bounds=item.bounds, must_filter=tuple(item.must_filter), tags=tags,
            chart_type=kept["expected.chart_type"], data_shape=kept["expected.data_shape"],
            validation_notes=kept["expected.validation_notes"],
            detail_fields=tuple(uncarried_golden_fields(item)))

    def ambiguities(self) -> list[Any]:
        tables = {bare_name(t.name).casefold() for t in self._area_tables()}
        metrics = {m.name for m in self.sa.metrics}
        out = []
        for a in sorted(self.org.ambiguities, key=lambda a: a.name):
            # `columns` and `column_groups` are written `table.<name>`; `applies_to` names tables.
            anchors = {bare_name(x).casefold() for x in a.applies_to} | {
                bare_name(x.rsplit(".", 1)[0]).casefold() for x in (*a.columns, *a.column_groups)}
            if a.anchored() and not (anchors & tables or set(a.metrics) & metrics):
                continue  # about another area
            if _mentions(json.dumps(a.model_dump(), sort_keys=True), self.names):
                self.drop("ambiguity", a.name, "depends_on_excluded", _LEFT_HOME)
            else:
                out.append(a)
        return out

    def terminology(self) -> list[tuple[str, str]]:
        out = []
        for term in sorted(self.org.key_terminology):
            if _mentions(f"{term} {self.org.key_terminology[term]}", self.names):
                self.drop("terminology", term, "depends_on_excluded", _LEFT_HOME)
            else:
                out.append((term, self.org.key_terminology[term]))
        return out


_BINDING_DETAIL = {
    "no_binding_for_target": "no {engine} binding; add one by hand, since SQL is never translated",
    "unparseable_for_target": "its {engine} binding does not expand or parse in that dialect; "
                              "`sm validate` names the problem",
}


def _miss_reason(misses: list[tuple[str, Reason]]) -> Reason:
    # A binding that exists but fails is the one a person can fix in place, so it names the entry
    # when the engines missed for different reasons; the detail still names each engine.
    reasons = {reason for _, reason in misses}
    return "no_binding_for_target" if reasons == {"no_binding_for_target"} else "unparseable_for_target"


def build_view(org: Datasource, sa: SubjectArea, target: Target, options: ExportOptions,
               source_engine: str, examples: list[dict], golden: list[tuple[str, GoldenItem]],
               excluded: Optional[Exclusions] = None) -> tuple[ExportView, list[LossEntry]]:
    """The eligible part of `sa` for `target`, and the losses deciding it cost. `excluded` is
    computed from the model when not given; a run computes it once for every target."""
    b = _Builder(org, sa, target, options, excluded or exclusions(org, options, source_engine),
                 D.metric_index(org), source_engine)
    view = ExportView(
        target=target.name, engines=target.engines, source_engine=source_engine,
        sql_engine=b.sql_engine, datasource=org.datasource,
        description=b.text("datasource", org.datasource, org.description),
        area=sa.name, area_description=b.text("subject_area", sa.name, sa.description),
        tables=tuple(b.tables()),
        metrics=tuple(b.metrics()), relationships=tuple(b.relationships()),
        entities=tuple(b.entities()), examples=tuple(b.examples(examples)),
        golden=tuple(b.golden(golden)), ambiguities=tuple(b.ambiguities()),
        terminology=tuple(b.terminology()), unreviewed=frozenset(b.unreviewed),
        fiscal_year_start_month=org.fiscal_year_start_month,
        default_time_window=b.text("subject_area", sa.name, sa.default_time_window or "",
                                   "default_time_window") or None,
        signed=frozenset(b.signed))
    return view, b.losses


def _timestamp(value: Any) -> str:
    # YAML reads an unquoted timestamp in a free-form example file as a date or datetime.
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def golden_detail_losses(view: ExportView, carried: Mapping[str, frozenset[str]]) -> list[LossEntry]:
    """One `detail_lost` entry per golden item holding detail the target's files did not carry:
    its `detail_fields` less what the writer declared in `carried`. Raises on a declared field
    name that is not one of `GOLDEN_DETAIL_FIELDS` or an item the view does not hold, since
    either would make the report's account of the item wrong without anyone seeing it."""
    names = {q.name for q in view.golden}
    for name, fields in carried.items():
        unknown = sorted(set(fields) - set(GOLDEN_DETAIL_FIELDS))
        if name not in names or unknown:
            raise ValueError(f"writer {view.target} declared carried golden detail it cannot "
                             f"carry: {name!r} {unknown}")
    out = []
    for q in view.golden:
        kept_home = [f for f in q.detail_fields if f not in carried.get(q.name, frozenset())]
        if kept_home:
            out.append(degraded(view.area, "golden", q.name, "detail_lost", f"its "
                                f"{', '.join(kept_home)} has no field in this target's files and "
                                "stays in agami"))
    return out


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


def source_engine(org: Datasource) -> str:
    engines = {sc.storage_type for sc in org.storage_connections}
    if len(engines) != 1:
        raise ExportRefused(f"the semantic model declares {len(engines)} storage engines; an export "
                            "needs exactly one, so that its SQL is written in one dialect")
    return engines.pop()


def _sql_tables(sql: str, engine: str) -> set[str]:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel

    try:
        tree = sqlglot.parse_one(sql, dialect=_dialect(engine), error_level=ErrorLevel.RAISE)
    except Exception:
        return set()
    ctes = {c.alias_or_name.casefold() for c in tree.find_all(exp.CTE)}
    return {t.name.casefold() for t in tree.find_all(exp.Table)} - ctes


def uncarried_golden_fields(item: GoldenItem) -> list[str]:
    """The fields of a golden item beyond its question and SQL: how its answer is compared, what
    it must filter on, and its notes. The core reports them as `detail_lost` for each target, less
    those the target's writer declares carried. `match` counts only when it is not the default
    `exact`; the signer is in DOES_NOT_TRAVEL."""
    want = item.expected
    present = {"match": item.match != "exact", "bounds": item.bounds is not None,
               "must_filter": bool(item.must_filter), "tags": bool(item.tags),
               "recorded": item.recorded is not None, "expected.chart_type": bool(want.chart_type),
               "expected.data_shape": bool(want.data_shape),
               "expected.validation_notes": bool(want.validation_notes)}
    return [k for k, v in present.items() if v]


def golden_load_losses(findings: Any) -> list[LossEntry]:
    """Each golden file or case that did not load, as a loss: left alone, a broken file would just
    be missing from the export with nothing said. There is no reason code for "the model's own file
    does not load", so the closest one, a file or case that does not parse, stands in."""
    return [dropped("", "golden", f.locator or f.code, "unparseable_for_target",
                    f"it does not load, so it is not exported: {f.message}")
            for f in findings.findings if f.severity == "error"]


def assign_golden(org: Datasource, datasets: list, engine: str
                  ) -> tuple[dict[str, list[tuple[str, GoldenItem]]], list[LossEntry]]:
    """Golden datasets are kept per profile, not per subject area, so each item goes to the first
    area (by name) holding every table it reads: `expected.tables_used`, else the tables its SQL
    reads. One the areas cannot hold alone is reported as `spans_subject_areas`."""
    areas = sorted(org.subject_areas, key=lambda s: s.name)
    held = [(sa.name, {bare_name(t.name).casefold() for t in sa.tables_defined}
             | {bare_name(r.table).casefold() for r in sa.tables}) for sa in areas]
    by_area: dict[str, list[tuple[str, GoldenItem]]] = {sa.name: [] for sa in areas}
    losses = []
    for ds in datasets:
        for item in ds.test_cases:
            tables = ({bare_name(t).casefold() for t in item.expected.tables_used}
                      or _sql_tables(item.expected.sql or "", engine))
            home = held[0][0] if len(held) == 1 else next(
                (a for a, names in held if tables and tables <= names), None)
            if home is None:
                losses.append(dropped("", "golden", f"{ds.name}:{item.id}", "spans_subject_areas",
                                      "no one subject area holds every table it reads"))
            else:
                by_area[home].append((ds.name, item))
    return by_area, losses


def cross_area_losses(org: Datasource) -> list[LossEntry]:
    why = "it belongs to no one subject area, and each subject area exports alone"
    rels = [f"{r.from_subject_area}/{r.from_table}->{r.to_subject_area}/{r.to_table}"
            for r in org.cross_subject_area_relationships]
    return [dropped("", kind, name, "spans_subject_areas", why) for kind, names in (
        ("relationship", rels), ("entity", [e.name for e in org.cross_subject_area_entities]),
        ("metric", [m.name for m in org.cross_subject_area_metrics])) for name in names]


def _resolve_targets(names: tuple[str, ...]) -> list[Target]:
    known = targets()
    unknown = [n for n in names if n not in known]
    if unknown or not names:
        offered = ", ".join(sorted(known)) or "none, since no target writer is installed yet"
        raise ExportRefused(f"unknown target {', '.join(unknown) or '(none given)'}; "
                            f"--target takes one of: {offered}")
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        # Refused rather than folded into one, so the report's options say what actually ran.
        raise ExportRefused(f"--target {', '.join(repeated)} is given more than once; "
                            "name each target once")
    return [known[n] for n in names]


def _load(root: Path) -> Datasource:
    try:
        return load_datasource(root)
    except Exception as exc:
        raise ExportRefused(f"the semantic model at {root} does not load ({type(exc).__name__}); "
                            f"run `sm validate {root}` and fix what it reports") from exc


def _select_areas(org: Datasource, wanted: tuple[str, ...]) -> list[SubjectArea]:
    have = {sa.name for sa in org.subject_areas}
    unknown = [a for a in wanted if a not in have]
    if unknown:
        raise ExportRefused(f"no subject area {', '.join(unknown)}; "
                            f"--area takes one of: {', '.join(sorted(have))}")
    chosen = [sa for sa in org.subject_areas if not wanted or sa.name in wanted]
    return sorted(chosen, key=lambda s: s.name)


def _require_location(tgts: list[Target], engine: str, options: ExportOptions) -> None:
    for t in tgts:
        if engine not in t.engines and options.location is None:
            raise ExportRefused(
                f"target {t.name} runs on {', '.join(t.engines)} but the model is written for "
                f"{engine}, so where its tables live there is unknown; "
                "pass --location <database>.<schema>")


def _safe_path(path: str) -> str:
    p = PurePosixPath(path)
    if p.is_absolute() or not p.parts or ".." in p.parts or "\\" in path:
        raise ValueError(f"a writer may write only under its own directory, not {path!r}")
    return p.as_posix()


_CARRIED_KINDS = ("table", "column", "default_filter", "metric", "relationship", "entity", "example",
                  "golden", "ambiguity", "terminology")


def _carried(view: ExportView) -> dict[str, int]:
    return {"table": len(view.tables), "column": sum(len(t.table.columns) for t in view.tables),
            "default_filter": sum(len(t.table.default_filters) for t in view.tables),
            "metric": len(view.metrics), "relationship": len(view.relationships),
            "entity": len(view.entities), "example": len(view.examples), "golden": len(view.golden),
            "ambiguity": len(view.ambiguities), "terminology": len(view.terminology)}


def _export_target(t: Target, org: Datasource, areas: list[SubjectArea], engine: str,
                   options: ExportOptions, examples: dict[str, list], golden: dict[str, list],
                   shared_losses: list[LossEntry], excluded: Exclusions
                   ) -> tuple[dict[str, bytes], dict[str, Any]]:
    files: dict[str, bytes] = {}
    # Casefolded path -> the subject area that wrote it. Casefolded because the output is often
    # checked out on a file system that ignores case, where `Sales.sql` and `sales.sql` are one file.
    owner: dict[str, str] = {}
    losses = [safe_loss(x, t.name, excluded.secret) for x in shared_losses]
    unreviewed: list[dict[str, str]] = []
    carried = dict.fromkeys(_CARRIED_KINDS, 0)
    built = [build_view(org, sa, t, options, engine, examples[sa.name], golden[sa.name], excluded)
             for sa in areas]
    first: dict[str, str] = {}
    for sa, (view, view_losses) in zip(areas, built):
        claimed = {k: first[k] for k in (physical_key(et.physical) for et in view.tables
                                         if et.table.source_type != "sql") if k in first}
        for et in view.tables:
            if et.table.source_type != "sql":
                first.setdefault(physical_key(et.physical), sa.name)
        view = dataclasses.replace(view, claimed=claimed)
        out = t.write(view, options)
        for path, data in out.files.items():
            rel = _safe_path(path)
            key = rel.casefold()
            if key in owner and owner[key] != sa.name:
                # Two area names one target's naming rule maps alike (`a-b`, `a_b`). Refused, not
                # dropped: every file is still in memory here, so nothing is half-written, and one
                # area's files without the other's would refer to what is not there.
                raise ExportRefused(
                    f"subject areas {owner[key]} and {sa.name} both export to {t.name}/{rel}, since "
                    f"{t.name} names them alike; rename one, or export each alone with --area "
                    "into its own --out")
            if key in owner:
                raise ValueError(f"writer {t.name} wrote {rel} twice")
            files[rel] = data
            owner[key] = sa.name
        losses += view_losses + [safe_loss(x, t.name, excluded.secret) for x in (
            *out.losses, *golden_detail_losses(view, out.carried_golden_fields))]
        unreviewed += [{"area": sa.name, "kind": k, "name": n} for k, n in sorted(view.unreviewed)]
        for kind, n in _carried(view).items():
            carried[kind] += n
        # What a writer drops was counted in its view, so it comes off the carried count, once per
        # item however many of the target's files report it (Databricks writes three).
        for kind, _ in {(x.kind, x.name) for x in out.losses if x.outcome == "dropped"}:
            if kind in carried:
                carried[kind] = max(0, carried[kind] - 1)
    section = {"files": sorted(f"{t.name}/{p}" for p in files), "carried": carried,
               "losses": [x.to_dict() for x in sorted(losses, key=_loss_key)],
               "included_unreviewed": unreviewed}
    return files, section


def _loss_key(x: LossEntry) -> tuple[str, ...]:
    return (x.area, x.kind, x.name, x.reason, x.outcome, x.detail)


def report_text(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _overlap(a: Path, b: Path) -> Optional[str]:
    """How resolved path `a` relates to resolved path `b`, when one holds the other."""
    if a == b:
        return "is"
    if b in a.parents:
        return "lies inside"
    if a in b.parents:
        return "holds"
    return None


def _check_out(root: Path, out: Path, tgts: list[Target]) -> None:
    """Refuse an `--out` that would write into, or replace, what the export reads. `<out>/<target>/`
    is removed whole on every run, so with the model at `par/snowflake`, `--out par --target
    snowflake` would delete it; and the export never modifies the model, so `--out` inside it is
    refused too. Paths are resolved, so a symbolic link is judged by where it points."""
    model = {"the semantic model": root.resolve()}
    for sub in ("golden_datasets", "prompt_examples"):
        # Read from inside the model, but either may be a link to a directory kept elsewhere.
        model[f"the model's {sub} directory"] = (root / sub).resolve()
    written = {f"--out {out}": out.resolve()}
    for t in tgts:
        written[f"{out / t.name}"] = (out / t.name).resolve()
        written[f"{out / t.name} (once replaced)"] = out.resolve() / t.name
    for what, path in written.items():
        for read, home in model.items():
            relation = _overlap(path, home)
            if relation:
                raise ExportRefused(
                    f"{what} {relation} {read} ({home}); the export replaces <out>/<target>/ whole "
                    "and never writes into the model, so pass an --out that neither holds the "
                    "model nor lies inside it")
    existing = next((p for p in (out, *out.parents) if p.exists() or p.is_symlink()), None)
    if existing is not None and not existing.is_dir():
        raise ExportRefused(f"--out {out}: {existing} exists and is not a directory; "
                            "pass a directory, or a path that does not exist yet")


REPORT_FILE = "export-report.json"


def _stage(staging: Path, results: dict[str, dict[str, bytes]], report: str) -> None:
    for name, files in results.items():
        tdir = staging / name
        tdir.mkdir()
        for rel, data in sorted(files.items()):
            (tdir / rel).parent.mkdir(parents=True, exist_ok=True)
            (tdir / rel).write_bytes(data)
    (staging / REPORT_FILE).write_text(report, encoding="utf-8")


def _swap(out: Path, staging: Path, names: list[str]) -> None:
    """Move each staged entry into `out`, setting aside what it replaces; on a failure put every
    entry back as it was. A rename within one directory either happens or does not, so `out`
    holds the old output or the new, never a mix."""
    old = staging / ".replaced"
    old.mkdir()
    done: list[tuple[str, bool]] = []
    try:
        for name in names:
            had = (out / name).exists() or (out / name).is_symlink()
            if had:
                (out / name).rename(old / name)
            done.append((name, had))
            (staging / name).rename(out / name)
    except OSError:
        for name, had in reversed(done):
            if (out / name).exists() or (out / name).is_symlink():
                (out / name).rename(staging / name)
            if had:
                (old / name).rename(out / name)
        raise


def _write(out: Path, results: dict[str, dict[str, bytes]], report: str) -> None:
    """Write every target's directory and the report, all or nothing. Each `<out>/<target>/` is
    replaced whole, so an item the model no longer has does not linger in old output. Everything
    is written first into a directory beside them and only then moved in, so a write that fails
    (a name the file system refuses, a full disk) leaves `--out` as it was and is refused like any
    other problem, instead of leaving half a new export beside an old report."""
    created = not out.exists()
    staging: Optional[Path] = None
    try:
        out.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".sm-export-", dir=out))
        _stage(staging, results, report)
        _swap(out, staging, [*results, REPORT_FILE])
    except OSError as exc:
        what = f"{exc.strerror or type(exc).__name__}"
        name = Path(exc.filename).name if exc.filename else ""
        where = f": {name[:60]}{'...' if len(name) > 60 else ''}" if name else ""
        raise ExportRefused(f"could not write the export under {out} ({what}{where}); nothing "
                            "under --out was changed. A file name too long for the file system "
                            "comes from a long subject area, table or metric name: shorten it, or "
                            "pass an --out on a file system that takes it") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if created and out.is_dir() and not any(out.iterdir()):
            out.rmdir()


class _FlagRecorder:
    """Stands in for the `sm export` parser while a target declares its flags, so the core learns
    which target each flag belongs to without parsing anything."""

    def __init__(self, target: str, owners: dict[str, tuple[str, str, Any]]) -> None:
        self.target, self.owners = target, owners

    def add_argument(self, *flags: str, **kwargs: Any) -> None:
        dest = kwargs.get("dest") or next(
            f for f in flags if f.startswith("--"))[2:].replace("-", "_")
        self.owners[dest] = (self.target, flags[0], kwargs.get("default"))


def flag_owners() -> dict[str, tuple[str, str, Any]]:
    """Each target flag's argparse dest -> (the target it belongs to, the flag as typed, its
    default). A Databricks artifact's flag belongs to `databricks`, which writes it."""
    owners: dict[str, tuple[str, str, Any]] = {}
    for t in targets().values():
        if t.add_arguments is not None:
            t.add_arguments(_FlagRecorder(t.name, owners))  # type: ignore[arg-type]
    return owners


def _check_target_args(options: ExportOptions) -> None:
    # Refused, not ignored: the report records every flag given, and one for a target this run
    # does not write would read there as applied.
    owners = flag_owners()
    for dest, value in sorted(options.target_args.items()):
        target, flag, default = owners.get(dest, (None, "", None))
        # Every flag a writer ships is suppressed unless given; one with a default counts as given
        # only when it differs from it.
        if target is not None and target not in options.targets and value != default:
            raise ExportRefused(f"{flag} belongs to --target {target}, which this export does not "
                                f"select; add --target {target}, or drop {flag}")


def run_export(root: Path, out: Path, options: ExportOptions) -> str:
    """Export the model at `root` into `out` and return the report's text. Every refusal is raised
    before the first write, and nothing under `root` is written."""
    tgts = _resolve_targets(options.targets)
    _check_target_args(options)
    _check_out(root, out, tgts)
    org = _load(root)
    areas = _select_areas(org, options.areas)
    engine = source_engine(org)
    _require_location(tgts, engine, options)
    datasets, findings = load_golden_datasets(root.name, root.parent)
    golden, golden_losses = assign_golden(org, datasets, engine)
    golden_losses = golden_load_losses(findings) + golden_losses
    examples = {sa.name: list_prompt_examples(root, sa.name) for sa in areas}
    shared = cross_area_losses(org) + golden_losses
    excluded = exclusions(org, options, engine)
    results, sections = {}, {}
    for t in tgts:
        results[t.name], sections[t.name] = _export_target(t, org, areas, engine, options, examples,
                                                           golden, shared, excluded)
    report = report_text({
        "source": {"datasource": org.datasource, "engine": engine, "areas": [sa.name for sa in areas]},
        "options": options.to_report(), "targets": sections, "does_not_travel": list(DOES_NOT_TRAVEL)})
    _write(out, results, report)
    return report


# ---------------------------------------------------------------------------
# `sm export`
# ---------------------------------------------------------------------------

_CORE_DESTS = frozenset({"command", "func", "root", "targets", "out", "areas", "include_unreviewed",
                         "include_sensitive", "location"})


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("root")
    sp.add_argument("--target", action="append", default=[], dest="targets", metavar="TARGET",
                    help="a target format; repeat for several")
    sp.add_argument("--out", required=True, help="directory; each run replaces <out>/<target>/ whole")
    sp.add_argument("--area", action="append", default=[], dest="areas",
                    help="export only this subject area; repeat for several (default: every area)")
    sp.add_argument("--include-unreviewed", action="store_true",
                    help="carry items not yet approved too; the report lists each one")
    sp.add_argument("--include-sensitive", action="store_true",
                    help="carry columns flagged sensitive, and what names them")
    sp.add_argument("--location", default=None, metavar="DATABASE.SCHEMA",
                    help="where the tables live on the target, when it runs another engine")
    for t in targets().values():
        if t.add_arguments is not None:
            t.add_arguments(sp)


def options_from_args(args: argparse.Namespace) -> ExportOptions:
    """The options `add_arguments` parsed; a flag a target added lands in `target_args`. Raises
    `ExportRefused` for a malformed `--location`."""
    return ExportOptions(
        targets=tuple(args.targets), areas=tuple(args.areas),
        include_unreviewed=args.include_unreviewed, include_sensitive=args.include_sensitive,
        location=parse_location(args.location) if args.location else None,
        target_args={k: v for k, v in vars(args).items() if k not in _CORE_DESTS})
