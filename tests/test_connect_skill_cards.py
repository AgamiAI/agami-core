"""agami-connect Phase 1.6 asks for tables on a card, not on a prune page."""

from __future__ import annotations

import re
from pathlib import Path

SKILL = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "agami"
    / "skills"
    / "agami-connect"
    / "SKILL.md"
)


def _section_16() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("### 1.6 ")
    end = text.index("### 1.6s ")
    return text[start:end]


def test_skill_links_the_card_conventions():
    assert "shared/question-cards.md" in SKILL.read_text(encoding="utf-8")


def test_1_6_uses_the_summary_and_the_four_options():
    s = _section_16()
    assert "inventory_summary.py" in s
    for label in (
        "Keep everything",
        "Drop staging and backups",
        "Only some schemas",
        "Let me name them",
    ):
        assert label in s
    assert "AskUserQuestion" in s
    assert "--tables-file" in s


def test_1_6_default_path_opens_no_page_and_waits_for_no_paste():
    default = _section_16().split("**Show the full view")[0]
    assert not re.search(r"auto-open|end the turn|paste the block", default, re.I)
    assert "AGAMI PRUNE" not in default


def test_1_6_keeps_the_full_view_as_an_option():
    s = _section_16()
    assert "Show the full view" in s
    assert "render_prune.py" in s and "serve_page.py" in s
    assert "parse_prune_block.py" in s


def test_1_6_handles_all_noise_and_the_empty_result():
    s = _section_16()
    assert "no tables kept" in s
    assert "covers every table" in s


def test_1_6_follow_up_card_stays_within_four_options():
    s = _section_16()
    assert "fewer than 4" in s
    assert "single schema" in s.lower()


def test_1_6_lists_the_first_noise_names_before_the_card():
    s = _section_16()
    assert "first 5" in s and "and N more" in s
    assert "schema_count" in s


# --- Phase 0 and 0a: start and connect as cards, with the one-time password page ------------------

HELPER = SKILL.parent.parent.parent / "scripts" / "connect_helper.py"


def _between(start: str, end: str) -> str:
    text = SKILL.read_text(encoding="utf-8")
    i = text.index(start)
    return text[i : text.index(end, i + len(start))]


def _helper_constant(name: str) -> object:
    """A module-level constant of connect_helper.py, read from its source so the test needs no keyring."""
    import ast

    tree = ast.parse(HELPER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in connect_helper.py")


def _hard_rules() -> str:
    return _between("### HARD RULES", "### Preflight steps")


def _password_page() -> str:
    return _between("### 0a.4k ", "\n### ")


def test_start_card_offers_first_run_and_returning_options():
    s = _between("## Phase 0: Preflight", "## Phase 0a:")
    assert "AskUserQuestion" in s
    for label in ("Connect a database", "Try the sample data"):
        assert label in s
    for label in (
        "Ask a question (Recommended)",
        "Reload the schema",
        "Connect another database",
    ):
        assert label in s


def test_hard_rules_forbid_secrets_in_chat_but_allow_the_field_cards():
    rules = _hard_rules()
    assert "Never ask for a password in chat" in rules
    for secret in ("password", "token", "key", "DSN"):
        assert secret in rules
    # Host, port, database and user are asked on cards now; the rule says so and why.
    assert "cards" in rules and "host" in rules


def test_database_type_card_has_no_recommended_option():
    s = _between("### 0a.2 ", "### 0a.3 ")
    assert "AskUserQuestion" in s
    assert "(Recommended)" not in s.replace("no `(Recommended)`", "")


def test_connection_fields_are_one_card_with_defaults_and_no_password():
    s = _between("### 0a.4c ", "### 0a.4k ")
    assert "AskUserQuestion" in s
    assert "5432" in s and "3306" in s
    assert "Other" in s
    assert "password" in s.lower()  # says the password is not on this card


def test_password_page_runs_the_helper_in_the_background():
    s = _password_page()
    assert "connect_helper.py" in s
    assert "--spec" in s
    assert "run_in_background" in s
    assert "expires_in" in s
    assert "local/tmp/" in s  # the spec file goes in the scratch folder, never /tmp


def test_password_page_says_how_long_the_link_lasts():
    assert _helper_constant("EXPIRES_IN") == 300
    assert "expires after 5 minutes" in _password_page()


def test_password_page_posts_the_link_with_the_spec_sentence():
    assert "Enter the password on this page; it goes to your keychain, not the chat" in _password_page()


def test_password_page_maps_every_status_and_reason():
    s = _password_page()
    for status in ("connected", "connect_failed", "refused", "expired", "missing_driver"):
        assert f"`{status}`" in s, status
    reasons = _helper_constant("REASONS")
    assert reasons  # the helper's own list, so a new reason fails here until the skill handles it
    for reason in reasons:
        assert f"`{reason}`" in s, reason


def test_password_page_offers_secure_storage_and_driver_install_cards():
    s = _password_page()
    assert "Set up secure storage" in s
    assert "Use a file instead (manual)" in s
    assert 'scripts/sm" install' in s
    assert "Install <package>" in s and "Not now" in s
    assert 'scripts/py" -m pip install --user' in s
    assert "Try again" in s


def test_password_page_has_an_sso_path_with_no_link():
    s = _password_page()
    assert '"auth": "sso"' in s
    assert "no link" in s.lower()


def test_password_page_never_puts_the_password_in_chat_or_the_spec():
    s = _password_page()
    assert "never" in s.lower() and "password" in s.lower()
    assert '"password"' not in s  # the spec JSON carries no password field


def test_mac_and_windows_path_never_asks_to_say_introspect():
    for start, end in (("### 0a.4c ", "### 0a.4k "), ("### 0a.4k ", "\n### ")):
        assert not re.search(r"say\W+introspect my database", _between(start, end), re.I)


def test_linux_path_keeps_the_file_and_asks_with_a_card():
    s = _between("### 0a.4 ", "## Phase 0s:")
    assert "promote_credentials.py" in s
    assert "credentials.example" in s
    assert "I've filled it in" in s
    assert "Show me the file again" in s
    assert not re.search(r"say\W+.?introspect my database", s, re.I)
    assert "END THE TURN" not in s


# --- fix round 1 ---------------------------------------------------------------------------------


def _start_card() -> str:
    return _between("1a. **The start card**", "2. **Update-check")


def test_password_page_treats_a_first_status_line_as_final():
    s = _password_page()
    assert "first line has a `status`" in s


def test_password_page_wait_is_bounded_and_checks_for_exit():
    s = _password_page()
    assert "15 seconds" in s
    assert "has exited" in s
    assert "stderr" in s
    assert "tight loop" in s


def test_password_page_names_how_to_stop_the_helper():
    s = _password_page()
    assert "TaskStop" in s and "PID" in s


def test_returning_card_reloads_the_schema_instead_of_review():
    s = _start_card()
    assert "Reload the schema" in s
    assert "`Review the model`" not in s
    assert "reintrospect" in s


def test_returning_card_routes_other_text_by_what_it_asks_for():
    s = _start_card()
    assert "re-introspect" in s.lower() and "add a schema" in s.lower()
    assert "only a question goes to agami-query" in s.lower()


def test_connect_another_database_passes_through_1_1a():
    s = _start_card()
    row = next(line for line in s.splitlines() if "`Connect another database`" in line)
    assert "1.1a" in row


def test_ready_branch_matches_the_start_card():
    text = SKILL.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("1. **Resolve the environment**"))
    assert "a reload or re-introspect request skips the card" in line


def test_fields_card_prompt_rules_out_passwords_and_urls():
    s = _between("### 0a.4c ", "### 0a.4k ")
    assert "never a password or a URL that holds one" in s


# --- Phase 1.7 / 2 progress, Phase 4 review, Phase 6 seed check: cards, not pages -----------------


def _phase(start: str, end: str) -> str:
    return _between(start, end)


def _phase_4() -> str:
    return _phase("## Phase 4:", "## Phase 5:")


def _phase_6() -> str:
    return _phase("## Phase 6:", "## Phase 7:")


def _default_path(section: str) -> str:
    """The part of a section before its optional full view."""
    return section.split("**Show the full view")[0]


def test_build_progress_lines_carry_only_the_engines_numbers():
    s = _phase("### 1.7 ", "### 1.8 ")
    assert "progress line" in s.lower()
    assert "from the engine's output" in s
    assert "never estimate" in s.lower() or "nothing invented" in s.lower()
    assert 'origin: "fk"' in s  # declared foreign keys are counted from review-items, not guessed


def test_enrich_phase_names_a_progress_line_per_step_with_its_source():
    s = _phase("## Phase 2:", "### 2a.0 ")
    assert "progress line" in s.lower()
    for source in ("enrich-metadata", "coverage_pct", "suggest-metrics"):
        assert source in s, source
    assert "one finding" in s.lower()


def test_4a_default_path_opens_no_explorer_and_waits_for_no_paste():
    default = _default_path(_phase_4())
    assert not re.search(r"end the turn|paste|feedback block", default, re.I)
    assert "invoke `/agami-model preseed`" not in default
    assert "AskUserQuestion" in default


def test_4a_reads_the_queue_and_the_personal_data_columns_from_the_engine():
    s = _phase_4()
    assert 'sm" review-items "$ROOT" --scope preseed' in s
    assert 'sm" sensitive "$ROOT"' in s
    assert "extra_lines" in s


def test_4a_review_card_has_the_three_options():
    s = _phase_4()
    for label in ("Approve all", "Review one by one", "Skip for now"):
        assert label in s, label
    assert "(Recommended)" in s


def test_4a_one_by_one_is_four_per_card_with_approve_all_remaining():
    s = _phase_4()
    assert "up to 4" in s
    assert "Approve all remaining" in s
    assert "Approve" in s and "Reject" in s


def test_4a_other_is_a_change_applied_with_curate_then_reasked():
    s = _phase_4()
    assert '"op":"edit"' in s and 'sm" curate' in s
    assert "ask about that item again" in s.lower()
    assert "never a generator script" in s.lower() or "never hand-edit" in s.lower()


def test_4a_unparseable_other_is_reasked_never_guessed():
    s = _phase_4()
    assert "ask the same card again" in s.lower()
    assert "never guess" in s.lower()


def test_4a_personal_data_card_hides_columns_with_exclude_ops():
    s = _phase_4()
    assert "Hide these from queries" in s
    assert "Keep them all queryable" in s
    assert "multiSelect" in s
    assert '"op":"exclude","kind":"table"' in s and '"column"' in s
    assert "grouped by table" in s.lower()


def test_4a_identity_card_asks_once_and_saves_to_config():
    s = _phase_4()
    assert "reviewer_email" in s and "reviewer_role" in s
    assert "chmod 600" in s
    assert "first approval" in s.lower()
    assert "never infer" in s.lower()
    assert "you@example.com" in s


def test_4a_keeps_the_explorer_as_the_full_view():
    s = _phase_4()
    assert "Show the full view" in s
    assert "serve_page.py" in s
    assert "/agami-model preseed" in s.split("**Show the full view")[1]


def test_4b_is_the_only_place_after_review_is_passed():
    text = SKILL.read_text(encoding="utf-8")
    four_b = _between("**4b ", "## Phase 5:")
    assert "--after-review" in four_b
    assert "Skip for now" in four_b
    phase_5 = _phase("## Phase 5:", "## Phase 6:")
    assert "columns_unenriched" in phase_5 and "preseed_review_pending" in phase_5
    assert "open `/agami-model preseed`, end the turn" not in phase_5
    assert text.count("--after-review") == four_b.count("--after-review") + phase_5.count("--after-review")


def test_6_shows_a_table_of_seeds_then_the_four_option_card():
    s = _phase_6()
    assert 'sm" seed-validate' in s
    assert "15 rows" in s
    for label in (
        "Keep all that ran (Recommended)",
        "Review the failing ones",
        "Drop the failing ones",
        "Review all one by one",
    ):
        assert label in s, label


def test_6_keep_signs_with_add_example_and_drop_uses_remove_example():
    s = _phase_6()
    assert 'sm" add-example' in s and "--signer" in s and "--role" in s
    assert 'sm" remove-example' in s


def test_6_one_by_one_uses_keep_drop_and_other_for_a_fix():
    s = _phase_6()
    assert "Keep all remaining" in s
    assert "up to 4" in s
    assert "ask the same card again" in s.lower() and "never guess" in s.lower()


def test_6_default_path_renders_no_dashboard_and_waits_for_no_reply_grammar():
    default = _default_path(_phase_6())
    assert not re.search(r"end the turn|paste|Open it", default, re.I)
    assert "render_examples_validation.py" not in default


def test_6_keeps_the_examples_page_as_the_full_view():
    full = _phase_6().split("**Show the full view")[1]
    assert "render_examples_validation.py" in full and "serve_page.py" in full
    assert "validate N" in full  # its pasted block is still accepted
