"""Personal-data detection for introspection: whole-word name rules, value rules on the
table's one sample, and key-path rules inside JSON values.

Names are split into words (`_`, `-`, spaces, digits, camelCase), so `phoneme_error_rate` is
not a phone and `customer_name` is a person's name. The older substring patterns still run:
a name they flag stays flagged unless every hit sits in a known non-person word below, so a
build never loses a flag it had before except for those pinned cases. Sampled values are only
read here, never returned: callers get booleans, reasons and key paths.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import date
from typing import Any, Iterable, Optional

# The patterns earlier releases matched as substrings. Kept so a name they flag stays flagged.
STRONG_PII_RE = re.compile(
    r"(dob|birth|phone|mobile|email|address|aadhaar|aadhar|\bpan\b|\bssn\b|passport|"
    r"pincode|\bzip\b|account_?no|card_?no|salary|income)",
    re.IGNORECASE,
)
# Weakly-PII patterns — only sensitive inside a PII-ish table (a "name" column on
# a product table is not PII; on a person table it is).
WEAK_PII_RE = re.compile(r"(\bname\b|first_?name|last_?name|full_?name|surname)", re.IGNORECASE)
# Broader "might be PII" tokens for the SUSPECTED tier — a review aid only, never auto-flags.
_EXTRA_PII_RE = re.compile(
    r"(gender|\bsex\b|ip_?address|mac_?address|\bip\b|latitude|longitude|\bgeo\b|national_?id|"
    r"tax_?id|licen[cs]e|user_?name|nationality|ethnicit|religion|marital|emergency_?contact|"
    r"next_?of_kin|date_?of_?birth|maiden)", re.IGNORECASE)

_TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# One word alone is personal data, wherever it appears in the name.
_PII_WORDS = frozenset({
    "email", "emails", "emailaddress", "phone", "phones", "phonenumber", "phoneno", "telephone",
    "msisdn", "ssn", "dob", "birthdate", "birthday", "dateofbirth", "zip", "zipcode", "postcode",
    "postalcode", "pincode", "passport", "aadhaar", "aadhar", "salary", "salaries", "income",
    "firstname", "lastname", "fullname", "middlename", "surname", "accountno", "accountnumber",
    "cardno", "cardnumber", "panno", "pannumber",
})
_NUMBER_WORDS = frozenset({"no", "num", "nbr", "number"})
# Word sequences that are personal data together (`e_mail`, `date_of_birth`, `card_no`).
_PII_SEQUENCES: tuple[tuple[frozenset, ...], ...] = (
    (frozenset({"e"}), frozenset({"mail"})),
    (frozenset({"date"}), frozenset({"of"}), frozenset({"birth"})),
    (frozenset({"birth"}), frozenset({"date", "dt", "day"})),
    (frozenset({"d"}), frozenset({"o"}), frozenset({"b"})),
    (frozenset({"postal", "post", "pin"}), frozenset({"code"})),
    (frozenset({"account", "acct", "card"}), _NUMBER_WORDS),
    (frozenset({"credit", "debit"}), frozenset({"card"})),
    (frozenset({"social"}), frozenset({"security"})),
    (frozenset({"pan"}), _NUMBER_WORDS | {"card"}),
    (frozenset({"first", "last", "full", "middle", "given", "family", "maiden"}), frozenset({"name"})),
)
# `<person word>_name` names a person; `product_name` or `file_name` does not.
PERSON_WORDS = frozenset({
    "customer", "contact", "user", "employee", "patient", "person", "owner", "author", "member",
    "client",
})
# Words that contain an older pattern but are not about a person (`phoneme`, `adobe`).
NON_PII_WORDS = frozenset({
    "phoneme", "phonemes", "phonetic", "phonetics", "phonics", "headphone", "headphones",
    "earphone", "earphones", "microphone", "microphones", "megaphone", "megaphones",
    "saxophone", "saxophones", "xylophone", "xylophones", "gramophone", "gramophones",
    "adobe", "automobile", "automobiles",
})
# An `address` after one of these is a machine's address. Listed as suspected, not flagged.
_MACHINE_ADDRESS_WORDS = frozenset({"ip", "ipv4", "ipv6", "mac", "mem", "memory", "host", "server"})
# `mobile` followed by one of these is a number; followed by anything else it is a device or
# channel (`mobile_sessions`).
_MOBILE_NUMBER_WORDS = _NUMBER_WORDS | {"phone", "contact"}


def tokens(name: str) -> list[str]:
    """The words of a column name, lower-cased: `customerEmail_2` -> customer, email, 2."""
    return [w for _, _, w in _token_spans(name)]


def _token_spans(name: str) -> list[tuple[int, int, str]]:
    spans = [(m.start(), m.end(), m.group().lower()) for m in _TOKEN_RE.finditer(name or "")]
    # The splitter cuts `ipv4` into ipv|4 and `IPv4` into i|pv|4; join them back into one word.
    out: list[tuple[int, int, str]] = []
    for s, e, w in spans:
        if w in ("4", "6") and out and out[-1][1] == s:
            if out[-1][2] == "ipv":
                out[-1] = (out[-1][0], e, "ipv" + w)
                continue
            if out[-1][2] == "pv" and len(out) > 1 and out[-2][2] == "i" and out[-2][1] == out[-1][0]:
                out[-2:] = [(out[-2][0], e, "ipv" + w)]
                continue
        out.append((s, e, w))
    return out


def _mobile_is_number(words: list[str], i: int) -> bool:
    nxt = words[i + 1] if i + 1 < len(words) else None
    prev = words[i - 1] if i > 0 else None
    if prev in ("is", "has"):
        return False  # a yes/no about the device
    return nxt is None or nxt.isdigit() or nxt in _MOBILE_NUMBER_WORDS


def _address_is_personal(words: list[str], i: int) -> bool:
    return i == 0 or words[i - 1] not in _MACHINE_ADDRESS_WORDS


def _has_sequence(words: list[str], seq: tuple[frozenset, ...]) -> bool:
    n = len(seq)
    return any(all(words[i + k] in seq[k] for k in range(n)) for i in range(len(words) - n + 1))


def _word_rules(words: list[str]) -> bool:
    for i, w in enumerate(words):
        if w in _PII_WORDS:
            return True
        if w in ("address", "addresses", "addr") and _address_is_personal(words, i):
            return True
        if w == "mobile" and _mobile_is_number(words, i):
            return True
        if w == "pan" and len(words) == 1:
            return True
    return any(_has_sequence(words, s) for s in _PII_SEQUENCES)


def person_name(name: str) -> bool:
    """`customer_name`, `contactName`, `username`, `first_name` … — a person's name, any table."""
    words = tokens(name)
    for i, w in enumerate(words):
        if w == "name" and i > 0 and words[i - 1] in PERSON_WORDS:
            return True
        if w.endswith("name") and w[:-4] in PERSON_WORDS | {"first", "last", "full", "middle", "sur"}:
            return True
    return _has_sequence(words, _PII_SEQUENCES[-1])


def _legacy_hit_is_non_person(name: str, start: int, end: int, spans: list[tuple[int, int, str]]) -> bool:
    words = [w for _, _, w in spans]
    for i, (s, e, w) in enumerate(spans):
        if not (s <= start < e):
            continue
        if end > e:
            return False  # the hit runs across words (`account_no`): a real match
        if w in NON_PII_WORDS:
            return True
        if w == "mobile":
            return not _mobile_is_number(words, i)
        if w in ("address", "addresses"):
            return not _address_is_personal(words, i)
        return False
    return False


def legacy_strong(name: str) -> bool:
    """The older substring rule, minus hits that sit wholly inside a known non-person word."""
    hits = list(STRONG_PII_RE.finditer(name or ""))
    if not hits:
        return False
    spans = _token_spans(name)
    return not all(_legacy_hit_is_non_person(name, m.start(), m.end(), spans) for m in hits)


def strong_pii_name(name: str) -> bool:
    """Personal data by name alone, whatever the table."""
    return _word_rules(tokens(name)) or person_name(name) or legacy_strong(name)


def detect_sensitive(table_name: str, column_name: str) -> bool:
    """Sensitive by name: the strong rules anywhere; a bare `name` only inside a PII-ish table."""
    if strong_pii_name(column_name):
        return True
    table_pii = table_name.upper() == "PII" or bool(STRONG_PII_RE.search(table_name)) \
        or bool(WEAK_PII_RE.search(table_name)) \
        or any(w.rstrip("s") in PERSON_WORDS or w in ("people", "pii") for w in tokens(table_name))
    return bool(WEAK_PII_RE.search(column_name)) and table_pii


def suspected_pii(column_name: str) -> bool:
    """A BROADER 'might be PII' name rule than `detect_sensitive`, for the review tier: it also
    keeps every older substring hit (`mobile_sessions`, `ip_address`), so a column the strict
    rule stopped flagging is still listed for a person to confirm. Never auto-marks."""
    return bool(strong_pii_name(column_name) or STRONG_PII_RE.search(column_name)
                or WEAK_PII_RE.search(column_name) or _EXTRA_PII_RE.search(column_name))


# --- values ------------------------------------------------------------------

_EMAIL_VALUE_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_PHONE_VALUE_RE = re.compile(r"^\+?[\d\s().-]{7,24}$")
_DIGITS_RE = re.compile(r"\d")
_NUMBER_VALUE_RE = re.compile(r"^-?\d+\.\d+$")
# Four dot-separated groups is an IPv4 address; a dotted phone number (555.123.4567) has three.
_IPV4_VALUE_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# Midnight may carry a zone (`Z`, `+00`, `+05:30`): a timestamptz DOB reads back that way.
_DATE_ONLY_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[ T]00:00(?::00(?:\.0+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?$")
_ID_NAME_RE = re.compile(r"(^|_)(id|key|sk|code|hash|uuid|guid)s?$", re.IGNORECASE)
_ID_WORDS = frozenset({"id", "ids", "key", "keys", "sk", "code", "codes", "hash", "uuid", "guid"})
_BIRTH_HINT_WORDS = frozenset({"born", "bday", "birth", "birthday", "birthdate", "dob", "dateofbirth"})
# The share of non-empty sampled values a rule needs, so one stray address in a notes column
# doesn't flag the column.
VALUE_SHARE = 0.8

PHONE_SUSPECT_CAVEAT = "Values look like phone numbers; may be personal data (review)."
DOB_SUSPECT_CAVEAT = "Values look like dates of birth; may be personal data (review)."
VALUE_SUSPECT_CAVEATS = {PHONE_SUSPECT_CAVEAT: "values look like phone numbers",
                         DOB_SUSPECT_CAVEAT: "values look like dates of birth"}
JSON_PII_CAVEAT_PREFIX = "Contains personal data at: "


def _present(values: Iterable[Any]) -> list[str]:
    return [str(v).strip() for v in values if v is not None and str(v).strip() != ""]


def _share(vals: list[str], ok) -> bool:
    return bool(vals) and sum(1 for v in vals if ok(v)) >= VALUE_SHARE * len(vals)


def personal_value(v: Any) -> bool:
    """An email- or phone-shaped value, which must never become a choice-list key."""
    s = str(v).strip()
    return bool(_EMAIL_VALUE_RE.match(s)) or (not s.isdigit() and _phone_like(s, False))


def looks_like_emails(values: Iterable[Any]) -> bool:
    return _share(_present(values), _EMAIL_VALUE_RE.match)


def _phone_like(v: str, digits_only_ok: bool) -> bool:
    if (not _PHONE_VALUE_RE.match(v) or _DATE_ONLY_RE.match(v) or _NUMBER_VALUE_RE.match(v)
            or _IPV4_VALUE_RE.match(v)):
        return False
    n = len(_DIGITS_RE.findall(v))
    if not 7 <= n <= 15:
        return False
    if v.isdigit():
        return digits_only_ok and n >= 10  # a bare number is a phone only at phone length
    return True


def _id_named(column_name: str) -> bool:
    """`order_id`, `customerId`, `trackingCode`, `id_number`: an identifier, whose bare digits
    are not a phone. Trailing number words are dropped, so `id_no` ends in `id`."""
    if _ID_NAME_RE.search(column_name or ""):
        return True
    words = tokens(column_name)
    while len(words) > 1 and words[-1] in _NUMBER_WORDS:
        words.pop()
    return bool(words) and words[-1] in _ID_WORDS


def looks_like_phones(column_name: str, values: Iterable[Any]) -> bool:
    digits_ok = not _id_named(column_name)
    return _share(_present(values), lambda v: _phone_like(v, digits_ok))


def phone_key(column_name: str, column_type: str, v: Any) -> bool:
    """One list key that reads as a phone number on this column: bare digits count unless the
    column is an identifier. Judged per key, as one key written into the model is one leak."""
    return column_type in ("string", "integer") and _phone_like(str(v).strip(), not _id_named(column_name))


def _plausible_birth(v: str) -> bool:
    m = _DATE_ONLY_RE.match(v)
    if not m:
        return False
    y = int(m.group(1))
    return 1900 <= y <= date.today().year - 13


def looks_like_birth_dates(column_name: str, values: Iterable[Any]) -> bool:
    """Name-gated: most dates in a table are events, not birthdays."""
    if not _BIRTH_HINT_WORDS & set(tokens(column_name)):
        return False
    return _share(_present(values), _plausible_birth)


def value_suspect_caveat(column_name: str, column_type: str, values: list) -> Optional[str]:
    """The suspected-tier caveat the sample earns, if any: phone- or birth-date-shaped values."""
    if column_type in ("string", "integer") and looks_like_phones(column_name, values):
        return PHONE_SUSPECT_CAVEAT
    if column_type in ("string", "date", "timestamp") and looks_like_birth_dates(column_name, values):
        return DOB_SUSPECT_CAVEAT
    return None


# --- JSON --------------------------------------------------------------------

JSON_MAX_DEPTH = 3
JSON_MAX_KEYS = 50
# Python-literal fallback is bounded: a larger value is not worth parsing for a key scan.
_LITERAL_MAX_CHARS = 64 * 1024
# A path segment is reported only when it reads like a field name; anything else may be data
# (a person's name used as a key) and is shown as `*`.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _as_object(v: Any) -> Optional[dict]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.lstrip().startswith("{"):
        try:
            obj = json.loads(v)
        except ValueError:
            obj = _literal_object(v)
        return obj if isinstance(obj, dict) else None
    return None


def _literal_object(v: str) -> Optional[dict]:
    """A Postgres json/jsonb value read through the CLI arrives as a Python repr
    (`{'email': ...}`). literal_eval only builds literals; it never runs code."""
    if len(v) > _LITERAL_MAX_CHARS:
        return None
    try:
        obj = ast.literal_eval(v.strip())
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _walk(obj: dict, prefix: tuple, depth: int, leaves: dict[tuple, list], seen: set[tuple]) -> None:
    """Collect leaf values per key path; `seen` gets (depth, key) for every key read."""
    for k, v in obj.items():
        if not isinstance(k, str):
            continue
        path = (*prefix, k)
        if path not in leaves:
            if len(leaves) >= JSON_MAX_KEYS:
                continue
            leaves[path] = []
        seen.add((depth, k))
        items = v if isinstance(v, list) else [v]
        for item in items[:5]:
            if isinstance(item, dict):
                if depth < JSON_MAX_DEPTH:
                    _walk(item, path, depth + 1, leaves, seen)
            elif item is not None and not isinstance(item, list):
                leaves[path].append(item)


def _shown_path(path: tuple, counts: dict[tuple, int], n_objects: int) -> str:
    def trusted(i: int) -> bool:
        if not _IDENT_RE.match(path[i]):
            return False
        # A field name recurs across rows; a key that is data (a person's name) mostly doesn't.
        return n_objects < 2 or counts.get((i + 1, path[i]), 0) * 2 >= n_objects
    return ".".join(seg if trusted(i) else "*" for i, seg in enumerate(path))


def json_pii_paths(table_name: str, values: Iterable[Any]) -> list[str]:
    """Key paths inside JSON-object values that hold personal data, by key name or by
    email-shaped values (`email`, `contact.phone`). Empty when no value is a JSON object.
    A segment that may itself be data is shown as `*` (`*.email`)."""
    leaves: dict[tuple, list] = {}
    counts: dict[tuple, int] = {}
    n_objects = 0
    for v in values:
        obj = _as_object(v)
        if obj is not None:
            n_objects += 1
            seen: set[tuple] = set()
            _walk(obj, (), 1, leaves, seen)
            for p in seen:
                counts[p] = counts.get(p, 0) + 1
    hits: list[tuple] = []
    for path, vals in leaves.items():
        if any(path[:i] in hits for i in range(1, len(path))):
            continue  # its parent is already named
        by_name = detect_sensitive(table_name, path[-1]) or strong_pii_name("_".join(path))
        if by_name or looks_like_emails(vals):
            hits.append(path)
    return list(dict.fromkeys(_shown_path(p, counts, n_objects) for p in hits))


def json_pii_caveat(paths: list[str]) -> str:
    return JSON_PII_CAVEAT_PREFIX + ", ".join(paths)


def suspected_reason(name: str, caveats: Iterable[str]) -> Optional[str]:
    """Why an unflagged column is in the suspected tier: its name, or a value caveat the
    sample left on it. None when it isn't."""
    for c in caveats:
        if c in VALUE_SUSPECT_CAVEATS:
            return VALUE_SUSPECT_CAVEATS[c]
    return "name" if suspected_pii(name) else None


__all__ = [
    "STRONG_PII_RE", "WEAK_PII_RE", "PERSON_WORDS", "NON_PII_WORDS", "tokens", "person_name",
    "strong_pii_name", "legacy_strong", "detect_sensitive", "suspected_pii", "looks_like_emails",
    "looks_like_phones", "looks_like_birth_dates", "value_suspect_caveat", "json_pii_paths",
    "json_pii_caveat", "suspected_reason", "VALUE_SUSPECT_CAVEATS", "JSON_PII_CAVEAT_PREFIX",
]
