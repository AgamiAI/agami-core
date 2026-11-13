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


def test_1_6_has_no_page_and_shows_tables_in_the_chat():
    # The prune page was removed: listing, picking and leaving columns out all happen in the chat.
    s = _section_16()
    for gone in ("render_prune.py", "parse_prune_block.py", "prune_html", "serve_page.py", "--out"):
        assert gone not in s, gone
    assert 'inventory_summary.py --inventory "<inventory_path>" --show' in s
    assert "--check-columns" in s and "--exclude-columns" in s


def test_1_6_handles_all_noise_and_the_empty_result():
    s = _section_16()
    assert "no tables kept" in s
    assert "covers every table" in s


def test_1_6_recommends_dropping_noise_only_when_every_flagged_table_is_a_copy():
    # `test_results` and `temp_readings` are real tables; recommending their drop lost data.
    s = _section_16()
    assert "noise_all_copies" in s and "copy_of" in s
    assert 'without "(Recommended)"' in s


def test_1_6_columns_in_other_go_to_exclude_columns_whatever_the_table_answer():
    s = _section_16()
    assert "neither `--tables-file` nor `--exclude-columns`" not in s
    keep_row = next(line for line in s.splitlines() if line.startswith("| Keep everything |"))
    assert "--exclude-columns" in keep_row and "still" in keep_row
    assert "whatever the table answer" in s


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
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
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
    assert (
        "Enter the password on this page; it goes to your keychain, not the chat"
        in _password_page()
    )


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
    assert "/agami-model preseed" in s.split("**Show the full view** (an option")[1]


def test_4b_is_the_only_place_after_review_is_passed():
    text = SKILL.read_text(encoding="utf-8")
    four_b = _between("**4b ", "## Phase 5:")
    assert "--after-review" in four_b
    assert "Skip for now" in four_b
    phase_5 = _phase("## Phase 5:", "## Phase 6:")
    assert "columns_unenriched" in phase_5 and "preseed_review_pending" in phase_5
    assert "open `/agami-model preseed`, end the turn" not in phase_5
    assert text.count("--after-review") == four_b.count("--after-review") + phase_5.count(
        "--after-review"
    )


def test_6_shows_a_table_of_seeds_then_the_four_option_card():
    s = _phase_6()
    assert 'sm" seed-validate' in s
    assert "15 rows" in s
    for label in (
        "Keep all that ran (Recommended)",
        "Review one by one",
        "Drop the failing ones",
        "Mark them on a page",
    ):
        assert f"- **{label}**" in s, label
    assert "Review the failing ones" not in s and "Review all one by one" not in s


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
    full = _phase_6().split("**Show the full view** (an option")[1]
    assert "render_examples_validation.py" in full and "serve_page.py" in full
    assert "validate N" in full  # its pasted block is still accepted


# --- fix round 1 (Task 4) -----------------------------------------------------------------------


def test_personal_data_card_lists_and_hides_suspected_columns():
    s = _phase_4()
    assert "suspected_count" in s and "`suspected`" in s
    assert "(suspected)" in s
    assert "Flagged and suspected columns are picked the same way" in s


def test_an_other_that_asks_to_stop_goes_to_the_continue_or_pause_card():
    s = _phase_4()
    assert "asks to stop is a stop, not a change" in s
    assert "stop, pause or finish later" in _between("**4b ", "## Phase 5:")
    assert 'Any other "Other" you can\'t act on is re-asked, never guessed' in s


def test_review_summary_mentions_guessed_joins_without_a_card():
    s = _phase_4()
    assert 'review-items "$ROOT" --scope rule2' in s
    assert "joins were guessed from names and data" in s
    assert "Review them any time in agami-model" in s
    assert "not put on a card" in s


def test_phase_6_unsigned_identity_answer_continues_to_phase_7():
    s = _phase_6()
    assert "Don't sign anything yet" in s
    assert "goes on to Phase 7 (not 4b)" in s


def _card_headers(text: str) -> list[str]:
    headers = re.findall(r'"header":\s*"([^"]+)"', text)
    headers += re.findall(r"header `([^`]+)`", text)
    headers += re.findall(r"`((?:Metric|Entity|Example|Ex) \d+/\d+)`", text)
    return headers


def test_every_card_header_in_the_skill_fits_twelve_characters():
    headers = _card_headers(SKILL.read_text(encoding="utf-8"))
    assert len(headers) >= 10  # the pattern still finds the skill's headers
    too_long = [h for h in headers if len(h) > 12]
    assert not too_long, too_long


def _option_lists(text: str) -> list[list[str]]:
    """Each card's options: a JSON "options" array, or the `- **…**` bullets after an AskUserQuestion line."""
    lists = [re.findall(r'"label"', m) for m in re.findall(r'"options":\s*\[(.*?)\]', text, re.S)]
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "AskUserQuestion" not in line or not line.rstrip().endswith(":"):
            continue
        bullets, j = [], i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        while j < len(lines) and lines[j].startswith("- **"):
            bullets.append(lines[j])
            j += 1
        if bullets:
            lists.append(bullets)
    return lists


def test_no_card_example_in_the_skill_lists_more_than_four_options():
    lists = _option_lists(SKILL.read_text(encoding="utf-8"))
    assert lists  # the scan still finds the skill's cards
    too_many = [opts for opts in lists if len(opts) > 4]
    assert not too_many, too_many


# --- Final-review fixes: waiting on the helper, card limits, the closing flow ---------------------


def _helper_step_2() -> str:
    return _between("**2. Start the helper in the background.**", "**3. Post the link**")


def test_the_15_second_rule_is_for_the_password_path_only():
    s = _helper_step_2()
    fifteen = [p for p in s.split("\n\n") if "15 seconds" in p]
    assert fifteen and all("password path" in p for p in fifteen)
    # SSO tests the connection before it prints, so it waits for the exit, up to 5 minutes, said once.
    sso = next(p for p in s.split("\n\n") if "`auth: sso`" in p or '"auth": "sso"' in p)
    assert "browser sign-in may open" in sso
    assert "5 minutes" in sso and "once" in sso


def test_the_helper_wait_uses_monitor_never_a_foreground_sleep():
    s = _helper_step_2()
    assert "Monitor tool" in s
    assert "first line" in s and "exit" in s
    assert "fallback" in s.lower()
    assert not re.search(r"\bsleep \d", SKILL.read_text(encoding="utf-8"))


def test_the_final_line_is_also_waited_for_with_monitor():
    s = _between("**4. Read the final line**", "| final line |")
    assert "Monitor" in s


def test_1_2_asks_for_the_tables_on_a_card():
    s = _between("### 1.2 ", "### 1.3 ")
    assert "Paste the tables" not in s
    assert "AskUserQuestion" in s and "Other" in s
    assert "--keep names:" in s


def test_1_3_is_skipped_on_a_first_onboard_and_stays_within_four_options():
    s = _between("### 1.3 ", "### 1.4 ")
    assert "first onboard" in s and "Only some schemas" in s
    assert "at most 4" in s


def _card_labels(section: str) -> list[str]:
    return re.findall(r"^\| `([^`]+)` \|", section, re.M)


def test_1_5_context_card_asks_for_definitions_questions_and_reports():
    # A live review found onboarding never asked for the questions people ask or the reports they trust.
    s = _between("### 1.5 ", "### 1.6 ")
    assert "header `Context`" in s and "multiSelect" in s
    labels = _card_labels(s)
    assert labels == [
        "Definitions or a semantic layer",
        "Questions people ask",
        "Report SQL or dashboards",
        "Nothing to share",
    ]
    assert "spec workbook" in s and "dbt / LookML / Cube" in s
    assert "agami-asked-questions.txt" in s and "reconcile/inbox" in s


def test_1_4_asks_the_company_and_the_database_on_cards():
    s = _between("### 1.4 ", "### 1.5 ")
    assert "header `Company`" in s and "header `About it`" in s
    assert "**Skip for now**" in s and "**Draft it from the database**" in s
    assert "**Generate one for me (Recommended)** / **Skip**" in s
    # typed values go in the question text, never as an option (question-cards rule 5)
    assert "Write it now (Other field)" not in s
    assert 'sm" set-org' in s and "set-description" in s


def test_asked_questions_and_reports_are_used_after_the_build():
    text = SKILL.read_text(encoding="utf-8")
    phase5 = text[text.index("## Phase 5") : text.index("## Phase 6")]
    assert "agami-asked-questions.txt" in phase5 and "*(yours)*" in phase5
    six_b = text[text.index("### 6b ") : text.index("## Phase 7")]
    assert "header `Reports`" in six_b and "**Check them now (Recommended)** / **Later**" in six_b
    assert "agami-reconcile" in six_b


def test_6a_asks_its_fix_on_a_card():
    s = _between("### 6a ", "## Phase 7:")
    assert "AskUserQuestion" in s
    assert "> •" not in s
    for label in ("Fix it everywhere (Recommended)", "Just this example", "Leave it for now"):
        assert label in s


def test_review_from_phase_7_returns_to_the_starter_questions():
    s = _between("## Phase 7:", "## Phase 8:")
    line = next(ln for ln in s.splitlines() if "Review what needs sign-off" in ln)
    assert "Phase 8" in line


def test_phase_2c_points_at_the_phase_4_cards_not_the_explorer():
    text = SKILL.read_text(encoding="utf-8")
    assert "in bulk in the explorer" not in text
    assert "on the Phase 4 cards" in _between("### 2c ", "### 2d ")


def test_the_numbered_list_fallback_is_stated():
    text = SKILL.read_text(encoding="utf-8")
    assert "Where AskUserQuestion is unavailable, ask the same as a short numbered list" in text
    style = _between("## Conversation style", "## Progress tracking")
    assert "never inline-bullet options" not in style or "unavailable" in style


def test_8_5_link_candidates_card_stays_within_four_options():
    s = _between("## Phase 8.5:", "## Error handling")
    assert "at most 4" in s


def test_the_sample_wrap_up_offers_its_starter_questions_on_a_card():
    # A live run of the sample path ended in a numbered list to type a number back to.
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("7. **Wrap up")
    wrap_up = text[start : text.index('8. **The "where to go deeper"', start)]
    assert "header `Ask`" in wrap_up
    assert "A numbered list of ~6 starter questions" not in wrap_up
    options = [line for line in wrap_up.splitlines() if line.strip().startswith("| `")]
    assert 2 <= len(options) <= 4


def test_the_name_card_follows_the_fields_and_offers_the_database_name():
    # A live run asked "what should I call this database?" and then "database name": the same question twice.
    text = SKILL.read_text(encoding="utf-8")
    order = next(
        line
        for line in text.splitlines()
        if line.startswith("- **macOS and Windows: the password page.**")
    )
    assert order.index("0a.4c") < order.index("0a.3")
    s = _between("### 0a.3 ", "### 0a.4c ")
    assert "What should agami call this connection?" in s and "`<database> (Recommended)`" in s


def test_the_password_link_comes_with_a_card():
    # A live run showed the link in a line of chat being missed.
    text = SKILL.read_text(encoding="utf-8")
    step = text[text.index("**3. Post the link**") : text.index("**4. Read the final line**")]
    assert "header `Password`" in step
    assert "**I've saved it** / **New link** / **Use a file instead (manual)**" in step
    assert "no card, nothing to paste" not in step


def test_sign_off_shows_the_sql_each_definition_maps_to():
    # Live-run feedback: a definition was signed off from its words alone.
    for skill in ("agami-connect", "agami-model"):
        text = (SKILL.parent.parent / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "`Kind | Name | What it means | SQL`" in text, skill
        assert "**The SQL goes in the `Approve` option's `preview`**" in text, skill
        assert "Never put the SQL in the question" in text, skill


def test_the_onboarding_steps_are_shown_in_plain_words():
    # Live-run feedback: show where the person is in onboarding, the same way every time.
    text = SKILL.read_text(encoding="utf-8")
    section = text[text.index("## Progress tracking") : text.index("## Phase −1")]
    steps = [
        line.split(". ", 1)[1] for line in section.splitlines() if line[:2].rstrip(".").isdigit()
    ]
    assert steps[0] == "Connect your database" and steps[-1] == "Summary and first question"
    for jargon in ("ntrospect", "nrich", "NL→SQL", "Preflight"):
        assert not any(jargon in s for s in steps), jargon
    assert "never add your own working steps" in section


def test_an_empty_database_is_said_before_the_build_goes_on():
    text = SKILL.read_text(encoding="utf-8")
    assert "**Empty tables — say so before going on.**" in text
    assert "header `Empty`: **Connect a different database (Recommended)**" in text


def test_the_start_card_does_not_recommend_asking_an_empty_database():
    text = SKILL.read_text(encoding="utf-8")
    assert "**An empty database gets no recommendation to ask it.**" in text


def test_finished_steps_carry_their_numbers():
    # Live-run feedback: the step list should fill in counts as they're learned.
    text = SKILL.read_text(encoding="utf-8")
    section = text[text.index("## Progress tracking") : text.index("## Phase −1")]
    assert "**When a step finishes, add its numbers to its label**" in section
    assert "never an estimate" in section


def test_sign_off_titles_are_the_decision_in_plain_words():
    # Live-run feedback: "Approve ela_proficiency_rate? 48.8% of ..." mixed an identifier and a result into the title.
    for skill in ("agami-connect", "agami-model"):
        text = (SKILL.parent.parent / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "The question is only the decision, with the item's `plain_name`" in text, skill
        assert "kept apart from the title" in text, skill


# --- tester feedback on the question-cards branch ----------------------------------------------


def test_example_cards_show_the_sql_in_the_keep_preview():
    # Checking an example took typing "show me the SQL"; a metric card already shows its SQL.
    s = _phase_6()
    one_by_one = s[s.index("**One by one.**"):]
    assert "**The SQL goes in the `Keep` option's `preview`**" in one_by_one
    assert "`sql`" in one_by_one and "row_preview" in one_by_one


def test_review_cards_offer_the_full_view_as_an_option():
    # "show the full view" worked only typed in Other, and no card said it existed. #651: the page now
    # saves its own decisions, so the option says what it does, on every question of a card.
    four, six = _phase_4(), _phase_6()
    assert four.count("- **Review on a page**") == 2      # the Sign-off card and the item cards
    assert six.count("- **Mark them on a page**") == 1     # the Examples card
    assert "and **Mark them on a page** (the page below, on every question of the card" in six  # item cards
    assert "| Review on a page |" in four and "| Mark them on a page |" in six
    assert "first question only" not in four + six
    phase_7 = _phase("## Phase 7", "## Phase 8")
    assert "`Show the full view`" in phase_7


def test_currency_and_flag_rate_asks_name_columns_in_words():
    text = SKILL.read_text(encoding="utf-8")
    assert "What currency is the Price column on Widgets in?" in text
    assert "naming each by its `label`, never as `table.column`" in text
    assert "`flag_rates.labels`" in text
    assert "share-of-true rate — say if" not in text


def _join_candidates() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("**Join candidates (tested, not written).**")
    return text[start:text.index("Never accept them all unasked")]


def test_join_candidates_are_listed_in_the_chat_before_the_card():
    s = _join_candidates()
    assert "Show every join before asking about any of them" in s
    assert "| # | Join" in s and "leads.converted_contact_id → contacts.id" in s
    assert "evidence.match_rate" in s and "low_overlap" in s


def test_join_candidate_group_options_preview_the_joins_they_add():
    s = _join_candidates()
    assert "each option's `preview` lists every join that option adds" in s
    assert "Never offer a numbered range" in s
    assert "Add all remaining" in s and "Add none" in s


# --- pages that save their own decisions (#651) -------------------------------------------------


def test_4a_full_view_saves_through_the_helper_and_applies_like_agami_model():
    full = _phase_4().split("**Show the full view** (an option")[1]
    assert "--save-to-helper" in full and "--decisions" in full and "--read-only" not in full
    assert "question-cards.md#6a-a-page-that-saves-its-decisions" in full
    assert "as `/agami-model` Phase 5b does" in full
    assert "`--initial-tab pii` from the personal-data question" in full


def test_4a_bulk_sign_off_lists_the_unseen_items_first():
    s = _phase_4()
    assert "a sign-off is never recorded for a definition the person hasn't seen" in s
    assert '"bulk":true' in s


def test_6_examples_page_saves_and_its_card_says_ive_marked_them():
    full = _phase_6().split("**Show the full view** (an option")[1]
    assert "--save-to-helper --artifacts-dir" in full and "--decisions" in full
    assert "**I've marked them on the page** and **Back to the cards**" in full
    # what the page saved maps onto the old block's lines, applied by the same commands
    for pair in ("`keep` is `validate N`", "`drop` is `reject N`", "`fix` (with its `sql`) is `edit N`", "`note_all` is `note all`"):
        assert pair in full, pair
    assert "is said back and not applied" in full


def test_6_saved_examples_are_checked_by_the_parser_and_a_keep_needs_an_identity():
    full = _phase_6().split("**Show the full view** (an option")[1]
    assert "parse_examples_decisions.py --decisions-file" in full and "--expect-profile <profile>" in full
    assert "Never read it by hand" in full
    assert "**A keep is a sign-off**" in full and "is not applied; say so" in full


def test_6_with_add_my_own_drop_the_failing_ones_gives_way():
    """With #649's Add my own on the card and some failures, five options would not fit."""
    s = _phase_6()
    rule = s[s.index("**When the card also offers Add my own**"):]
    rule = rule[: rule.index("\n\n")]
    assert "**Drop the failing ones** gives way" in rule
    assert "**Keep all that ran** / **Review one by one** / **Add my own** / **Mark them on a page**" in rule


def test_approve_all_remaining_marks_its_ops_bulk_in_connect():
    assert 'Each of its approve ops carries `"bulk": true`.' in _phase_4()


# --- 1.6p: pick from a list, on a page that saves itself (#654) ---------------------------------


def _section_16p() -> str:
    return _between("### 1.6p ", "### 1.7 ")


def _tables_card() -> list[str]:
    s = _section_16()
    i = s.index("Then call AskUserQuestion, one question (header `Tables`)")
    lines = s[i:].splitlines()[2:]
    return [ln for ln in lines[: next(n for n, ln in enumerate(lines) if not ln.startswith("- **"))]]


def test_1_6_offers_pick_from_a_list_within_four_options():
    options = _tables_card()
    assert len(options) == 4
    assert [o.split("**")[1] for o in options] == [
        "Pick from a list", "Keep everything", "Drop staging and backups", "Only some schemas"]
    assert "more than 10 tables" in options[0] and "(Recommended)" in options[0]
    # one recommendation at a time: dropping the copies is recommended only on a small catalog
    assert "With 10 tables or fewer, mark it **(Recommended)**" in options[2]


def test_1_6_takes_typed_names_in_the_question_not_as_an_option():
    s = _section_16()
    card = s[s.index("(header `Tables`)"):]
    assert card.splitlines()[0].endswith('Or type table names or patterns in Other, like orders, customers or all crm_*."*:')
    assert "--keep pick:" in s and "there is no option for it" in s


def test_1_6p_serves_the_picker_with_the_saving_helper_and_writes_through_keep():
    s = _section_16p()
    for step in ("SSH_CONNECTION", "render_table_picker.py", "serve_page.py", "--decisions",
                 "run_in_background", "Monitor tool", "never a foreground sleep",
                 "**I've saved it** / **Back to the cards**", "TaskStop",
                 '--keep page:"<decisions file>"', "agami-keep.txt", '"Keeping <kept> of <N> tables."',
                 '{"status": "expired"}', "never edit the file"):
        assert step in s, step
    assert s.index("reads the helper's output first") < s.index("TaskStop")


def test_1_6p_falls_back_to_a_numbered_list_typed_in_other():
    s = _section_16p()
    fallback = s[s.index("**The numbered list**"):]
    assert "--numbered" in fallback and "--keep pick:" in fallback
    q = next(ln for ln in fallback.splitlines() if "(header `Table pick`)" in ln)
    # what to type is in the question (rule 5), and the options are the alternatives to typing
    assert "Type numbers, ranges or patterns in Other, like 1-12, 15 or all crm_* or everything except audit_*." in q
    assert "**Keep everything** / **Drop staging and backups**" in q and "Type" not in q.split(":", 1)[1].split("**")[1]
    assert "Past 200 tables" in fallback and "the first 200 tables" in fallback
    # numbers are read only up to the last one the list showed, and never on the main card
    assert '--shown <shown>' in fallback
    assert "--shown 0" in _section_16()


def test_1_6p_headers_are_new_and_fit_a_card():
    s = _section_16p()
    for h in ("Table list", "Table pick"):
        assert f"header `{h}`" in s and len(h) <= 12


def test_1_6p_back_to_the_cards_goes_to_the_numbered_list():
    """Copilot on #657: in a cloud or web session the page can be unreachable without
    SSH_CONNECTION; going back to the Tables card offered Pick from a list, another dead page."""
    s = _section_16p()
    back = s[s.index("**Back to the cards** reads the helper's output first"):]
    back = back[: back.index("\n")]
    assert back.index("TaskStop") < back.index("**The numbered list**")
    assert "`Tables` card again" not in back
