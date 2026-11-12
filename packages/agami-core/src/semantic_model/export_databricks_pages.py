"""Databricks Pages (F19 / ACE-172): entities, key terminology and (on request) metrics as Pages.

A Page is Databricks' governed definition of a business concept, created with
`POST /api/2.0/knowledge/pages`. Its body is the documented Page object: `domain`, `display_name`,
`description`, `synonyms`, a Markdown `body` and, for a page agami publishes, `state`. The export
writes one such body per page under `databricks/pages/`, plus one manifest per subject area holding
what the body has no field for, in the order it is submitted: the governed tag and domain each page
sits in, the asset links (`RELATED_ASSETS`, a separate call per link once the page exists), the
certification a curator assigns afterwards, and a `request_id` that makes a re-submitted create
return the existing page. Nothing here calls Databricks; submitting is the customer's act.

Key terminology belongs to the whole datasource, so its pages sit in a datasource domain and take a
datasource-scoped `request_id`: each subject area still writes its own copy of the file (the core
refuses two areas writing one path), but every copy creates the same single page.

Left to the customer on purpose: the domain id (a placeholder per domain, known only once the
domain exists), the metastore, the owner (omitted, so it defaults to whoever submits) and whether
anything is published: without `--pages-publish` every page is a draft, which Genie One shows only
to its owner. Only an item approved on a recorded review is published or marked certified: a key
term has no review state in agami, so its page stays a draft and uncertified under the flag too.
A signer's identity never enters a file.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import uuid
from typing import Any, Optional

from . import export_databricks as DB
from .export import ExportOptions, ExportView, LossEntry, WriterOutput, degraded, dropped, export_id
from .models import bare_name

PAGES_DIR = "pages"
METASTORE = "{metastore_id}"             # an asset link's scope for a Unity Catalog table
MAX_TITLE = 200
MAX_SYNONYMS = 50
MAX_SYNONYM = 200
MAX_TAG_KEY = 256                        # a governed tag key
MAX_DOMAIN_TEXT = 4096                   # a domain's, and a tag policy's, description
# Governed tag keys refuse `* . / < > % & ? \ =` and control characters; Unity Catalog tag keys
# also refuse `, - :`. A domain's key is both, and `/` is what names a subdomain.
_BAD_TAG_KEY = re.compile(r"[*./<>%&?\\=,\-:\x00-\x1f]")
# What a re-export sends to replace a page in place; `domain` cannot be updated. `state` is in the
# mask only of a page this export publishes, so a re-export never unpublishes a page a Page manager
# published by hand.
UPDATE_MASK = "display_name,body,synonyms,description"
PUBLISH_MASK = UPDATE_MASK + ",state"
PUBLISH = "pages_publish"                # the flags' argparse dests
METRICS = "pages_metrics"
_CERTIFY = ("certification is not a Page field and no documented API assigns a tag to a Page: a "
            "curator assigns system.certification_status in Discover; the manifest marks it certified")
_DRAFTS = ("its pages are drafts, which Genie One uses only in their owner's conversations; "
           "--pages-publish publishes the ones approved on a recorded review")


def page_file(area: str, kind: str, name: str) -> str:
    return f"{PAGES_DIR}/{DB.databricks_name(area)}__{kind}__{DB.databricks_name(name)}.json"


def manifest_file(area: str) -> str:
    # One `__`, where a page file has two and `databricks_name` never writes a run of `_`.
    return f"{PAGES_DIR}/{DB.databricks_name(area)}__manifest.json"


def request_id(export_id: str) -> str:
    """The UUIDv4 the create call takes as `request_id`, minted from the item's export id, so the
    same item sends the same id on every run and a second create returns the page the first made."""
    return str(uuid.UUID(hex=export_id, version=4))


def page_request_id(view: ExportView, kind: str, name: str) -> str:
    """A page's `request_id`. Databricks scopes it to the account and caller, so it names the
    datasource too: two datasources with an entity `customer` make two pages. A term's names no
    subject area, so each area's copy of a term page creates the one page."""
    scope = f"{view.datasource}/{kind}/{name}" if kind == "terminology" else \
        f"{view.datasource}/{view.area}/{kind}/{name}"
    return request_id(export_id(view.target, kind, scope))


def domain_placeholder(*scope: str) -> str:
    """What a page's `domain` holds until the customer has the id: `{domain_id__<scope>}`."""
    return "{domain_id__" + "__".join(DB.databricks_name(s) for s in scope) + "}"


def _json(doc: Any) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class _Pages:
    """The pages of one subject area, in qualified-name order: entities, metrics, terminology."""

    def __init__(self, view: ExportView, options: ExportOptions) -> None:
        self.view = view
        self.publish = bool(options.target_args.get(PUBLISH))
        self.losses: list[LossEntry] = []
        self.files: dict[str, bytes] = {}
        self.entries: list[dict[str, Any]] = []
        self.titles: dict[str, set[str]] = {}    # domain placeholder -> casefolded titles
        self.physical = {bare_name(t.table.name).casefold(): t.physical for t in view.tables}
        self.domains: list[dict[str, Any]] = []

    # Made on first use, so a domain no page sits in is neither written nor reported.
    @functools.cached_property
    def datasource_domain(self) -> str:
        v = self.view
        return self.domain("datasource", v.datasource, v.description, (v.datasource,))

    @functools.cached_property
    def area_domain(self) -> str:
        v = self.view
        return self.domain("subject_area", v.area, v.area_description, (v.datasource, v.area))

    def drop(self, kind: str, name: str, reason: Any, detail: str) -> None:
        self.losses.append(dropped(self.view.area, kind, name, reason, detail))

    def degrade(self, kind: str, name: str, reason: Any, detail: str) -> None:
        self.losses.append(degraded(self.view.area, kind, name, reason, detail))

    def domain(self, kind: str, name: str, description: str, scope: tuple[str, ...]) -> str:
        """The placeholder of the domain `name` gives (a datasource's or a subject area's), adding
        its governed tag and create-domain bodies the first time. A domain is named by its tag key;
        a name the key rules refuse becomes its `databricks_name`."""
        key = name.strip()
        if not key or _BAD_TAG_KEY.search(key) or len(key) > MAX_TAG_KEY:
            key = DB.databricks_name(name)
            self.degrade(kind, name, "target_rule", f"its domain is named {key}: a governed tag key "
                         f"holds at most {MAX_TAG_KEY} characters and none of * . / < > % & ? \\ = , - :")
        known = next((d for d in self.domains if d["create"]["tag_key"] == key), None)
        if known is not None:
            return known["placeholder"]
        create: dict[str, Any] = {"tag_key": key}
        if description and len(description) > MAX_DOMAIN_TEXT:
            self.degrade(kind, name, "target_limit", f"its description is left off its domain, "
                         f"which holds at most {MAX_DOMAIN_TEXT} characters")
        elif description:
            create["description"] = description
        policy = dict(create)
        if self.publish:
            create["draft"] = False
        self.domains.append({"placeholder": domain_placeholder(*scope), "tag_policy": policy,
                             "create": create})
        return self.domains[-1]["placeholder"]

    def synonyms(self, kind: str, name: str, names: list[str]) -> list[str]:
        # Databricks trims, de-duplicates and sorts them; doing it here keeps the file as stored.
        kept = {s.strip() for s in names if s.strip() and s.strip().casefold() != name.casefold()}
        long = sorted(s for s in kept if len(s) > MAX_SYNONYM)
        if long:
            self.degrade(kind, name, "target_limit", f"{len(long)} synonym(s) over {MAX_SYNONYM} "
                         "characters left out")
        out = sorted(kept - set(long))
        if len(out) > MAX_SYNONYMS:
            self.degrade(kind, name, "target_limit", f"carries the first {MAX_SYNONYMS} synonyms "
                         f"of {len(out)}, in sorted order")
        return out[:MAX_SYNONYMS]

    def fqn(self, table: str) -> Optional[str]:
        """The Unity Catalog name an asset link takes, `catalog.schema.table`, or None when the
        export does not carry the table or knows no catalog for it."""
        p = self.physical.get(bare_name(table).casefold())
        if p is None or not (p.database and p.schema):
            return None
        return f"{p.database}.{p.schema}.{p.table}"

    def links(self, kind: str, name: str, tables: list[str]) -> list[str]:
        out: list[str] = []
        for table in tables:
            fqn = self.fqn(table)
            if fqn is None:
                self.degrade(kind, name, "target_rule", f"no asset link to {table}: an asset link "
                             "needs catalog.schema.table, and this export knows no catalog for it "
                             "(--location gives one)")
            elif fqn not in out:
                out.append(fqn)
        return out

    def add(self, kind: str, name: str, description: str, synonyms: list[str], body: str,
            tables: list[str], domain: str) -> None:
        path = page_file(self.view.area, kind, name)
        if len(name) > MAX_TITLE:
            self.drop(kind, name, "target_limit", f"a Page title holds at most {MAX_TITLE} characters")
            return
        # Published titles are unique within a domain.
        titles = self.titles.setdefault(domain, set())
        if name.casefold() in titles or path in self.files:
            self.drop(kind, name, "name_collision", "an earlier page of its domain has its title, "
                      "or one of this subject area its file name")
            return
        titles.add(name.casefold())
        # Published and certified only on a recorded review: a key term travels as approved,
        # but agami holds terms with no review state, so nobody has vouched for one.
        approved = self.view.reviewed(kind, name)
        published = self.publish and approved
        if not approved and self.view.approved(kind, name):
            self.degrade(kind, name, "not_approved", "a draft, never published or marked "
                         f"certified: agami holds {kind} with no review state, so no review of "
                         "it is recorded")
        page: dict[str, Any] = {"domain": f"domains/{domain}", "display_name": name}
        if description:
            page["description"] = description
        if synonyms:
            page["synonyms"] = synonyms
        page["body"] = body
        if published:
            page["state"] = "PUBLISHED"
        self.files[path] = _json(page)
        if approved:
            self.degrade(kind, name, "no_home_in_target", _CERTIFY)
        self.entries.append({
            "kind": kind, "name": name, "file": path.removeprefix(f"{PAGES_DIR}/"),
            "domain": domain, "request_id": page_request_id(self.view, kind, name),
            "state": "PUBLISHED" if published else "DRAFT",
            "update_mask": PUBLISH_MASK if published else UPDATE_MASK,
            "certification_status": "certified" if approved else None,
            "related_assets": [{"asset_type": "ASSET_TYPE_UC_TABLE", "asset_scope_id": METASTORE,
                                "asset_id": t} for t in tables]})

    def term(self, term: str, definition: str) -> None:
        self.add("terminology", term, definition, [], definition or term, [],
                 self.datasource_domain)

    def entity(self, e: Any) -> None:
        tables = self.links("entity", e.name, [mp.table for mp in e.maps_to])
        lines = [self._mapping_line(e.name, mp, self.fqn(mp.table)) for mp in e.maps_to]
        if lines:
            self.degrade("entity", e.name, "carried_as_text", "its columns are listed in the body; "
                         "an asset link names a table, never a column")
        for field in ("value_pattern", "clarification_strictness"):
            if getattr(e, field):
                self.degrade("entity", e.name, "detail_lost", f"its {field} has no Page field")
        if e.parent_entity:
            self.degrade("entity", e.name, "carried_as_text", "its parent entity is named in the "
                         "body; a link to another page needs that page's id, known only after it exists")
        body = _body(e.description or e.name, (
            ("Identified by", lines), ("Key components", [f"`{k}`" for k in e.key_components]),
            ("Part of", [e.parent_entity] if e.parent_entity else []),
            ("Format", [e.value_format_hint] if e.value_format_hint else []),
            ("Caveats", list(e.caveats))))
        self.add("entity", e.name, e.description, self.synonyms(
            "entity", e.name, [*e.other_names, *([e.plural] if e.plural else [])]), body, tables,
            self.area_domain)

    def metric(self, m: Any) -> None:
        body = _body(m.description or m.name, (
            ("Calculation", [m.calculation]),
            ("Answers", [m.business_question] if m.business_question else []),
            ("Unit", [m.unit] if m.unit else []),
            ("Caveats", list(m.caveats))))
        self.add("metric", m.name, m.description, self.synonyms("metric", m.name, m.other_names),
                 body, self.links("metric", m.name, list(m.source_tables)), self.area_domain)

    def _mapping_line(self, entity: str, mp: Any, fqn: Optional[str]) -> str:
        ref = f"`{fqn or bare_name(mp.table)}.{mp.column}`" + (" (primary)" if mp.primary else "")
        if mp.default_filter and self.view.sql_engine is None:
            self.degrade("entity", entity, "sql_not_for_target", f"the filter on its {mp.table} "
                         f"mapping is written for {self.view.source_engine}; SQL is never translated")
        elif mp.default_filter:
            ref += f", where `{mp.default_filter}`"
        return ref


def _body(lead: str, sections: tuple[tuple[str, list[str]], ...]) -> str:
    """The Markdown body: the lead paragraph, then a section per detail a reader needs."""
    parts = [lead]
    for title, items in sections:
        if items:
            parts.append(f"## {title}\n\n" + "\n".join(f"- {i}" for i in items))
    return "\n\n".join(parts) + "\n"


def _steps(publish: bool) -> list[dict[str, Any]]:
    """The calls that submit a manifest, in the order each needs the one before."""
    return [
        {"step": "tag_policy", "method": "POST", "path": "/api/2.1/tag-policies",
         "body": "each domain's tag_policy", "when": "no governed tag with its tag_key exists yet; "
         "a domain is built on a governed tag"},
        {"step": "domain", "method": "POST", "path": "/api/2.0/domains",
         "body": "each domain's create", "when": "no domain with its tag_key exists yet",
         "then": "replace the domain's placeholder in every page file with the domain_id it "
                 "returns (or an existing domain's id, to use that one instead)"},
        {"step": "page", "method": "POST",
         "path": "/api/2.0/knowledge/pages?request_id=<request_id>", "body": "the page's file",
         "then": "keep the page_id from the name it returns (pages/<page_id>)"},
        {"step": "asset_link", "method": "POST",
         "path": "/api/2.0/knowledge/pages/<page_id>/asset-link-categories/RELATED_ASSETS/asset-links",
         "body": "each of the page's related_assets"},
        {"step": "certification", "method": None, "path": None, "body": None,
         "when": "the page's certification_status is certified" + (
             "" if publish else " and it has been published"),
         "then": "a curator of its domain opens the page in Discover, chooses Assign certification, "
                 "then Certified; no documented API assigns system.certification_status to a Page"},
    ]


def write_pages(view: ExportView, options: ExportOptions) -> WriterOutput:
    """One Page request body per eligible entity, terminology entry and (with --pages-metrics)
    metric, plus the area's manifest."""
    pages = _Pages(view, options)
    for e in view.entities:
        pages.entity(e)
    if options.target_args.get(METRICS):
        for m in view.metrics:
            pages.metric(m.metric)
    for term, definition in view.terminology:
        pages.term(term, definition)
    if not pages.entries:
        return WriterOutput({}, tuple(pages.losses))
    if not pages.publish:
        pages.degrade("subject_area", view.area, "target_rule", _DRAFTS)
    domains = pages.domains
    manifest = {
        "steps": _steps(pages.publish),
        "update": {"method": "PATCH",
                   "path": "/api/2.0/knowledge/pages/<page_id>?update_mask=<update_mask>",
                   "body": "the page's file; a page agami newly approves moves to PUBLISHED, since "
                           "a create with a known request_id changes nothing"},
        "fill_in": {**{d["placeholder"]: "the domain_id of the domain the step domain creates"
                       for d in domains},
                    METASTORE: "the metastore UUID of the tables"},
        "domains": domains,
        "pages": pages.entries}
    return WriterOutput({**pages.files, manifest_file(view.area): _json(manifest)},
                        tuple(pages.losses))


def add_arguments(sp: argparse.ArgumentParser) -> None:
    # Suppressed unless given, as every target flag is: the report records only what was asked.
    sp.add_argument(
        "--pages-publish", dest=PUBLISH, action="store_true", default=argparse.SUPPRESS,
        help="write each approved Databricks Page (and its domain) as published, so every user of "
        "its domain sees it and Genie One uses it in every conversation; without it every page is "
        "a draft its submitter alone sees. Key terms stay drafts, never marked certified: agami "
        "records no review of them, and the report lists each")
    sp.add_argument(
        "--pages-metrics", dest=METRICS, action="store_true", default=argparse.SUPPRESS,
        help="also write a Databricks Page per metric; off by default, since the metric views "
        "already give Genie each metric's definition")


DB.register_artifact(write_pages, add_arguments)
