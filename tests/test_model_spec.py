"""Unit tests for semantic_model/model_spec.py — applying a person's model spec (subject areas, table
ownership, joins, metrics, sensitive columns, rules) over an introspected model as one validated step."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import loader, model_spec, validator  # noqa: E402

COLS = {
    "sales_f": [
        "sale_key",
        "customer_key",
        "ship_to_customer_key",
        "customer_ntrl_key",
        "sale_date_key",
        "amount",
    ],
    "customer_d": ["customer_key", "customer_ntrl_key", "name", "email", "region", "current_flag"],
    "date_d": ["date_key", "full_date", "year"],
    "region_mgr_d": ["region", "manager_name"],
    "audit_log": ["id", "note"],
}


def _introspected(root: Path) -> None:
    """What introspection leaves on a star of views: tables in prefix areas and no joins."""
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "acme",
                "version": 1,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": ["subject_areas/sales", "subject_areas/misc"],
            }
        )
    )
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )
    areas = {"sales": ["sales_f", "date_d"], "misc": ["customer_d", "region_mgr_d", "audit_log"]}
    for area, ts in areas.items():
        (root / "subject_areas" / area / "tables").mkdir(parents=True)
        (root / "subject_areas" / area / "subject_area.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": area,
                    "tables": [
                        {"storage_connection": "c", "schema": "sales_data", "table": t} for t in ts
                    ],
                }
            )
        )
        for t in ts:
            (root / "subject_areas" / area / "tables" / f"{t}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "name": t,
                        "schema": "sales_data",
                        "storage_connection": "c",
                        "columns": [
                            {"name": c, "type": "integer" if c.endswith("key") else "string"}
                            for c in COLS[t]
                        ],
                    }
                )
            )
    (root / "datasource.md").write_text("# About this database\n\nA demo shop.\n")


def _spec(**over) -> dict:
    spec = {
        "subject_areas": [
            {"name": "orders", "description": "What was sold, to whom, when."},
            {"name": "regions", "description": "Who manages the customer's region."},
        ],
        "tables": [
            {"table": "sales_f", "owner": "orders", "also_in": ["regions"]},
            {"table": "customer_d", "owner": "orders", "also_in": ["regions"]},
            {"table": "date_d", "owner": "orders"},
            {"table": "region_mgr_d", "owner": "regions"},
        ],
        "joins": [
            {
                "from_table": "sales_f",
                "from_column": "customer_key",
                "to_table": "customer_d",
                "to_column": "customer_key",
                "role": "Customer (as at the time)",
                "approved": "yes",
            },
            {
                "from_table": "sales_f",
                "from_column": "ship_to_customer_key",
                "to_table": "customer_d",
                "to_column": "customer_key",
                "role": "Ship-to customer",
                "approved": "no",
            },
            {
                "from_table": "sales_f",
                "to_table": "customer_d",
                "role": "Customer (current)",
                "on": "sales_f.customer_ntrl_key = customer_d.customer_ntrl_key AND customer_d.current_flag = 'Y'",
                "approved": "yes",
            },
            {
                "from_table": "sales_f",
                "from_column": "sale_date_key",
                "to_table": "date_d",
                "to_column": "date_key",
                "role": "Sale date",
            },
            {
                "from_table": "customer_d",
                "from_column": "region",
                "to_table": "region_mgr_d",
                "to_column": "region",
                "role": "Region manager",
            },
        ],
        "metrics": [
            {
                "name": "Sales Amount",
                "area": "orders",
                "definition": "Total amount sold.",
                "calculation": "SUM(amount)",
                "source_tables": ["sales_f"],
                "approved": "yes",
            }
        ],
        "sensitive_columns": [{"table": "customer_d", "column": "email"}],
        "columns": [
            {
                "table": "sales_f",
                "column": "amount",
                "name": "Net Sales",
                "description": "Amount after discounts, in USD.",
            }
        ],
        "rules": [{"rule": "Unknown members", "detail": "Key -1 means Unknown."}],
    }
    spec.update(over)
    return spec


def test_applies_areas_joins_metrics_sensitive_and_rules(tmp_path):
    _introspected(tmp_path)
    res = model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com")
    assert res.applied and not res.errors, res.errors

    org = loader.load_datasource(tmp_path, include_rejected=True)
    assert not validator.validate(org).errors
    areas = {sa.name: sa for sa in org.subject_areas}
    assert set(areas) == {"orders", "regions"}
    # owned once, listed where needed: the shared fact and dimension are referenced by `regions`
    assert {t.name for t in areas["orders"].tables_defined} == {"sales_f", "customer_d", "date_d"}
    assert {t.name for t in areas["regions"].tables_defined} == {"region_mgr_d"}
    assert {r.table for r in areas["regions"].tables} == {"sales_f", "customer_d", "region_mgr_d"}
    # a table the spec does not name is dropped
    assert "audit_log" not in {t.name for sa in org.subject_areas for t in sa.tables_defined}

    rels = areas["orders"].relationships
    # joins between tables one area owns stay in it; customer_d (orders) -> region_mgr_d (regions)
    # crosses owners, so it is a cross-area join — the only place the runtime serves it
    assert len(rels) == 4 and not areas["regions"].relationships
    (x,) = org.cross_subject_area_relationships
    assert (x.from_subject_area, x.to_subject_area, x.description) == (
        "orders",
        "regions",
        "Region manager",
    )
    served = loader.load_datasource(tmp_path)  # the answering path drops what it cannot serve
    assert (
        sum(len(sa.relationships) for sa in served.subject_areas)
        + len(served.cross_subject_area_relationships)
        == 5
    )
    current = next(r for r in rels if r.on)
    assert current.from_column is None and "current_flag = 'Y'" in current.on
    assert current.description == "Customer (current)"
    approved = [r for r in rels if r.review_state == "approved"]
    assert len(approved) == 2
    assert all(r.signed_off_by == "you@example.com" and r.signed_off_at for r in approved)
    assert sum(r.review_state == "unreviewed" for r in rels) == 2

    metric = areas["orders"].metrics[0]
    assert metric.name == "sales_amount" and metric.bindings == {"PostgreSQL": "SUM(amount)"}
    assert metric.review_state == "approved"
    # the definition is the description, so the metric is listed to the answering model
    assert metric.description == "Total amount sold." and metric.other_names == ["Sales Amount"]
    assert not any("metric_index" in n for n in res.notes)
    email = next(
        c for c in areas["orders"].defined_table("customer_d").columns if c.name == "email"
    )
    assert email.sensitive

    md = (tmp_path / "datasource.md").read_text()
    assert "A demo shop." in md and "Key -1 means Unknown." in md
    assert (
        res.counts["joins_with_on_condition"] == 1 and res.counts["tables_dropped_not_in_spec"] == 1
    )


def test_reapplying_replaces_rules_without_duplicating(tmp_path):
    _introspected(tmp_path)
    model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com")
    res = model_spec.apply_spec(
        tmp_path,
        _spec(rules=[{"rule": "Dates", "detail": "Keys are day numbers."}]),
        signer="you@example.com",
    )
    assert res.applied, res.errors
    md = (tmp_path / "datasource.md").read_text()
    assert md.count("## Rules") == 1 and "day numbers" in md and "Key -1" not in md


def test_dry_run_writes_nothing(tmp_path):
    _introspected(tmp_path)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    res = model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com", dry_run=True)
    assert not res.applied and not res.errors and res.counts["joins"] == 5
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before


def test_every_problem_is_reported_at_once_and_nothing_is_written(tmp_path):
    _introspected(tmp_path)
    bad = _spec(
        tables=_spec()["tables"]
        + [{"table": "missing_d", "owner": "orders"}, {"table": "audit_log", "owner": "nowhere"}],
        joins=[
            {
                "from_table": "sales_f",
                "from_column": "no_such_col",
                "to_table": "date_d",
                "to_column": "date_key",
            },
            {
                "from_table": "sales_f",
                "from_column": "sale_date_key",
                "to_table": "date_d",
                "to_column": "date_key",
                "approved": "yes",
            },
        ],
    )
    res = model_spec.apply_spec(tmp_path, bad)  # no signer
    text = "\n".join(res.errors)
    assert not res.applied
    assert "missing_d" in text and "not in the introspected model" in text
    assert "'nowhere' is not a declared subject area" in text
    assert "sales_f.no_such_col does not exist" in text
    assert "needs --signer" in text
    assert {sa.name for sa in loader.load_datasource(tmp_path).subject_areas} == {"sales", "misc"}


def test_cli_apply_spec(tmp_path, capsys):
    from semantic_model import cli

    _introspected(tmp_path)
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(_spec()))
    rc = cli.main(
        ["apply-spec", str(tmp_path), "--file", str(spec_file), "--signer", "you@example.com"]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["applied"] and out["counts"]["joins_approved"] == 2


# --- the workbook a person fills in -> the spec -----------------------------------------------------


def _xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> str:
    """A minimal workbook of inline-string cells, one worksheet per entry."""
    import zipfile
    from xml.sax.saxutils import escape

    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"

    def col(i: int) -> str:
        s, i = "", i + 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    with zipfile.ZipFile(path, "w") as book:
        for n, rows in enumerate(sheets.values(), 1):
            body = "".join(
                f'<row r="{r}">'
                + "".join(
                    f'<c r="{col(i)}{r}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'
                    for i, v in enumerate(cells)
                    if v != ""
                )
                + "</row>"
                for r, cells in enumerate(rows, 1)
            )
            book.writestr(
                f"xl/worksheets/sheet{n}.xml",
                f'<worksheet xmlns="{main}"><sheetData>{body}</sheetData></worksheet>',
            )
        entries = "".join(
            f'<sheet name="{escape(s)}" sheetId="{n}" r:id="rId{n}"/>'
            for n, s in enumerate(sheets, 1)
        )
        book.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>{entries}</sheets></workbook>',
        )
        rels = "".join(
            f'<Relationship Id="rId{n}" Type="{rel}/worksheet" Target="worksheets/sheet{n}.xml"/>'
            for n in range(1, len(sheets) + 1)
        )
        book.writestr(
            "xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{pkg}">{rels}</Relationships>'
        )
        book.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" '
            f'Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
    return str(path)


def _filled_template(tmp_path: Path) -> str:
    return _xlsx(
        tmp_path / "spec.xlsx",
        {
            "How to use": [["Instructions"], ["Fill in every sheet."]],
            "Subject areas": [
                ["Subject area", "Description"],
                ["orders", "What was sold, to whom, when."],
                ["regions", "Who manages the region."],
            ],
            "Tables": [
                ["Table", "Owned by", "Also listed in"],
                ["sales_f", "orders", "regions"],
                ["customer_d", "orders", "regions"],
                ["date_d", "orders", ""],
                ["region_mgr_d", "regions", ""],
            ],
            "Joins": [
                [
                    "From table",
                    "From column",
                    "To table",
                    "To column",
                    "Condition",
                    "Role",
                    "Approved",
                    "Note",
                ],
                [
                    "sales_f",
                    "customer_key",
                    "customer_d",
                    "customer_key",
                    "",
                    "Customer (as at the time)",
                    "yes",
                    "for people",
                ],
                [
                    "sales_f",
                    "",
                    "customer_d",
                    "",
                    "sales_f.customer_ntrl_key = customer_d.customer_ntrl_key AND customer_d.current_flag = 'Y'",
                    "Customer (current)",
                    "yes",
                    "",
                ],
                ["sales_f", "sale_date_key", "date_d", "date_key", "", "Sale date", "no", ""],
                ["customer_d", "region", "region_mgr_d", "region", "", "Region manager", "", ""],
            ],
            "Metrics": [
                [
                    "Metric",
                    "Subject area",
                    "Definition",
                    "Calculation",
                    "Source tables",
                    "Approved",
                ],
                ["Sales Amount", "orders", "Total amount sold.", "SUM(amount)", "sales_f", "no"],
            ],
            "Columns": [
                ["Table", "Column", "Name", "Description"],
                ["sales_f", "amount", "Net Sales", "Amount after discounts."],
            ],
            "Sensitive columns": [["Table", "Column"], ["customer_d", "email"]],
            "Rules": [["Rule", "Detail"], ["Unknown members", "Key -1 means Unknown."]],
            "Settings": [
                ["Setting", "Value"],
                ["Schema", "sales_data"],
                ["Fiscal year start month", "9"],
            ],
        },
    )


def _workbook_module():
    import model_spec_workbook

    return model_spec_workbook


def test_workbook_parses_to_a_spec_with_counted_totals(tmp_path):
    mw = _workbook_module()
    spec, allow, errors = mw.parse(_filled_template(tmp_path))
    assert not errors
    assert allow == [
        "sales_data.sales_f",
        "sales_data.customer_d",
        "sales_data.date_d",
        "sales_data.region_mgr_d",
    ]
    assert spec["tables"][0]["also_in"] == ["regions"] and spec["fiscal_year_start_month"] == 9
    assert "note" not in spec["joins"][0]  # a column for people is not carried into the spec
    assert mw.summary(spec) == {
        "subject_areas": ["orders", "regions"],
        "tables": 4,
        "joins": 4,
        "joins_approved": 2,
        "joins_with_condition": 1,
        "metrics": 1,
        "sensitive_columns": 1,
        "columns_described": 1,
        "rules": 1,
    }


def test_workbook_missing_a_required_sheet_or_column_says_which(tmp_path):
    mw = _workbook_module()
    path = _xlsx(
        tmp_path / "bad.xlsx",
        {"Subject areas": [["Description"], ["x"]], "Tables": [["Table", "Owned by"], ["t", "a"]]},
    )
    _, _, errors = mw.parse(path)
    assert any("no 'joins' sheet" in e for e in errors)
    assert any("no column for 'name'" in e for e in errors)


def test_workbook_to_applied_model_end_to_end(tmp_path):
    mw = _workbook_module()
    model = tmp_path / "model"
    model.mkdir()
    _introspected(model)
    out, tables = tmp_path / "spec.json", tmp_path / "tables.txt"
    assert (
        mw.main(
            [
                "parse",
                "--file",
                _filled_template(tmp_path),
                "--out",
                str(out),
                "--tables-out",
                str(tables),
            ]
        )
        == 0
    )
    assert tables.read_text().splitlines()[0] == "sales_data.sales_f"
    res = model_spec.apply_spec(model, model_spec.load_spec(out), signer="you@example.com")
    assert res.applied, res.errors
    org = loader.load_datasource(model)
    assert org.fiscal_year_start_month == 9
    assert (
        sum(len(sa.relationships) for sa in org.subject_areas)
        + len(org.cross_subject_area_relationships)
        == 4
    )


def test_a_dry_run_needs_no_signer_and_counts_what_needs_sign_off(tmp_path):
    _introspected(tmp_path)
    res = model_spec.apply_spec(
        tmp_path, _spec(), dry_run=True
    )  # no signer: the skill asks after this
    assert not res.errors
    assert res.counts["joins_approved"] == 2 and res.counts["metrics_approved"] == 1
    real = model_spec.apply_spec(tmp_path, _spec())
    assert real.errors == [
        "3 joins/metrics are marked approved, so the run needs --signer (who signs them off)"
    ]


def test_a_plain_aggregate_metric_is_flagged_not_refused(tmp_path):
    _introspected(tmp_path)
    spec = _spec(
        metrics=[
            {
                "name": "Total Amount",
                "area": "orders",
                "definition": "Sum of amount.",
                "calculation": "SUM(amount)",
                "source_tables": ["sales_f"],
            },
            {
                "name": "Orders",
                "area": "orders",
                "definition": "Distinct sales.",
                "calculation": "COUNT(DISTINCT sale_key)",
                "source_tables": ["sales_f"],
            },
        ]
    )
    res = model_spec.apply_spec(tmp_path, spec, signer="you@example.com", dry_run=True)
    assert not res.errors
    flagged = [n for n in res.notes if "plain aggregate" in n]
    assert len(flagged) == 1 and "'Total Amount'" in flagged[0]


def test_a_stale_column_description_is_skipped_and_listed_not_refused(tmp_path):
    # data dictionaries go stale; a description for a dropped column must not block the whole model
    _introspected(tmp_path)
    spec = _spec(
        columns=_spec()["columns"] + [{"table": "sales_f", "column": "nope", "description": "x"}]
    )
    res = model_spec.apply_spec(tmp_path, spec, signer="you@example.com")
    assert res.applied and not res.errors and res.counts["columns_described"] == 1
    assert any("sales_f.nope" in n and "skipped 1" in n for n in res.notes)


def test_a_sensitive_column_that_does_not_exist_is_refused(tmp_path):
    _introspected(tmp_path)
    bad = _spec(sensitive_columns=[{"table": "customer_d", "column": "emial"}])
    res = model_spec.apply_spec(tmp_path, bad, signer="you@example.com", dry_run=True)
    assert any("customer_d.emial: column does not exist" in e for e in res.errors)


def test_cli_reports_a_missing_input_file_as_json(tmp_path, capsys, monkeypatch):
    # a relative path that doesn't resolve must say so, not print a traceback a skill reads as success
    from semantic_model import cli

    _introspected(tmp_path)
    monkeypatch.chdir(tmp_path)
    for argv in (
        ["apply-spec", str(tmp_path), "--file", "nope.json"],
        ["curate", str(tmp_path), "--ops-file", "nope.json"],
    ):
        with pytest.raises(SystemExit) as exit_:
            cli.main(argv)
        out = json.loads(capsys.readouterr().out)
        assert (
            exit_.value.code == 2
            and out["error"] == f"file not found: {(tmp_path / 'nope.json').resolve()}"
        )


def test_reapplying_after_enrichment_keeps_one_file_per_entity_and_lookup_by_name(tmp_path):
    # enrichment adds an entity under a display name; re-applying the spec rewrites the tree, and
    # the entity must still be ONE file that curate finds by its name
    from semantic_model import curate

    _introspected(tmp_path)
    assert model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com").applied
    added = curate.write_items(
        tmp_path,
        "orders",
        "entity",
        [
            {
                "name": "Ship To Customer",
                "description": "Who an order ships to.",
                "maps_to": [{"table": "customer_d", "column": "customer_key", "primary": True}],
            }
        ],
    )
    assert added.validated, added.errors
    assert model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com").applied
    files = sorted(
        p.name for p in (tmp_path / "subject_areas" / "orders" / "entities").glob("*.yaml")
    )
    assert files == ["ship_to_customer.yaml"]
    res = curate.apply(
        tmp_path,
        [
            {
                "op": "approve",
                "kind": "entity",
                "area": "orders",
                "name": "Ship To Customer",
                "at": "2026-01-01T00:00:00Z",
            }
        ],
        signer="you@example.com",
        role="owner",
    )
    assert res.applied, res.skipped


def test_validator_warns_about_an_area_join_the_loader_wont_serve(tmp_path):
    # the shape that silently loses joins: a join in an area to a table that area doesn't own
    from semantic_model.models import Relationship

    _introspected(tmp_path)
    org = loader.load_datasource(tmp_path, include_rejected=True)
    misc = next(sa for sa in org.subject_areas if sa.name == "misc")
    misc.relationships.append(
        Relationship(
            from_table="sales_f",
            from_column="customer_key",
            to_table="customer_d",
            to_column="customer_key",
            relationship="many_to_one",
        )
    )
    warnings = validator.validate(org).warnings
    assert any("won't be served" in w and "sales_f" in w for w in warnings)


# --- approving ONE join when two tables are joined several ways -----------------------------------


def _unapproved(tmp_path):
    _introspected(tmp_path)
    spec = _spec()
    for item in spec["joins"] + spec["metrics"]:
        item["approved"] = "no"
    assert model_spec.apply_spec(tmp_path, spec).applied
    return tmp_path


def _rels(root):
    org = loader.load_datasource(root, include_rejected=True)
    return [r for sa in org.subject_areas for r in sa.relationships] + list(
        org.cross_subject_area_relationships
    )


def test_an_ambiguous_join_approval_is_refused_not_guessed(tmp_path):
    from semantic_model import curate

    root = _unapproved(tmp_path)
    op = {
        "op": "approve",
        "kind": "relationship",
        "area": "orders",
        "name": "sales_f->customer_d",
        "at": "2026-01-01T00:00:00Z",
    }
    res = curate.apply(root, [op], signer="you@example.com", role="owner")
    assert not res.applied and "3 joins match sales_f->customer_d" in res.skipped[0]["reason"]
    assert all(r.review_state == "unreviewed" for r in _rels(root))


def test_a_join_approval_names_its_join_by_columns_or_condition(tmp_path):
    from semantic_model import curate

    root = _unapproved(tmp_path)
    base = {
        "op": "approve",
        "kind": "relationship",
        "area": "orders",
        "name": "sales_f->customer_d",
        "at": "2026-01-01T00:00:00Z",
    }
    on = (
        "sales_f.customer_ntrl_key = customer_d.customer_ntrl_key AND customer_d.current_flag = 'Y'"
    )
    res = curate.apply(
        root,
        [
            {**base, "from_column": "ship_to_customer_key", "to_column": "customer_key"},
            {**base, "on": "  " + on.replace(" AND ", "\n  AND ")},
        ],
        signer="you@example.com",
        role="owner",
    )
    assert len(res.applied) == 2, res.skipped
    approved = {(r.from_column, bool(r.on)) for r in _rels(root) if r.review_state == "approved"}
    assert approved == {("ship_to_customer_key", False), (None, True)}


def test_approve_queue_approves_each_of_several_joins_between_two_tables(tmp_path, capsys):
    from semantic_model import cli

    root = _unapproved(tmp_path)
    assert (
        cli.main(
            [
                "approve-queue",
                str(root),
                "--kind",
                "relationship",
                "--signer",
                "you@example.com",
                "--role",
                "owner",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert all(r.review_state == "approved" for r in _rels(root))


def test_explorer_gives_each_join_between_two_tables_its_own_key(tmp_path):
    import render_model_explorer as rme

    root = _unapproved(tmp_path)
    org = loader.load_datasource(root, include_rejected=True)
    keys = [
        rme._join_key(r)
        for sa in org.subject_areas
        for r in sa.relationships
        if (r.from_table, r.to_table) == ("sales_f", "customer_d")
    ]
    assert len(keys) == 3 and len(set(keys)) == 3


def test_a_stated_grain_is_set_and_a_wrong_one_is_refused(tmp_path):
    _introspected(tmp_path)
    spec = _spec()
    spec["tables"][1]["grain"] = "customer_key"  # customer_d
    spec["tables"][2]["grain"] = ["date_key"]  # date_d
    res = model_spec.apply_spec(tmp_path, spec, signer="you@example.com")
    assert res.applied and res.counts["tables_with_grain"] == 2, res.errors
    t = {
        t.name: t
        for sa in loader.load_datasource(tmp_path).subject_areas
        for t in sa.tables_defined
    }
    assert t["customer_d"].grain == ["customer_key"] and t["date_d"].grain == ["date_key"]
    spec["tables"][2]["grain"] = "date_sk"
    bad = model_spec.apply_spec(tmp_path, spec, signer="you@example.com", dry_run=True)
    assert any("grain column 'date_sk' does not exist" in e for e in bad.errors)


def test_workbook_reads_a_grain_column(tmp_path):
    mw = _workbook_module()
    path = _xlsx(
        tmp_path / "g.xlsx",
        {
            "Subject areas": [["Subject area"], ["orders"]],
            "Tables": [["Table", "Owned by", "Grain"], ["sales_f", "orders", "order_key, line_no"]],
            "Joins": [["From table", "To table"], ["sales_f", "sales_f"]],
        },
    )
    spec, _, errors = mw.parse(path)
    assert not errors and spec["tables"][0]["grain"] == ["order_key", "line_no"]


# --- re-applying a spec over a model that has moved on ---------------------------------------------

_XJOIN = {
    "from_table": "sales_f",
    "from_column": "sale_date_key",
    "to_table": "region_mgr_d",
    "to_column": "region",
    "relationship": "many_to_one",
    "from_subject_area": "orders",
    "to_subject_area": "regions",
}


def _model_files(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".snapshots" not in p.parts and p.name != "curation_log.jsonl"
    }


def test_reapplying_replaces_cross_area_joins_kept_in_their_own_file(tmp_path):
    from semantic_model import curate

    _introspected(tmp_path)
    assert model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com").applied
    added = curate.add_relationships(tmp_path, cross=[_XJOIN])
    assert added.applied and (tmp_path / "cross_subject_area_relationships.yaml").exists()
    res = model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com")
    assert res.applied, res.errors
    assert not (tmp_path / "cross_subject_area_relationships.yaml").exists()
    served = loader.load_datasource(tmp_path).cross_subject_area_relationships
    assert [(x.from_table, x.to_table) for x in served] == [("customer_d", "region_mgr_d")]


def test_a_cross_area_join_in_its_own_file_is_approved_where_it_lives(tmp_path):
    from semantic_model import curate

    _introspected(tmp_path)
    assert model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com").applied
    assert curate.add_relationships(
        tmp_path, cross=[{**_XJOIN, "review_state": "unreviewed"}]
    ).applied
    res = curate.apply(
        tmp_path,
        [
            {
                "op": "approve",
                "kind": "relationship",
                "area": "orders",
                "name": "sales_f->region_mgr_d",
                "from_column": "sale_date_key",
                "at": "2026-01-01T00:00:00Z",
            }
        ],
        signer="you@example.com",
        role="owner",
    )
    assert res.applied, res.skipped
    doc = yaml.safe_load((tmp_path / "cross_subject_area_relationships.yaml").read_text())
    assert doc["edges"][0]["review_state"] == "approved"


def test_reapplying_keeps_metrics_the_spec_does_not_redefine(tmp_path):
    from semantic_model import curate

    _introspected(tmp_path)
    assert model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com").applied
    assert curate.write_items(
        tmp_path,
        "orders",
        "metric",
        [
            {
                "name": "repeat_rate",
                "calculation": "Share of customers with two or more sales.",
                "bindings": {"PostgreSQL": "COUNT(DISTINCT customer_key)"},
                "source_tables": ["sales_f"],
            }
        ],
    ).validated
    res = model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com")
    assert res.applied and res.counts["metrics_kept"] == 1
    names = {m.name for sa in loader.load_datasource(tmp_path).subject_areas for m in sa.metrics}
    assert names == {"sales_amount", "repeat_rate"}


def test_a_failed_apply_restores_the_model_and_its_version(tmp_path, monkeypatch):
    from semantic_model import curate, snapshot

    _introspected(tmp_path)
    assert model_spec.apply_spec(tmp_path, _spec(), signer="you@example.com").applied
    assert curate.add_relationships(tmp_path, cross=[_XJOIN]).applied
    before, version = _model_files(tmp_path), snapshot.newest_version(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(model_spec, "_write_rules", boom)
    res = model_spec.apply_spec(
        tmp_path, _spec(subject_areas=_spec()["subject_areas"]), signer="you@example.com"
    )
    assert not res.applied and "previous model was restored" in res.errors[0]
    assert _model_files(tmp_path) == before
    assert snapshot.newest_version(tmp_path) == version
