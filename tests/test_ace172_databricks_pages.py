"""`databricks` target, Pages (ACE-172): entities, terminology and metrics as Page request bodies.

`_validate_page`, `_validate_link` and `_validate_manifest` are written from Databricks' REST API
reference (read 2026-11-12): the Page object and its create and update calls
(docs.databricks.com/api/knowledge/v1/create-page and …/update-page: `domain` and `display_name`
required on create, a 1-200 character title, at most 50 synonyms of 1-200 characters each, stored
trimmed, de-duplicated and sorted, `state` DRAFT or PUBLISHED and DRAFT when omitted, `request_id` a
UUIDv4 query parameter, `update_mask` a non-empty list of display_name, body, synonyms, description,
state and owner_ids, never domain), an asset link (…/create-asset-link: `asset_type`,
`asset_scope_id`, `asset_id`, all required, and the nine asset types), a domain
(docs.databricks.com/api/domains/v1/create-domain: `tag_key` required, `draft`, `icon`, `subtitle`
up to 280 characters, `description` up to 4096, owner ids, `parent_domain_id`) and a tag policy
(…/tag-policies/v1/create-tag-policy: `tag_key` 1-256 characters, `description` up to 4096,
`values`); a governed tag key's characters come from the governed-tags page.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import re
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
PKG_SRC = REPO / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import cli  # noqa: E402
from semantic_model import export as X  # noqa: E402
from semantic_model import export_databricks as DB  # noqa: E402
from semantic_model import export_databricks_pages as P  # noqa: E402  (registers the artifact)
from semantic_model.loader import load_datasource  # noqa: E402

SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"
AREA = "sales"
SIGNER = "you@example.com"

# ---------------------------------------------------------------------------
# A structural validator, from the Pages REST API reference
# ---------------------------------------------------------------------------

PAGE_FIELDS = {"name", "domain", "display_name", "body", "synonyms", "description", "state",
               "owner_ids"}
OUTPUT_ONLY = {"name", "create_time", "update_time"}
ASSET_TYPES = {"ASSET_TYPE_UC_TABLE", "ASSET_TYPE_WORKSPACE_DASHBOARD",
               "ASSET_TYPE_WORKSPACE_GENIE_AGENT", "ASSET_TYPE_WORKSPACE_NOTEBOOK",
               "ASSET_TYPE_WORKSPACE_APP", "ASSET_TYPE_WORKSPACE_DBSQL_QUERY",
               "ASSET_TYPE_ACCOUNT_PAGE", "ASSET_TYPE_ACCOUNT_USER", "ASSET_TYPE_EXTERNAL_URL"}


def _validate_page(doc: dict) -> list[str]:
    errs = [f"unknown field {k}" for k in set(doc) - PAGE_FIELDS - OUTPUT_ONLY]
    errs += [f"server-assigned {k} on create" for k in set(doc) & OUTPUT_ONLY]
    if not re.fullmatch(r"domains/[^/]+", str(doc.get("domain", ""))):
        errs.append("domain must be domains/{domain_id}")
    title = doc.get("display_name")
    if not isinstance(title, str) or not 1 <= len(title) <= 200:
        errs.append("display_name must be 1 to 200 characters")
    for k in ("body", "description"):
        if k in doc and not isinstance(doc[k], str):
            errs.append(f"{k} must be a string")
    syn = doc.get("synonyms", [])
    if not isinstance(syn, list) or len(syn) > 50:
        errs.append("at most 50 synonyms")
    elif any(not isinstance(s, str) or not 1 <= len(s) <= 200 or s != s.strip() for s in syn):
        errs.append("each synonym 1 to 200 characters, trimmed")
    elif syn != sorted(set(syn)):
        errs.append("synonyms de-duplicated and sorted")
    if "state" in doc and doc["state"] not in ("DRAFT", "PUBLISHED"):
        errs.append("state must be DRAFT or PUBLISHED")
    if "owner_ids" in doc and (not isinstance(doc["owner_ids"], list) or len(doc["owner_ids"]) > 1):
        errs.append("at most one owner id")
    return errs


def _validate_link(link: dict) -> list[str]:
    errs = [f"unknown link field {k}" for k in set(link) - {"asset_type", "asset_scope_id", "asset_id"}]
    errs += [f"link needs {k}" for k in ("asset_type", "asset_scope_id", "asset_id") if not link.get(k)]
    if link.get("asset_type") not in ASSET_TYPES:
        errs.append(f"asset_type {link.get('asset_type')}")
    return errs


DOMAIN_FIELDS = {"tag_key", "draft", "icon", "subtitle", "description", "technical_owner_ids",
                 "business_owner_ids", "parent_domain_id"}
TAG_POLICY_FIELDS = {"tag_key", "description", "values"}
MASK_PATHS = {"display_name", "body", "synonyms", "description", "state", "owner_ids"}
BAD_TAG_KEY = re.compile(r"[*./<>%&?\\=,\-:\x00-\x1f]")
# The documented calls, as the manifest names them, in submit order.
CALLS = [("tag_policy", "POST", "/api/2.1/tag-policies"), ("domain", "POST", "/api/2.0/domains"),
         ("page", "POST", "/api/2.0/knowledge/pages?request_id=<request_id>"),
         ("asset_link", "POST", "/api/2.0/knowledge/pages/<page_id>/asset-link-categories/"
                                "RELATED_ASSETS/asset-links"),
         ("certification", None, None)]


def _validate_tag_key(key) -> list[str]:
    if not isinstance(key, str) or not 1 <= len(key) <= 256 or key != key.strip():
        return ["tag_key must be 1 to 256 characters, trimmed"]
    return ["tag_key has a character a governed tag refuses"] if BAD_TAG_KEY.search(key) else []


def _validate_domain(d: dict) -> list[str]:
    errs = [f"unknown domain entry field {k}" for k in set(d) - {"placeholder", "tag_policy", "create"}]
    if not re.fullmatch(r"\{domain_id__[0-9a-z_]+\}", str(d.get("placeholder"))):
        errs.append("placeholder must be {domain_id__<name>}")
    create, policy = d.get("create", {}), d.get("tag_policy", {})
    errs += [f"unknown domain field {k}" for k in set(create) - DOMAIN_FIELDS]
    errs += [f"unknown tag policy field {k}" for k in set(policy) - TAG_POLICY_FIELDS]
    errs += _validate_tag_key(create.get("tag_key"))
    if policy.get("tag_key") != create.get("tag_key"):
        errs.append("a domain's tag_key is its governed tag's")
    if "draft" in create and not isinstance(create["draft"], bool):
        errs.append("draft must be a boolean")
    for doc in (create, policy):
        if len(doc.get("description", "")) > 4096:
            errs.append("description over 4096 characters")
    if len(create.get("subtitle", "")) > 280:
        errs.append("subtitle over 280 characters")
    if any(k.endswith("owner_ids") for k in create):
        errs.append("an owner is the customer's to choose")
    return errs


def _validate_manifest(doc: dict) -> list[str]:
    errs = [f"unknown manifest field {k}" for k in set(doc) - {"steps", "update", "fill_in",
                                                             "domains", "pages"}]
    if [(s.get("step"), s.get("method"), s.get("path")) for s in doc.get("steps", [])] != CALLS:
        errs.append("steps must be the documented calls in submit order")
    if doc.get("update", {}).get("method") != "PATCH" or not str(doc.get("update", {}).get(
            "path")).startswith("/api/2.0/knowledge/pages/<page_id>?update_mask="):
        errs.append("update must be PATCH /api/2.0/knowledge/pages/<page_id>")
    domains = {d.get("placeholder") for d in doc.get("domains", [])}
    for d in doc.get("domains", []):
        errs += _validate_domain(d)
    if len(domains) != len(doc.get("domains", [])):
        errs.append("each domain once")
    keys = [d.get("create", {}).get("tag_key") for d in doc.get("domains", [])]
    if len(set(keys)) != len(keys):
        errs.append("each tag_key once")
    errs += [f"no fill_in for {p}" for p in domains if p not in doc.get("fill_in", {})]
    for e in doc.get("pages", []):
        if e.get("domain") not in domains:
            errs.append(f"{e.get('name')}: its domain has no create")
        try:
            if uuid.UUID(e.get("request_id", "")).version != 4:
                errs.append("request_id must be a UUIDv4")
        except ValueError:
            errs.append("request_id must be a UUIDv4")
        mask = str(e.get("update_mask", "")).split(",")
        if not mask or not set(mask) <= MASK_PATHS or len(set(mask)) != len(mask):
            errs.append(f"{e.get('name')}: update_mask {e.get('update_mask')}")
        if e.get("state") not in ("DRAFT", "PUBLISHED"):
            errs.append("state must be DRAFT or PUBLISHED")
        # A mask with state writes it, so it must be the page's own: a draft's never has it.
        if ("state" in mask) != (e.get("state") == "PUBLISHED"):
            errs.append(f"{e.get('name')}: state in update_mask only of a published page")
        if e.get("certification_status") not in (None, "certified"):
            errs.append("certification_status is certified or absent")
        errs += [x for link in e.get("related_assets", []) for x in _validate_link(link)]
    return errs


# ---------------------------------------------------------------------------
# A synthetic model: Databricks, with terminology and entities in every state
# ---------------------------------------------------------------------------


def _entity(name, *maps, **kw):
    return {"name": name, "maps_to": [{"table": t, "column": c, **({"primary": True} if i == 0 else {})}
                                      for i, (t, c) in enumerate(maps)],
            "review_state": "approved", "signed_off_by": SIGNER, "signed_off_role": "owner", **kw}


def _write_model(root: Path) -> Path:
    def put(rel, doc):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    def col(name, type_="string", **kw):
        return {"name": name, "type": type_, **kw}

    put("datasource.yaml", {
        "datasource": "acme", "description": "Acme demo sales.",
        "storage_connections": [{"name": "acme_dbx", "storage_type": "Databricks", "storage_config": {}}],
        "subject_areas": [f"subject_areas/{AREA}"],
        "key_terminology": {"ARR": "Annual recurring revenue: twelve times MRR.",
                            "Customer": "Anyone who has placed an order.",
                            "Churn": "A subscription that ended in the period.",
                            "Tax ID": "The id in tax_id, which stays home."}})
    put(f"subject_areas/{AREA}/subject_area.yaml", {"name": AREA, "description": "Orders."})
    for name, cols in (("orders", [col("id", "integer"), col("customer_id", "integer"),
                                   col("store_id", "integer")]),
                       ("customers", [col("id", "integer"), col("tax_id", sensitive=True)]),
                       ("stores", [col("id", "integer"), col("code")])):
        put(f"subject_areas/{AREA}/tables/{name}.yaml",
            {"name": name, "schema": "SALES_DATA", "columns": cols})
    entities = [
        _entity("customer", ("customers", "id"), ("orders", "customer_id"),
                other_names=["buyer", " buyer ", "Customer", "x" * 201], plural="customers",
                description="A person who buys.", caveats=["Guest checkouts have no row."],
                value_pattern="^C[0-9]+$", value_format_hint="C followed by digits"),
        _entity("store", ("stores", "id"), review_state="unreviewed", signed_off_by=None,
                parent_entity="region", key_components=["code"]),
        _entity("open order", ("orders", "id"), maps_to=[
            {"table": "orders", "column": "id", "primary": True,
             "default_filter": "orders.store_id IS NOT NULL"}]),
        _entity("open-order", ("orders", "id")),
        _entity("t" * 201, ("orders", "id")),
        _entity("bulk", ("orders", "id"), other_names=[f"name {i:02d}" for i in range(60)]),
        _entity("taxpayer", ("customers", "tax_id")),
    ]
    for e in entities:
        put(f"subject_areas/{AREA}/entities/{e['name'][:20].replace(' ', '_')}.yaml", e)
    # Its title is the entity `open order`'s, in the same domain; only --pages-metrics writes it.
    put(f"subject_areas/{AREA}/metrics/open_order.yaml", {
        "name": "Open Order", "description": "Orders placed.", "calculation": "Count of orders.",
        "bindings": {"Databricks": "COUNT(orders.id)"}, "source_tables": ["orders"],
        "review_state": "approved", "signed_off_by": SIGNER})
    return root


def _add_area(root: Path, area: str = "billing") -> None:
    """A second subject area, with its own table and entity, under the same terminology."""
    ds = yaml.safe_load((root / "datasource.yaml").read_text(encoding="utf-8"))
    ds["subject_areas"].append(f"subject_areas/{area}")
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False), encoding="utf-8")
    base = root / "subject_areas" / area
    (base / "tables").mkdir(parents=True)
    (base / "entities").mkdir()
    (base / "subject_area.yaml").write_text(yaml.safe_dump({"name": area}), encoding="utf-8")
    (base / "tables" / "invoices.yaml").write_text(yaml.safe_dump(
        {"name": "invoices", "schema": "SALES_DATA", "columns": [{"name": "id", "type": "integer"}]}),
        encoding="utf-8")
    (base / "entities" / "invoice.yaml").write_text(yaml.safe_dump(
        _entity("invoice", ("invoices", "id")), sort_keys=False), encoding="utf-8")


@pytest.fixture()
def model(tmp_path: Path) -> Path:
    return _write_model(tmp_path / "art" / "acme")


def _sm(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["export", *argv])
    return rc, buf.getvalue()


def _export(root: Path, out: Path, *flags: str) -> tuple[dict, dict[str, dict], dict[str, bytes]]:
    rc, text = _sm(str(root), "--target", "databricks", "--out", str(out), *flags)
    assert rc == 0, text
    raw = {p.name: p.read_bytes() for p in sorted((out / "databricks" / P.PAGES_DIR).glob("*.json"))}
    return json.loads(text)["targets"]["databricks"], {k: json.loads(v) for k, v in raw.items()}, raw


def _pages(docs: dict[str, dict]) -> dict[str, dict]:
    return {k: v for k, v in docs.items() if not k.endswith("__manifest.json")}


def _manifest(docs: dict[str, dict], area: str) -> dict[str, dict]:
    return {e["name"]: e for e in docs[f"{DB.databricks_name(area)}__manifest.json"]["pages"]}


def _losses(section: dict, kind: str, name: str) -> list[tuple[str, str]]:
    return [(x["outcome"], x["reason"]) for x in section["losses"]
            if x["kind"] == kind and x["name"] == name]


@pytest.fixture()
def sample(tmp_path):
    return _export(SAMPLE, tmp_path / "s", "--location", "DEMO.SALES_DATA")


@pytest.fixture()
def synthetic(model, tmp_path):
    return _export(model, tmp_path / "y", "--location", "ACME.SALES_DATA")


# ---------------------------------------------------------------------------
# Success criteria
# ---------------------------------------------------------------------------


def test_the_sample_writes_one_body_per_eligible_entity_and_term(sample):
    _, docs, _ = sample
    sa = load_datasource(SAMPLE).subject_areas[0]
    want = {P.page_file(sa.name, "entity", e.name).split("/")[1] for e in sa.entities}
    assert set(_pages(docs)) == want and len(want) == 8


def test_an_excluded_entity_or_term_gets_no_body(model, tmp_path):
    section, docs, _ = _export(model, tmp_path / "y", "--location", "ACME.SALES_DATA")
    names = {d["display_name"] for d in _pages(docs).values()}
    assert "store" not in names and _losses(section, "entity", "store") == [("dropped", "not_approved")]
    # Both name the sensitive column, so both stay home with it.
    assert "taxpayer" not in names and "Tax ID" not in names
    assert ("dropped", "depends_on_excluded") in _losses(section, "terminology", "Tax ID")
    assert {"ARR", "Churn", "customer", "open order", "bulk"} <= names


def test_every_body_and_link_passes_the_validator(sample, synthetic):
    for _, docs, _ in (sample, synthetic):
        for name, doc in _pages(docs).items():
            assert _validate_page(doc) == [], name
            assert "owner_ids" not in doc and "state" not in doc and "name" not in doc
        for doc in (d for k, d in docs.items() if k.endswith("__manifest.json")):
            assert _validate_manifest(doc) == []
            # A repeated request_id would make a second create return the first page.
            for key in ("request_id", "file"):
                values = [entry[key] for entry in doc["pages"]]
                assert len(set(values)) == len(values), key
            for entry in doc["pages"]:
                assert entry["file"] in docs
                assert uuid.UUID(entry["request_id"]).version == 4
                for link in entry["related_assets"]:
                    assert _validate_link(link) == [], link


def test_approved_items_are_certified_and_unreviewed_ones_are_not(model, tmp_path):
    section, docs, _ = _export(model, tmp_path / "u", "--location", "ACME.SALES_DATA",
                               "--include-unreviewed")
    m = _manifest(docs, AREA)
    assert m["store"]["certification_status"] is None
    assert m["customer"]["certification_status"] == "certified"
    # A key term has no review state in agami, so nobody vouched for it: never certified.
    assert m["ARR"]["certification_status"] is None
    assert ("degraded", "not_approved") in _losses(section, "terminology", "ARR")
    # The body has no certification field, and the report says so for each certified page.
    assert all("certif" not in json.dumps(d) for d in _pages(docs).values())
    assert ("degraded", "no_home_in_target") in _losses(section, "entity", "customer")
    assert ("degraded", "no_home_in_target") not in _losses(section, "entity", "store")


def test_a_related_asset_is_a_carried_table_by_its_physical_name(sample, synthetic):
    for (_, docs, _), catalog in ((sample, "DEMO"), (synthetic, "ACME")):
        carried = {f"{catalog}.SALES_DATA.{t}" for t in ("customers", "orders", "stores", "products",
                                                         "categories", "invoices", "payments", "plans",
                                                         "subscriptions", "order_items", "refunds",
                                                         "subscription_events")}
        links = [a["asset_id"] for d in docs.values() if "pages" in d
                 for e in d["pages"] for a in e["related_assets"]]
        assert links and set(links) <= carried
    m = _manifest(synthetic[1], AREA)
    assert [a["asset_id"] for a in m["customer"]["related_assets"]] == [
        "ACME.SALES_DATA.customers", "ACME.SALES_DATA.orders"]
    # A column has no asset type, so it is named in the body, and the report says so.
    assert "`ACME.SALES_DATA.orders.customer_id`" in _pages(synthetic[1])[
        "sales__entity__customer.json"]["body"]
    assert ("degraded", "carried_as_text") in _losses(synthetic[0], "entity", "customer")


def test_two_runs_write_byte_identical_files(model, tmp_path):
    for root, flags in ((SAMPLE, ("--location", "DEMO.SALES_DATA")),
                        (model, ("--location", "ACME.SALES_DATA", "--include-unreviewed"))):
        a = _export(root, tmp_path / "one", *flags)[2]
        b = _export(root, tmp_path / "two", *flags)[2]
        assert a == b and a


# ---------------------------------------------------------------------------
# Mapping detail and what does not carry
# ---------------------------------------------------------------------------


def test_an_entity_maps_name_description_synonyms_and_body(synthetic):
    section, docs, _ = synthetic
    page = _pages(docs)["sales__entity__customer.json"]
    assert page["display_name"] == "customer" and page["description"] == "A person who buys."
    # Trimmed, de-duplicated, sorted; the title itself and an over-long one left out.
    assert page["synonyms"] == ["buyer", "customers"]
    assert ("degraded", "target_limit") in _losses(section, "entity", "customer")
    assert "## Caveats\n\n- Guest checkouts have no row." in page["body"]
    assert "C followed by digits" in page["body"] and "^C[0-9]+$" not in json.dumps(page)
    assert ("degraded", "detail_lost") in _losses(section, "entity", "customer")
    bulk = _pages(docs)["sales__entity__bulk.json"]
    assert len(bulk["synonyms"]) == 50 and ("degraded", "target_limit") in _losses(section, "entity", "bulk")


def test_a_term_maps_to_title_description_and_body(synthetic):
    _, docs, _ = synthetic
    page = _pages(docs)["sales__terminology__arr.json"]
    # A term belongs to the datasource, so its page sits in the datasource's domain.
    assert page == {"domain": "domains/{domain_id__acme}", "display_name": "ARR",
                    "description": "Annual recurring revenue: twelve times MRR.",
                    "body": "Annual recurring revenue: twelve times MRR."}
    assert _manifest(docs, AREA)["ARR"]["related_assets"] == []


def test_title_and_file_collisions_and_long_titles_are_dropped(synthetic):
    section, docs, _ = synthetic
    titles = [d["display_name"] for d in _pages(docs).values()]
    # Titles are unique within a domain: the entity `customer` (the area's) and the term
    # `Customer` (the datasource's) sit in two, so both are written.
    assert titles.count("customer") == 1 and titles.count("Customer") == 1
    assert ("dropped", "name_collision") not in _losses(section, "terminology", "Customer")
    assert "open-order" not in titles and ("dropped", "name_collision") in _losses(
        section, "entity", "open-order")
    assert ("dropped", "target_limit") in _losses(section, "entity", "t" * 201)


def test_a_mapping_filter_travels_only_in_the_target_engine(model, synthetic, tmp_path):
    body = _pages(synthetic[1])["sales__entity__open_order.json"]["body"]
    assert "where `orders.store_id IS NOT NULL`" in body
    section, docs, _ = _export(SAMPLE, tmp_path / "s", "--location", "DEMO.SALES_DATA")
    assert not [x for x in section["losses"] if x["reason"] == "sql_not_for_target"
                and x["kind"] == "entity"]
    org = load_datasource(model)
    opts = X.ExportOptions(("databricks",), location=X.parse_location("ACME.SALES_DATA"))
    view, _ = X.build_view(org, org.subject_areas[0], X.TARGETS["databricks"], opts, "Snowflake",
                           [], [])
    out = P.write_pages(view, opts)
    assert "IS NOT NULL" not in out.files["pages/sales__entity__open_order.json"].decode()
    assert any(x.name == "open order" and x.reason == "sql_not_for_target" for x in out.losses)


def test_a_parent_entity_is_named_in_the_body(model, tmp_path):
    section, docs, _ = _export(model, tmp_path / "u", "--location", "ACME.SALES_DATA",
                               "--include-unreviewed")
    body = _pages(docs)["sales__entity__store.json"]["body"]
    assert "## Part of\n\n- region" in body and "## Key components\n\n- `code`" in body
    assert ("degraded", "carried_as_text") in _losses(section, "entity", "store")


def test_without_a_catalog_no_asset_link_is_written(model, tmp_path):
    section, docs, _ = _export(model, tmp_path / "n")
    assert _manifest(docs, AREA)["customer"]["related_assets"] == []
    assert ("degraded", "target_rule") in _losses(section, "entity", "customer")
    assert "`customers.id` (primary)" in _pages(docs)["sales__entity__customer.json"]["body"]


def test_no_identity_and_no_api_call_reaches_a_file(synthetic, model, tmp_path):
    _, docs, raw = synthetic
    _, _, unrev = _export(model, tmp_path / "u", "--location", "ACME.SALES_DATA", "--include-unreviewed")
    for blob in (*raw.values(), *unrev.values()):
        text = blob.decode()
        assert SIGNER not in text and "owner" not in text


def test_request_ids_are_stable_uuids_from_export_ids():
    eid = X.export_id("databricks", "entity", "sales/entity/customer")
    rid = P.request_id(eid)
    assert uuid.UUID(rid).version == 4 and rid == P.request_id(eid) and rid != P.request_id("0" * 32)


def test_an_area_with_no_pages_writes_no_manifest(model):
    org = load_datasource(model)
    opts = X.ExportOptions(("databricks",))
    view, _ = X.build_view(org, org.subject_areas[0], X.TARGETS["databricks"], opts, "Databricks",
                           [], [])
    empty = dataclasses.replace(view, entities=(), terminology=())
    assert P.write_pages(empty, opts).files == {}


def test_pages_is_a_registered_databricks_artifact_the_privacy_scan_covers():
    import importlib.util

    assert P.write_pages in DB.ARTIFACTS
    spec = importlib.util.spec_from_file_location("privacy", REPO / "tests" / "test_privacy_no_network.py")
    privacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(privacy)
    module = PKG_SRC / "semantic_model" / "export_databricks_pages.py"
    assert module in privacy.SCRIPTS
    assert not any(rx.search(module.read_text(encoding="utf-8")) for rx in privacy._FORBIDDEN_RE)


@pytest.mark.parametrize("break_it,needle", [
    (lambda d: d.pop("domain"), "domain"),
    (lambda d: d.update(display_name=""), "display_name"),
    (lambda d: d.update(synonyms=["b", "a"]), "sorted"),
    (lambda d: d.update(synonyms=[" a"]), "trimmed"),
    (lambda d: d.update(synonyms=[str(i) for i in range(51)]), "50"),
    (lambda d: d.update(state="LIVE"), "state"),
    (lambda d: d.update(name="pages/x"), "server-assigned"),
    (lambda d: d.update(owner="x"), "unknown field"),
    (lambda d: d.update(body=1), "body"),
    (lambda d: d.update(owner_ids=["1", "2"]), "owner"),
])
def test_the_validator_catches_each_rule(break_it, needle):
    doc = {"domain": "domains/{domain_id__acme}", "display_name": "x", "synonyms": ["a"],
           "body": "b"}
    assert _validate_page(dict(doc)) == []
    break_it(doc)
    assert any(needle in e for e in _validate_page(doc)), _validate_page(doc)
    assert _validate_link({"asset_type": "TABLE", "asset_id": "a", "x": 1}) != []


# ---------------------------------------------------------------------------
# Publishing, domains, one glossary per datasource, metric pages
# ---------------------------------------------------------------------------


def _view(root: Path, *flags: str, **replace):
    org = load_datasource(root)
    args = dict(f.removeprefix("--").replace("-", "_").split("=", 1) if "=" in f
                else (f.removeprefix("--").replace("-", "_"), True) for f in flags)
    opts = X.ExportOptions(("databricks",), location=X.parse_location("ACME.SALES_DATA"),
                           target_args=args)
    view, _ = X.build_view(org, org.subject_areas[0], X.TARGETS["databricks"], opts, "Databricks",
                           [], [])
    return dataclasses.replace(view, **replace), opts


def test_without_the_flag_every_page_is_a_draft_and_the_report_says_so_once(synthetic):
    section, docs, _ = synthetic
    assert all("state" not in d for d in _pages(docs).values())
    m = _manifest(docs, AREA)
    assert {e["state"] for e in m.values()} == {"DRAFT"}
    assert {e["update_mask"] for e in m.values()} == {P.UPDATE_MASK}
    # The Genie artifact reports on the subject area too: pick this artifact's entry by its flag.
    assert ("degraded", "target_rule") in _losses(section, "subject_area", AREA)
    (detail,) = [x["detail"] for x in section["losses"]
                 if x["kind"] == "subject_area" and "--pages-publish" in x["detail"]]
    assert "--pages-publish" in detail and "owner" in detail
    domains = docs[f"{AREA}__manifest.json"]["domains"]
    assert all("draft" not in d["create"] for d in domains)


def test_the_publish_flag_publishes_approved_pages_and_keeps_unreviewed_ones_drafts(model, tmp_path):
    section, docs, _ = _export(model, tmp_path / "p", "--location", "ACME.SALES_DATA",
                               "--include-unreviewed", "--pages-publish")
    pages, manifest = _pages(docs), docs[f"{AREA}__manifest.json"]
    assert _validate_manifest(manifest) == []
    assert all(_validate_page(d) == [] for d in pages.values())
    assert pages["sales__entity__customer.json"]["state"] == "PUBLISHED"
    # A key term stays a draft: approved by the contract, but no review of it is recorded.
    assert "state" not in pages["sales__terminology__arr.json"]
    assert (m_arr := _manifest(docs, AREA)["ARR"])["state"] == "DRAFT"
    assert m_arr["certification_status"] is None
    assert ("degraded", "not_approved") in _losses(section, "terminology", "ARR")
    assert "state" not in pages["sales__entity__store.json"]
    m = _manifest(docs, AREA)
    assert (m["customer"]["state"], m["customer"]["update_mask"]) == ("PUBLISHED", P.PUBLISH_MASK)
    assert (m["store"]["state"], m["store"]["update_mask"]) == ("DRAFT", P.UPDATE_MASK)
    assert all(d["create"]["draft"] is False for d in manifest["domains"])
    assert not [x for x in section["losses"]
                if x["kind"] == "subject_area" and "--pages-publish" in x["detail"]]


def test_the_flag_is_recorded_in_the_report(model, tmp_path):
    rc, text = _sm(str(model), "--target", "databricks", "--out", str(tmp_path / "r"),
                   "--pages-publish")
    assert rc == 0, text
    assert json.loads(text)["options"]["target_args"] == {"pages_publish": True}


def test_domains_come_from_the_datasource_and_the_subject_area(synthetic):
    _, docs, _ = synthetic
    manifest = docs[f"{AREA}__manifest.json"]
    assert manifest["domains"] == [
        {"placeholder": "{domain_id__acme__sales}",
         "tag_policy": {"tag_key": "sales", "description": "Orders."},
         "create": {"tag_key": "sales", "description": "Orders."}},
        {"placeholder": "{domain_id__acme}",
         "tag_policy": {"tag_key": "acme", "description": "Acme demo sales."},
         "create": {"tag_key": "acme", "description": "Acme demo sales."}}]
    assert _pages(docs)["sales__entity__customer.json"]["domain"] == "domains/{domain_id__acme__sales}"
    assert [s["step"] for s in manifest["steps"]] == [c[0] for c in CALLS]
    assert set(manifest["fill_in"]) == {"{domain_id__acme__sales}", "{domain_id__acme}", P.METASTORE}


def test_a_domain_name_the_tag_rules_refuse_and_an_over_long_description_are_reported(model):
    view, opts = _view(model, area="Sales/EMEA", area_description="x" * 4097)
    out = P.write_pages(view, opts)
    (manifest,) = [json.loads(v) for k, v in out.files.items() if k.endswith("__manifest.json")]
    assert _validate_manifest(manifest) == []
    area = manifest["domains"][0]
    assert area["create"] == {"tag_key": "sales_emea"} and area["tag_policy"] == {"tag_key": "sales_emea"}
    reasons = {(x.kind, x.reason) for x in out.losses if x.name == "Sales/EMEA"}
    assert reasons == {("subject_area", "target_rule"), ("subject_area", "target_limit")}


def test_an_area_named_as_its_datasource_shares_its_domain(model):
    view, opts = _view(model, area="acme")
    out = P.write_pages(view, opts)
    manifest = json.loads(out.files["pages/acme__manifest.json"])
    assert _validate_manifest(manifest) == []
    assert [d["create"]["tag_key"] for d in manifest["domains"]] == ["acme"]
    # One domain, so one title space: the term `Customer` now meets the entity `customer`.
    assert any(x.name == "Customer" and x.reason == "name_collision" for x in out.losses)


def test_each_area_writes_the_same_term_page_and_it_creates_one_page(model, tmp_path):
    _add_area(model)
    _, docs, raw = _export(model, tmp_path / "t", "--location", "ACME.SALES_DATA")
    assert raw["sales__terminology__arr.json"] == raw["billing__terminology__arr.json"]
    sales, billing = _manifest(docs, AREA), _manifest(docs, "billing")
    # The same request_id, so the second create returns the page the first made.
    assert sales["ARR"]["request_id"] == billing["ARR"]["request_id"]
    assert sales["ARR"]["domain"] == billing["ARR"]["domain"] == "{domain_id__acme}"
    assert sales["customer"]["request_id"] != billing["invoice"]["request_id"]
    assert _validate_manifest(docs["billing__manifest.json"]) == []


def test_request_ids_name_the_datasource(model):
    view, _ = _view(model)
    other = dataclasses.replace(view, datasource="demo")
    for kind, name in (("entity", "customer"), ("terminology", "ARR")):
        assert P.page_request_id(view, kind, name) != P.page_request_id(other, kind, name)
        assert uuid.UUID(P.page_request_id(view, kind, name)).version == 4


def test_metric_pages_only_with_their_flag(tmp_path):
    _, plain, _ = _export(SAMPLE, tmp_path / "a", "--location", "DEMO.SALES_DATA")
    assert not [k for k in plain if "__metric__" in k]
    section, docs, _ = _export(SAMPLE, tmp_path / "b", "--location", "DEMO.SALES_DATA",
                               "--pages-metrics")
    org = load_datasource(SAMPLE)
    opts = X.ExportOptions(("databricks",), location=X.parse_location("DEMO.SALES_DATA"))
    view, _ = X.build_view(org, org.subject_areas[0], X.TARGETS["databricks"], opts, "Databricks",
                           [], [])
    want = {P.page_file(view.area, "metric", m.metric.name).split("/")[1] for m in view.metrics}
    assert want and {k for k in docs if "__metric__" in k} == want
    m = _manifest(docs, view.area)
    for em in view.metrics:
        page = docs[P.page_file(view.area, "metric", em.metric.name).split("/")[1]]
        assert _validate_page(page) == []
        assert page["display_name"] == em.metric.name
        assert f"## Calculation\n\n- {em.metric.calculation}" in page["body"]
        assert [a["asset_id"].rsplit(".", 1)[1] for a in m[em.metric.name]["related_assets"]] == [
            bare for bare in dict.fromkeys(em.metric.source_tables)]
    assert _validate_manifest(docs[f"{DB.databricks_name(view.area)}__manifest.json"]) == []


def test_a_metric_page_whose_title_its_domain_holds_is_dropped(model, tmp_path):
    section, docs, _ = _export(model, tmp_path / "m", "--location", "ACME.SALES_DATA",
                               "--pages-metrics")
    assert "sales__metric__open_order.json" not in docs
    assert ("dropped", "name_collision") in _losses(section, "metric", "Open Order")


def _manifest_doc() -> dict:
    return {"steps": [{"step": s, "method": m, "path": p} for s, m, p in CALLS],
            "update": {"method": "PATCH",
                       "path": "/api/2.0/knowledge/pages/<page_id>?update_mask=<update_mask>"},
            "fill_in": {"{domain_id__acme}": "x"},
            "domains": [{"placeholder": "{domain_id__acme}", "tag_policy": {"tag_key": "acme"},
                         "create": {"tag_key": "acme"}}],
            "pages": [{"domain": "{domain_id__acme}", "request_id": str(uuid.UUID(int=1, version=4)),
                       "state": "DRAFT", "update_mask": P.UPDATE_MASK, "certification_status": None,
                       "related_assets": []}]}


@pytest.mark.parametrize("break_it,needle", [
    (lambda d: d["steps"].reverse(), "submit order"),
    (lambda d: d["update"].update(method="PUT"), "PATCH"),
    (lambda d: d["domains"][0]["create"].update(tag_key="a/b"), "refuses"),
    (lambda d: d["domains"][0]["create"].update(tag_key=" a"), "trimmed"),
    (lambda d: d["domains"][0]["create"].update(owner="x"), "unknown domain field"),
    (lambda d: d["domains"][0]["create"].update(business_owner_ids=[1]), "customer's"),
    (lambda d: d["domains"][0]["create"].update(draft="no"), "boolean"),
    (lambda d: d["domains"][0]["create"].update(description="x" * 4097), "4096"),
    (lambda d: d["domains"][0]["tag_policy"].update(tag_key="b"), "governed tag"),
    (lambda d: d["domains"].append(dict(d["domains"][0])), "once"),
    (lambda d: d["fill_in"].clear(), "fill_in"),
    (lambda d: d["pages"][0].update(domain="{domain_id__demo}"), "no create"),
    (lambda d: d["pages"][0].update(request_id="1"), "UUIDv4"),
    (lambda d: d["pages"][0].update(update_mask="domain,body"), "update_mask"),
    (lambda d: d["pages"][0].update(update_mask=P.PUBLISH_MASK), "only of a published"),
    (lambda d: d["pages"][0].update(state="PUBLISHED"), "only of a published"),
    (lambda d: d["pages"][0].update(certification_status="trusted"), "certified"),
    (lambda d: d["pages"][0].update(related_assets=[{"asset_type": "METRIC_VIEW"}]), "asset_type"),
])
def test_the_manifest_validator_catches_each_rule(break_it, needle):
    doc = _manifest_doc()
    assert _validate_manifest(doc) == []
    break_it(doc)
    assert any(needle in e for e in _validate_manifest(doc)), _validate_manifest(doc)
