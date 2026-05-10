"""
Tests for plugins/agami/scripts/reconcile.py.

Covers the deterministic parts of the reconciliation harness:
- Number-string parsing (currency, magnitudes, percentages, accounting parens)
- CSV parsing (header detection, multi-column labels)
- Diff with tolerance
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from reconcile import diff, parse_csv, parse_value  # noqa: E402


# --- parse_value ---------------------------------------------------------

class TestParseValue:
    def test_plain_int(self):
        assert parse_value("47238221") == 47238221.0

    def test_plain_float(self):
        assert parse_value("148.95") == 148.95

    def test_with_thousands_commas(self):
        assert parse_value("47,238,221") == 47238221.0
        assert parse_value("47,238,221.00") == 47238221.0

    def test_currency_symbol_dollar(self):
        assert parse_value("$148.95") == 148.95
        assert parse_value("$47,238,221") == 47238221.0

    def test_currency_symbol_other(self):
        assert parse_value("€100") == 100.0
        assert parse_value("£250.50") == 250.5
        assert parse_value("₹2,162,087") == 2162087.0

    def test_iso_currency_code(self):
        assert parse_value("USD 148.95") == 148.95
        assert parse_value("INR 2,162,087") == 2162087.0

    def test_magnitude_suffix_M(self):
        assert parse_value("4.2M") == 4_200_000.0
        assert parse_value("$4.2M") == 4_200_000.0

    def test_magnitude_suffix_K(self):
        assert parse_value("12.5K") == 12_500.0

    def test_magnitude_suffix_B(self):
        assert parse_value("1.5B") == 1_500_000_000.0
        assert parse_value("1.5Bn") == 1_500_000_000.0

    def test_magnitude_suffix_indian_crore(self):
        assert parse_value("2.16Cr") == 21_600_000.0
        assert parse_value("₹2.16Cr") == 21_600_000.0

    def test_magnitude_suffix_indian_lakh(self):
        assert parse_value("1.2L") == 120_000.0

    def test_percent(self):
        assert parse_value("42%") == 0.42
        assert parse_value("12.4%") == 0.124

    def test_negative_accounting(self):
        assert parse_value("(123.45)") == -123.45
        assert parse_value("($1,234.56)") == -1234.56

    def test_negative_sign(self):
        assert parse_value("-100") == -100.0

    def test_whitespace(self):
        assert parse_value("  148.95 ") == 148.95
        assert parse_value("\t47238221\n") == 47238221.0

    def test_null_sentinels(self):
        assert parse_value("") is None
        assert parse_value(" ") is None
        assert parse_value("n/a") is None
        assert parse_value("N/A") is None
        assert parse_value("—") is None
        assert parse_value("null") is None

    def test_none_input(self):
        assert parse_value(None) is None

    def test_passthrough_numeric(self):
        assert parse_value(148.95) == 148.95
        assert parse_value(47238221) == 47238221.0

    def test_boolean_not_treated_as_number(self):
        # bool is a subclass of int in Python; we explicitly reject it.
        assert parse_value(True) is None
        assert parse_value(False) is None

    def test_unparseable(self):
        assert parse_value("not a number") is None
        assert parse_value("12 apples") is None


# --- diff ----------------------------------------------------------------

class TestDiff:
    def test_exact_match(self):
        r = diff(100.0, 100.0)
        assert r["match"] is True
        assert r["delta"] == 0.0
        assert r["reason"] == "match"

    def test_within_default_tolerance(self):
        # 1% tolerance — 100 vs 100.5 should match (delta 0.5%).
        r = diff(100.0, 100.5)
        assert r["match"] is True
        assert r["delta"] == 0.5
        assert pytest.approx(r["delta_pct"], abs=1e-6) == 0.005

    def test_outside_tolerance(self):
        r = diff(100.0, 105.0)
        assert r["match"] is False
        assert r["delta"] == 5.0
        assert r["reason"] == "mismatch"

    def test_custom_tolerance(self):
        # ±5% — 100 vs 104 matches.
        r = diff(100.0, 104.0, tolerance=0.05)
        assert r["match"] is True

    def test_negative_delta(self):
        r = diff(100.0, 95.0)
        assert r["delta"] == -5.0
        assert r["delta_pct"] == -0.05

    def test_zero_expected_exact(self):
        r = diff(0.0, 0.0)
        assert r["match"] is True

    def test_zero_expected_nonzero_actual(self):
        r = diff(0.0, 1.0)
        assert r["match"] is False

    def test_missing_expected(self):
        r = diff(None, 100.0)
        assert r["match"] is False
        assert r["reason"] == "missing_expected"

    def test_missing_actual(self):
        r = diff(100.0, None)
        assert r["match"] is False
        assert r["reason"] == "missing_actual"


# --- parse_csv -----------------------------------------------------------

class TestParseCsv:
    def test_simple_two_column_with_header(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("Label,Value\n")
            f.write("Q3 Revenue,$4.2M\n")
            f.write("Active customers,12450\n")
            path = f.name

        rows = parse_csv(path)
        assert len(rows) == 2
        assert rows[0]["label"] == "Q3 Revenue"
        assert rows[0]["expected_value"] == 4_200_000.0
        assert rows[0]["raw_value"] == "$4.2M"
        assert rows[1]["label"] == "Active customers"
        assert rows[1]["expected_value"] == 12450.0

    def test_no_header(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("Q3 Revenue,4200000\n")
            f.write("Active customers,12450\n")
            path = f.name

        rows = parse_csv(path)
        assert len(rows) == 2
        assert rows[0]["label"] == "Q3 Revenue"

    def test_three_column_appends_extras_to_label(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("Metric,Value,Quarter\n")
            f.write("Revenue,4.2M,Q3 2025\n")
            path = f.name

        rows = parse_csv(path)
        assert len(rows) == 1
        assert rows[0]["label"] == "Revenue (Q3 2025)"
        assert rows[0]["expected_value"] == 4_200_000.0

    def test_skips_empty_rows(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("Label,Value\n")
            f.write("\n")  # empty
            f.write(",,\n")  # all-empty
            f.write("Revenue,100\n")
            path = f.name

        rows = parse_csv(path)
        assert len(rows) == 1
        assert rows[0]["label"] == "Revenue"

    def test_unparseable_value_kept_as_null(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("Label,Value\n")
            f.write("Status,active\n")  # not a number
            f.write("Revenue,100\n")
            path = f.name

        rows = parse_csv(path)
        assert len(rows) == 2
        assert rows[0]["expected_value"] is None
        assert rows[0]["raw_value"] == "active"
        assert rows[1]["expected_value"] == 100.0

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            parse_csv("/nonexistent/path.csv")
