"""Personal-data detection (#516): whole-word name rules, value rules on the one per-table
sample, key paths inside JSON values, and that a refresh keeps an existing model's flags."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_model import (
    build,  # noqa: E402
    pii,  # noqa: E402
)
from semantic_model import curate as C  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402

from catalog_helpers import col, make_catalog_runner  # noqa: E402

# Names the earlier substring rule flagged. Each must stay flagged.
FLAGGED_ON_MAIN = [
    "email", "customer_email", "EMAIL_ADDRESS", "contactEmail", "emails_sent", "phone", "phone_number",
    "home_phone", "telephone", "phones", "mobile", "mobile_no", "mobile_number", "mobile_phone",
    "customer_mobile", "alt_mobile_1", "mobilephone", "dob", "dob_verified", "birth_date",
    "birthdate", "birthday", "date_of_birth", "birth_place", "place_of_birth", "address",
    "address_line1", "billing_address", "email_address", "shipping_address_city", "addresses",
    "aadhaar", "aadhar_no", "pan", "PAN", "ssn", "SSN", "passport", "passport_no", "pincode",
    "zip", "account_no", "accountno", "bank_account_no", "card_no", "cardno",
    "discard_notes", "salary", "base_salary", "income", "income_total", "annual_income",
    "customerPhone", "phone2", "emailaddress",
]

# Non-person words that contained a fragment of the old rule. Pinned: these are the ONLY names
# a new build stops flagging.
KNOWN_FALSE_POSITIVES = [
    "phoneme_error_rate", "phonemes", "phonetic_score", "mobile_sessions", "mobile_app_version",
    "is_mobile", "has_mobile", "headphones_sold", "earphone_count", "microphone_count",
    "adobe_campaign_id", "automobile_sales", "ip_address", "IPAddress", "mac_address",
    "server_address", "memory_address",
]

NEWLY_FLAGGED = [
    "customer_name", "contact_name", "contactName", "user_name", "username", "employee_name",
    "patient_name", "person_name", "owner_name", "author_name", "member_name", "client_name",
    "first_name", "firstName", "last_name", "full_name", "middle_name", "surname",
    "customer_ssn", "zip_code", "zipCode", "pan_number", "pan_no", "e_mail", "eMail",
    "postcode", "postal_code", "msisdn", "credit_card_number", "card_number", "account_number",
    "home_addr",
]

NOT_FLAGGED = [
    "product_name", "company_name", "table_name", "file_name", "filename", "name", "status",
    "order_total", "region", "campaign_id", "created_at", "notes", "description",
]


@pytest.mark.parametrize("name", FLAGGED_ON_MAIN)
def test_names_flagged_on_main_stay_flagged(name):
    assert pii.STRONG_PII_RE.search(name), "not a name the old rule flagged"
    assert build.detect_sensitive("events", name)


@pytest.mark.parametrize("name", KNOWN_FALSE_POSITIVES)
def test_known_false_positives_are_not_flagged_but_stay_suspected(name):
    assert pii.STRONG_PII_RE.search(name), "the old rule flagged it"
    assert not build.detect_sensitive("events", name)
    assert build.suspected_pii(name)  # still listed for review


@pytest.mark.parametrize("name", NEWLY_FLAGGED)
def test_names_the_old_rule_missed(name):
    assert build.detect_sensitive("orders", name)


@pytest.mark.parametrize("name", NOT_FLAGGED)
def test_ordinary_names_are_not_flagged(name):
    assert not build.detect_sensitive("products", name)


def test_bare_name_still_needs_a_person_table():
    assert build.detect_sensitive("PII", "name")
    assert build.detect_sensitive("customers", "name")
    assert not build.detect_sensitive("products", "name")


def test_tokens_split_snake_camel_and_digits():
    assert pii.tokens("customerEmail_2") == ["customer", "email", "2"]
    assert pii.tokens("IPAddress") == ["ip", "address"]
    assert pii.tokens("e-mail") == ["e", "mail"]


# --- value rules ---------------------------------------------------------------


def test_email_values_need_eighty_percent():
    assert pii.looks_like_emails(["you@example.com"] * 8 + ["n/a", None, ""] + ["x"])
    assert not pii.looks_like_emails(["you@example.com"] * 3 + ["hello"] * 7)
    assert not pii.looks_like_emails([None, ""])


def test_phone_values():
    assert pii.looks_like_phones("contact", ["+1 555 010 0100", "(555) 010-0199", "555-010-0142"])
    assert pii.looks_like_phones("reach", ["5550100100", "5550100101"])
    assert not pii.looks_like_phones("order_id", ["5550100100", "5550100101"])  # an id, not a phone
    assert not pii.looks_like_phones("ref", ["12345", "2026-01-02"])


def test_birth_dates_are_name_gated():
    old = ["1980-04-02", "1975-11-30", "1990-01-01"]
    assert pii.looks_like_birth_dates("born_on", old)
    assert not pii.looks_like_birth_dates("signup_date", old)
    assert not pii.looks_like_birth_dates("born_on", ["2026-01-01"])


# --- JSON ----------------------------------------------------------------------


def test_json_key_paths_by_name_and_by_value():
    rows = [json.dumps({"email": "you@example.com", "contact": {"phone": "+1 555 010 0100"},
                        "prefs": {"theme": "dark"}, "backup": "you@example.com"})] * 3
    assert pii.json_pii_paths("events", rows) == ["email", "contact.phone", "backup"]


def test_json_dict_values_nested_name_and_caps():
    rows = [{"a": {"b": {"c": {"email": "x"}}}}]  # depth 4: beyond the cap
    assert pii.json_pii_paths("t", rows) == []
    rows = [{"contact": {"name": "n"}}]
    assert pii.json_pii_paths("t", rows) == ["contact.name"]
    many = [{f"k{i}": i for i in range(60)} | {"email": "x"}]
    assert pii.json_pii_paths("t", many) == []  # past the 50-key cap


def test_json_ignores_non_objects_and_data_keys():
    assert pii.json_pii_paths("t", ["not json", "[1, 2]", None, "{broken"]) == []
    assert pii.json_pii_paths("t", [{"you@example.com": "x"}]) == []


# --- introspection -------------------------------------------------------------

_SAMPLE = [
    {"id": str(i), "contact": f"user{i}@example.com", "notes": "you@example.com" if i < 2 else "ok",
     "reach": f"+1 555 010 {i:04d}", "born_on": "1980-04-02",
     "profile": json.dumps({"email": f"user{i}@example.com", "contact": {"phone": "+1 555 010 0100"},
                            "birth_date": "1980-04-02", "tier": "gold"}),
     "label": "gold", "phoneme_error_rate": "0.1"}
    for i in range(10)
]


def _runner(sample=_SAMPLE):
    return make_catalog_runner(
        tables=["events"],
        columns={"events": [col("id", "integer", nullable=False), col("contact", "text"),
                            col("notes", "text"), col("reach", "text"), col("born_on", "date"),
                            col("profile", "jsonb"), col("label", "text"),
                            col("phoneme_error_rate", "numeric", scale=2)]},
        sample=lambda sql: [dict(r) for r in sample])


def _events(tmp_path, runner=None):
    org, rep = I.introspect("shop", "postgres", runner=runner or _runner(), artifacts_dir=tmp_path)
    return org.subject_areas[0].defined_table("events"), rep


def test_introspect_flags_and_lists_from_values(tmp_path):
    t, rep = _events(tmp_path)
    c = {x.name: x for x in t.columns}
    assert c["contact"].sensitive            # 100% email values
    assert not c["notes"].sensitive          # 20% email values
    assert not c["reach"].sensitive and pii.PHONE_SUSPECT_CAVEAT in c["reach"].caveats
    assert not c["born_on"].sensitive and pii.DOB_SUSPECT_CAVEAT in c["born_on"].caveats
    assert c["profile"].sensitive
    assert c["profile"].caveats == ["Contains personal data at: email, contact.phone, birth_date"]
    assert not c["label"].sensitive and not c["phoneme_error_rate"].sensitive
    assert "events.reach (values look like phone numbers)" in rep.suspected_columns
    assert "suspected personal data (review): events.reach" in rep.render()

    org = L.load_datasource(tmp_path / "shop")
    susp = {s["column"]: s["reason"] for s in C.suspected_sensitive_columns(org)["columns"]}
    assert susp["reach"] == "values look like phone numbers"
    assert susp["born_on"] == "values look like dates of birth"


def test_no_sampled_value_is_written(tmp_path):
    _, rep = _events(tmp_path)
    written = rep.render() + "".join(p.read_text() for p in (tmp_path / "shop").rglob("*") if p.is_file())
    for v in ("user1@example.com", "you@example.com", "+1 555 010", "1980-04-02"):
        assert v not in written, v


def test_one_sample_per_table_even_without_date_or_choice_columns(tmp_path):
    seen: list[str] = []

    def sample(sql):
        if "DISTINCT" not in sql:  # the value-list reads are separate, per column
            seen.append(sql)
        return [{"id": "1", "contact": "you@example.com"}]

    runner = make_catalog_runner(
        tables=["people"],
        columns={"people": [col("id", "integer", nullable=False), col("contact_id_ref", "text")]},
        sample=sample)
    I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True)
    assert len(seen) == 1


def test_refresh_keeps_existing_flags(tmp_path):
    # A model an older release built: phoneme_error_rate flagged, customer_name not.
    old = make_catalog_runner(
        tables=["events"],
        columns={"events": [col("id", "integer", nullable=False), col("phoneme_error_rate", "numeric"),
                            col("customer_name", "text")]})
    I.introspect("shop", "postgres", runner=old, artifacts_dir=tmp_path)
    import yaml
    p = next((tmp_path / "shop").rglob("events.yaml"))
    d = yaml.safe_load(p.read_text())
    for c in d["columns"]:
        c["sensitive"] = c["name"] == "phoneme_error_rate"
    p.write_text(yaml.safe_dump(d, sort_keys=False))

    org, _ = I.introspect("shop", "postgres", runner=old, artifacts_dir=tmp_path, append=True)
    t = org.subject_areas[0].defined_table("events")
    flags = {c.name: c.sensitive for c in t.columns}
    assert flags == {"id": False, "phoneme_error_rate": True, "customer_name": False}


def test_explorer_marks_value_suspects():
    c = m.Column(name="reach", type="string", caveats=[pii.PHONE_SUSPECT_CAVEAT])
    assert build.suspected_reason(c.name, c.caveats) == "values look like phone numbers"
    assert build.suspected_reason("total", []) is None


def test_refresh_reports_no_sample_findings_for_kept_columns(tmp_path):
    _events(tmp_path)
    org, rep = I.introspect("shop", "postgres", runner=_runner(), artifacts_dir=tmp_path, append=True)
    assert rep.suspected_columns == []
    assert not any("flagged sensitive" in n for n in rep.notes)


# --- review fixes ----------------------------------------------------------------

_REACH = [{"id": str(i), "reach": "5550100100" if i % 2 else "5550100101"} for i in range(12)]


@pytest.mark.parametrize("probe", [False, True])
def test_digit_only_phone_column_gets_no_choice_list(tmp_path, probe):
    runner = make_catalog_runner(
        tables=["events"],
        columns={"events": [] if probe else [col("id", "integer", nullable=False), col("reach", "text")]},
        sample=lambda sql: [dict(r) for r in _REACH])
    t, _ = _events(tmp_path, runner)
    reach = next(c for c in t.columns if c.name == "reach")
    assert pii.PHONE_SUSPECT_CAVEAT in reach.caveats
    assert reach.choice_field is None


def test_json_data_keys_are_redacted_but_still_flagged():
    rows = [json.dumps({"Demo Person": {"email": "you@example.com"}})]
    assert pii.json_pii_paths("t", rows) == ["*.email"]
    # An identifier-shaped key seen in fewer than half the objects is data too.
    rows = [json.dumps({f"Person{i}": {"email": "you@example.com"}, "contact": {"phone": "x"}})
            for i in range(4)]
    assert pii.json_pii_paths("t", rows) == ["*.email", "contact.phone"]


def test_json_data_key_never_reaches_caveat_or_report(tmp_path):
    sample = [{"id": str(i), "profile": json.dumps({"Demo Person": {"email": "you@example.com"}})}
              for i in range(3)]
    runner = make_catalog_runner(
        tables=["events"], columns={"events": [col("id", "integer", nullable=False), col("profile", "jsonb")]},
        sample=lambda sql: [dict(r) for r in sample])
    t, rep = _events(tmp_path, runner)
    prof = next(c for c in t.columns if c.name == "profile")
    assert prof.sensitive
    assert prof.caveats == ["Contains personal data at: *.email"]
    written = rep.render() + "".join(p.read_text() for p in (tmp_path / "shop").rglob("*") if p.is_file())
    assert "Demo Person" not in written


def test_json_python_repr_from_the_cli_is_read():
    assert pii.json_pii_paths("t", [str({"email": "you@example.com"})]) == ["email"]
    assert pii.json_pii_paths("t", ["{__import__('os').system('true'): 1}"]) == []  # never executed


def test_json_python_repr_through_a_cli_shaped_runner_flags_the_column(tmp_path):
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["id", "profile"])
    w.writeheader()
    for i in range(3):
        # What execute_sql's CSV writes for a native Postgres jsonb value: the Python repr.
        w.writerow({"id": i, "profile": str({"email": f"user{i}@example.com", "tier": "gold"})})
    rows = [I._Row(r) for r in csv.DictReader(io.StringIO(buf.getvalue()))]
    runner = make_catalog_runner(
        tables=["events"], columns={"events": [col("id", "integer", nullable=False), col("profile", "jsonb")]},
        sample=lambda sql: rows)
    t, _ = _events(tmp_path, runner)
    prof = next(c for c in t.columns if c.name == "profile")
    assert prof.sensitive and prof.caveats == ["Contains personal data at: email"]


@pytest.mark.parametrize("name", ["ipv4_address", "ipv6_address", "IPv4Address", "IPv6Address"])
def test_ip_address_variants_are_suspected_only(name):
    assert not build.detect_sensitive("events", name)
    assert build.suspected_pii(name)


def _finding_report(schema, column):
    rep = I.IntrospectReport(profile="shop", db_type="postgres", out_dir="", dry_run=True)
    cols = [m.Column(name=column, type="string")]
    I._pii_from_sample(schema, "events", cols, [{column: "+1 555 010 0100"}] * 3, rep)
    return rep


def test_refresh_drops_findings_only_for_the_kept_tables_schema():
    rep = _finding_report("public", "reach")
    other = _finding_report("private", "reach")
    rep.suspected_columns += other.suspected_columns
    rep.sample_findings += other.sample_findings
    I._drop_sample_findings(rep, "public", "events", {"reach"})
    assert rep.suspected_columns == ["events.reach (values look like phone numbers)"]
    assert [k for _, k in rep.sample_findings] == [("private", "events", "reach")]


def test_refresh_drops_findings_for_a_case_only_rename():
    rep = _finding_report("public", "Reach")
    I._drop_sample_findings(rep, "public", "events", {"reach"})
    assert rep.suspected_columns == []


def test_probe_mode_reads_one_sample_per_table(tmp_path):
    seen: list[str] = []
    rows = [{"id": str(i), "status": "open" if i % 2 else "closed", "created_on": "2026-01-02"}
            for i in range(12)]

    def sample(sql):
        if "DISTINCT" not in sql:
            seen.append(sql)
        return [dict(r) for r in rows]

    runner = make_catalog_runner(tables=["events"], columns={"events": []}, sample=sample)
    t, _ = _events(tmp_path, runner)
    assert next(c for c in t.columns if c.name == "status").choice_field
    assert len(seen) == 1


def test_ip_address_values_are_not_read_as_phone_numbers():
    # Dots are allowed in a phone number, so 192.168.1.10 used to pass as one and an ip_address column
    # was suspected for the wrong reason; four dotted groups is an address, three is still a phone.
    ips = ["192.168.1.10", "10.20.30.40", "172.16.254.1", "192.168.100.200"] * 5
    assert not pii.looks_like_phones("source", ips)
    assert pii.value_suspect_caveat("source", "string", ips) is None
    assert not any(pii.personal_value(v) for v in ips)
    phones = ["555.123.4567", "555.987.6543", "+1 555 0100 222", "(555) 010-0199"] * 5
    assert pii.looks_like_phones("source", phones)


# --- value lists from any source stay off personal columns ---------------------------------------


def test_a_declared_list_on_a_sensitive_column_is_dropped(tmp_path):
    runner = make_catalog_runner(
        tables=["people"],
        columns={"people": [col("id", "integer", nullable=False), col("address_type", "text"),
                            col("salary", "USER-DEFINED"), col("status", "text")]},
        checks=["CHECK ((address_type = ANY (ARRAY['Home'::text, 'home'::text, 'work'::text])))",
                "CHECK ((status = ANY (ARRAY['open'::text, 'closed'::text])))"],
        enums=[{"column_name": "salary", "enum_value": v} for v in ("low", "high")],
        estimate="50000",
    )
    org, _ = I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True)
    t = org.subject_areas[0].defined_table("people")
    for name in ("address_type", "salary"):
        c = t.get_column(name)
        assert c.sensitive and c.choice_field is None, name
        assert not any("'" in k for k in c.caveats), name  # no caveat quotes a dropped value
    assert t.get_column("status").choice_field == {"open": "", "closed": ""}


def test_a_declared_list_of_personal_values_is_dropped(tmp_path):
    runner = make_catalog_runner(
        tables=["people"],
        columns={"people": [col("id", "integer", nullable=False), col("owner", "text")]},
        checks=["CHECK ((owner = ANY (ARRAY['a@example.com'::text, 'b@example.com'::text])))"],
        estimate="50000",
    )
    org, _ = I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True)
    assert org.subject_areas[0].defined_table("people").get_column("owner").choice_field is None


def test_a_value_suspect_column_gets_no_list_read_from_the_data(tmp_path):
    seen: list[str] = []
    # Digit-only phone numbers: suspected from the sample, yet each value alone passes as a code.
    rows = [{"id": str(i), "reach": "5550100111" if i % 2 else "5550100222",
             "status": "open" if i % 2 else "closed"} for i in range(12)]

    def sample(sql):
        seen.append(sql)
        return [dict(r) for r in rows]

    runner = make_catalog_runner(
        tables=["events"],
        columns={"events": [col("id", "integer", nullable=False), col("reach", "text"), col("status", "text")]},
        sample=sample)
    org, _ = I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True)
    t = org.subject_areas[0].defined_table("events")
    assert pii.PHONE_SUSPECT_CAVEAT in t.get_column("reach").caveats
    assert t.get_column("reach").choice_field is None
    assert not any("DISTINCT" in s and '"reach"' in s for s in seen)
    assert t.get_column("status").choice_field == {"closed": "", "open": ""}


@pytest.mark.parametrize("v", ["1980-04-02T00:00:00Z", "1980-04-02 00:00:00+00:00", "1980-04-02 00:00:00+00",
                               "1980-04-02T00:00:00+05:30", "1980-04-02 00:00:00.000000-0800"])
def test_timezone_aware_midnight_is_a_birth_date(v):
    assert pii.looks_like_birth_dates("born_on", [v] * 3)
    assert pii.value_suspect_caveat("born_on", "timestamp", [v] * 3) == pii.DOB_SUSPECT_CAVEAT


def test_timezone_aware_non_midnight_is_not_a_birth_date():
    assert not pii.looks_like_birth_dates("born_on", ["1980-04-02T13:45:00+00:00"] * 3)


@pytest.mark.parametrize("name", ["customerId", "userID", "trackingCode", "id_number", "idNumber",
                                  "id_no", "customer_id_no", "accountUUID", "order_id", "id"])
def test_id_named_columns_with_digit_values_are_not_phones(name):
    assert not pii.looks_like_phones(name, ["5550100100", "5550100101"])


@pytest.mark.parametrize("name", ["reach", "phone_number", "mobileNo", "number", "contact"])
def test_non_id_names_with_digit_values_stay_phone_suspects(name):
    assert pii.looks_like_phones(name, ["5550100100", "5550100101"])


def test_introspect_tz_midnight_dob_and_camel_case_id(tmp_path):
    rows = [{"id": str(i), "bornOn": f"1980-04-{i + 1:02d} 00:00:00+00:00",
             "customerId": f"55501001{i:02d}"} for i in range(10)]
    runner = make_catalog_runner(
        tables=["events"],
        columns={"events": [col("id", "integer", nullable=False), col("bornOn", "timestamptz"),
                            col("customerId", "text")]},
        sample=lambda sql: [dict(r) for r in rows])
    t, rep = _events(tmp_path, runner)
    assert pii.DOB_SUSPECT_CAVEAT in t.get_column("bornOn").caveats
    assert pii.PHONE_SUSPECT_CAVEAT not in t.get_column("customerId").caveats
    assert "events.customerId (values look like phone numbers)" not in rep.suspected_columns
