# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit and CLI coverage for the plain-language standup draft gate."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from io import BytesIO, TextIOWrapper
from pathlib import Path

import pytest

from omnimarket.nodes.node_standup_draft_check_compute.__main__ import main
from omnimarket.nodes.node_standup_draft_check_compute.handlers.handler_standup_draft_check import (
    JARGON_WORDS,
    PREAMBLE_OPENERS,
    HandlerStandupDraftCheck,
    check,
    jargon_found,
    m4_delegation_warnings,
    milestone_warnings,
    visible_text,
)
from omnimarket.nodes.node_standup_draft_check_compute.models import (
    ModelStandupDraftCheckRequest,
    ModelStandupDraftCheckResult,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE = (FIXTURES / "pass_reference.md").read_text(encoding="utf-8")


M4_LINE = "M4 delegation: 8 of 9 checks pass; one check has failed since Oct 8."


THEMES = (
    "*Reliability:* Delegation no longer stalls when the message service restarts. (about 6 changes)\n"
    "*Speed of shipping:* Approved changes reach users sooner. (about 9 changes)"
)


MILESTONE_WARNING = "STANDUP-CHECK WARN milestone transition absent from draft: C90"


M4_WARNING = "STANDUP-CHECK WARN no sentence on where M4 delegation stands"


def draft(
    headline: str = "Staging deploys work again.",
    themes: str = THEMES,
    blocked: str = "Still blocked: nothing.",
) -> str:
    """A valid message whose parts a test replaces one at a time."""
    return f"{headline}\n\n{themes}\n\n{M4_LINE}\n\n{blocked}\n"


def rule_ids(text: str) -> list[str]:
    """Return just the violated rule IDs, in order, for focused assertions."""
    return [finding.rule for finding in check(text).findings]


def cli(*args: str, text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the node CLI without writing Python bytecode."""
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "omnimarket.nodes.node_standup_draft_check_compute",
            *args,
        ],
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )


def test_reference_passes() -> None:
    """The unmodified reference passes with its exact metrics."""
    result = check(REFERENCE)
    assert result.ok
    assert not result.findings
    assert result.words <= 170
    assert (result.themes, result.links, result.refs) == (3, 1, 1)
    output = cli(text=REFERENCE)
    assert output.returncode == 0
    assert not output.stderr
    assert (
        output.stdout
        == f"STANDUP-CHECK OK words={result.words} themes=3 links=1 refs=1\n"
    )


def test_pr_list_fixture_fails() -> None:
    """The legacy PR and ticket lists fail every rule that applies to a list."""
    output = cli(text=(FIXTURES / "fail_pr_list.md").read_text(encoding="utf-8"))
    assert output.returncode == 1
    assert not output.stdout
    findings = output.stderr.splitlines()
    rules = {line.split(":", 1)[0].removeprefix("FAIL ") for line in findings[:-1]}
    assert {"first-sentence", "themes", "still-blocked", "ref-list", "refs"} <= rules
    assert all(line.startswith("FAIL ") for line in findings[:-1])
    assert findings[-1].startswith("STANDUP-CHECK FAIL words=")


def test_previous_internal_voice_fails_on_jargon_and_shape() -> None:
    """The report that prompted the format fails: no groups, internal words, ids."""
    result = check((FIXTURES / "fail_internal_voice.md").read_text("utf-8"))
    rules = [finding.rule for finding in result.findings]
    assert "themes" in rules
    assert "jargon" in rules
    assert "refs-in-body" not in rules
    message = next(f.message for f in result.findings if f.rule == "jargon")
    for word in ("controller", "ledger", "PRs", "lane", "ulimit", "criterion id 'C28'"):
        assert word in message


@pytest.mark.parametrize("count", [170, 171])
def test_word_boundary(count: int) -> None:
    """Exactly 170 words passes while 171 triggers the word rule."""
    base = check(REFERENCE).words
    text = REFERENCE.replace("\n\n", " " + "extra " * (count - base) + "\n\n", 1)
    result = check(text)
    assert result.words == count
    assert result.ok == (count == 170)
    if count == 171:
        assert result.findings[0].message.startswith("171 words, limit 170")


@pytest.mark.parametrize("count", [3, 4])
def test_link_boundary(count: int) -> None:
    """Three links pass while four trigger only the link rule."""
    forms = [
        "<https://example.test/a|context>",
        "[context](https://example.test/c)",
        "https://example.test/d",
        "<https://example.test/f|more context>",
    ]
    result = check(draft(blocked="Still blocked: " + " ".join(forms[:count])))
    assert result.links == count
    assert result.ok == (count == 3)
    assert rule_ids(draft(blocked="Still blocked: " + " ".join(forms))) == ["links"]


@pytest.mark.parametrize("count", [2, 3])
def test_reference_boundary(count: int) -> None:
    """Two references in the last paragraph pass; a third trips only the limit."""
    refs = ["OMN-9001", "alpha#102", "https://example.test/alpha/pull/103"]
    result = check(draft(blocked="Still blocked: " + " ".join(refs[:count])))
    assert result.refs == count
    assert result.ok == (count == 2)
    assert rule_ids(draft(blocked="Still blocked: " + " ".join(refs))) == ["refs"]


@pytest.mark.parametrize(
    "ref", ["OMN-9001", "alpha#101", "#101", "https://example.test/a/pull/101"]
)
def test_reference_above_the_blocked_paragraph_fails(ref: str) -> None:
    """A ticket or pull request reference belongs only where someone must act."""
    assert rule_ids(draft(headline=f"Staging deploys work again ({ref}).")) == [
        "refs-in-body"
    ]
    themed = THEMES.replace("restarts.", f"restarts ({ref}).")
    assert rule_ids(draft(themes=themed)) == ["refs-in-body"]


def test_reference_deduplication() -> None:
    """A ticket id and its Linear link, or a pull request token and its link, count once."""
    body = (
        "Still blocked: OMN-9001 <https://linear.app/example/issue/OMN-9001/retries|OMN-9001> "
        "[alpha#101](https://example.test/alpha/pull/101) alpha#101 "
        "<https://example.test/beta/issues/101|#101>"
    )
    result = check(draft(blocked=body))
    assert result.ok
    assert result.refs == 2
    assert result.links == 3
    assert check(draft(blocked="Still blocked: alpha#101 OMN-9001 OMN-9002")).refs == 3


def test_long_link_labels_and_raw_urls() -> None:
    """Long destinations never inflate visible word counts."""
    base = check(REFERENCE).words
    url = "https://example.test/" + "a" * 99
    linked = REFERENCE.replace("Users can now", f"<{url}|Users can now>", 1)
    raw = REFERENCE.replace("Users can now", url, 1)
    assert check(linked).words == base
    assert check(raw).words == base - 2


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("<https://example.test/a|two words>", 2),
        ("<https://example.test/a>", 1),
        ("<@U1|operator>", 1),
        ("<#C1|channel>", 1),
        ("[two words](url)", 2),
        ("https://example.test/a", 1),
        ("*word* _word_ ~word~ >word", 4),
        ("- • ◦ \u2013 — * _ ~ > `", 0),
    ],
)
def test_visible_words(text: str, count: int) -> None:
    """Visible conversion handles links, mentions, markup, and empty glyphs."""
    assert len(visible_text(text).split()) == count
    assert check(text).words == count


@pytest.mark.parametrize("opener", PREAMBLE_OPENERS)
def test_preamble_openers(opener: str) -> None:
    """Each preamble opener fails with a message naming that opener."""
    result = check(draft(headline=f"> *{opener.upper()}* investigated retries."))
    finding = next(f for f in result.findings if f.rule == "first-sentence")
    assert repr(opener) in finding.message
    assert "claim of change" in finding.message


@pytest.mark.parametrize(
    "sentence",
    [
        "Standup reports now share one format.",
        "Today, deploys work again.",
        "Yesterday, the queue drained for the first time.",
        "Retry failures now produce receipts!",
        "Nothing new for users to try yet; 42 changes shipped and 50 more are in review.",
    ],
)
def test_change_claims(sentence: str) -> None:
    """Concrete claims pass the first sentence rule."""
    assert check(draft(headline=sentence)).ok


@pytest.mark.parametrize(
    "heading", ["Today I", "Yesterday I", "Worked on", "My status", "Standup:"]
)
def test_preamble_headings_still_fail(heading: str) -> None:
    """First-person and status headings stay rejected."""
    assert "first-sentence" in rule_ids(draft(headline=f"{heading} deploys."))


@pytest.mark.parametrize(
    "ending",
    ["Still blocked: nothing.", "*Still blocked:* nothing.", "still blocked: none."],
)
def test_blocker_formatting(ending: str) -> None:
    """Bold and lowercase blocker prefixes pass the final paragraph rule."""
    assert check(draft(blocked=ending)).ok


def test_single_newline_paragraphs_pass() -> None:
    """A status pasted with single newlines between its paragraphs is the same message."""
    text = REFERENCE.replace("\n\n", "\n")
    assert "\n\n" not in text
    assert check(text).ok


def test_blockers_must_be_final() -> None:
    """An earlier blocker paragraph cannot satisfy the final paragraph rule."""
    text = draft(blocked="Still blocked: retries.\n\nThe operator will investigate.")
    assert rule_ids(text) == ["still-blocked"]
    single = draft(blocked="Still blocked: retries.").rstrip() + "\n\nMore prose."
    assert rule_ids(single) == ["still-blocked"]


@pytest.mark.parametrize("text", ["", "* • >", "Still blocked: none."])
def test_empty_or_blocker_first_sentence(text: str) -> None:
    """Empty openers and a blocker-only paragraph fail the change claim rule."""
    assert "first-sentence" in rule_ids(text)


@pytest.mark.parametrize(
    "marker", ["-", "*", "+", "•", "◦", "\u2013", "—", "1.", "2)", "> -"]
)
def test_reference_list_markers(marker: str) -> None:
    """Every supported list marker rejects ticket references with raw line numbers."""
    text = f"Deploys work again.\n\n  {marker} OMN-9001 — Retry coverage\n\nStill blocked: none."
    findings = check(text).findings
    assert "ref-list" in {f.rule for f in findings}
    assert next(f for f in findings if f.rule == "ref-list").message.startswith(
        "line 3 is a list"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/alpha/pull/42",
        "https://example.test/alpha/issues/42",
        "https://linear.app/example/issue/OMN-9001/retries",
    ],
)
def test_list_link_url_is_checked(url: str) -> None:
    """A list link with plain prose still fails when its destination is a reference."""
    assert "ref-list" in rule_ids(
        draft(headline=f"Deploys work.\n- <{url}|retry context>")
    )


@pytest.mark.parametrize(
    ("labels", "ok"), [(1, False), (2, True), (5, True), (6, False)]
)
def test_theme_count_bounds(labels: int, ok: bool) -> None:
    """Two to five grouped themes pass; one, or a list of six, does not."""
    themes = "\n".join(
        f"*Group {n}:* It improved. (about {n} changes)" for n in range(1, labels + 1)
    )
    assert check(draft(themes=themes)).ok == ok
    if not ok:
        assert "themes" in rule_ids(draft(themes=themes))


def test_a_flat_list_of_changes_is_not_grouping() -> None:
    """Prose and bullets with no bold label are not themes."""
    flat_list = "- Retries improved.\n- Receipts are readable."
    assert rule_ids(draft(themes=flat_list)) == ["themes"]


def test_theme_marker_and_label_forms() -> None:
    """A bullet before the label, and a colon inside or outside the bold, both count."""
    themes = "• *Reliability:* It improved. (about 3 changes)\n*Cost*: It fell. (roughly 4 small fixes)"
    result = check(draft(themes=themes))
    assert result.ok
    assert result.themes == 2


def test_blocked_and_m4_lines_are_never_themes() -> None:
    """A bolded blocker or delegation line does not add a theme."""
    text = draft(blocked="*Still blocked:* nothing.").replace(
        "M4 delegation:", "*M4 delegation:*"
    )
    result = check(text)
    assert result.themes == 2
    assert result.ok


@pytest.mark.parametrize(
    "line",
    ["*Reliability:* It improved.", "*Reliability:* It improved a lot (many changes)."],
)
def test_every_theme_needs_a_rough_count(line: str) -> None:
    """A theme with no number of changes names the line to fix."""
    findings = check(
        draft(themes=f"{line}\n*Cost:* It fell. (about 2 changes)")
    ).findings
    assert [f.rule for f in findings] == ["theme-count"]
    assert "line 3 ('Reliability')" in findings[0].message


@pytest.mark.parametrize(
    "count",
    [
        "about 12 changes",
        "roughly 9 small fixes",
        "3 updates",
        "1 change",
        "about 1,200 improvements",
    ],
)
def test_rough_count_forms(count: str) -> None:
    """The count phrases a writer naturally uses all pass."""
    assert check(
        draft(themes=f"*Alpha:* It improved. ({count})\n*Beta:* It fell. ({count})")
    ).ok


def test_theme_is_one_outcome_sentence() -> None:
    """A theme past 40 words or two sentences fails; the count is not a sentence."""
    second = "\n*Cost:* It fell. (about 2 changes)"
    three = "*Reliability:* " + "It improved. " * 3 + "(about 4 changes)"
    assert "theme-long" in rule_ids(draft(themes=three + second))
    wordy = "*Reliability:* " + "word " * 40 + "(about 4 changes)"
    assert "theme-long" in rule_ids(draft(themes=wordy + second))
    two = "*Reliability:* It improved. Services reconnect alone. (about 4 changes)"
    assert "theme-long" not in rule_ids(draft(themes=two + second))


def test_message_cannot_open_with_a_theme() -> None:
    """The first block is one plain headline sentence, not a themed line."""
    text = f"{THEMES}\n\n{M4_LINE}\n\nStill blocked: nothing.\n"
    assert "headline" in rule_ids(text)


@pytest.mark.parametrize("word", JARGON_WORDS)
def test_each_banned_word_fails_in_a_theme(word: str) -> None:
    """Every word of the list is refused, in any case, naming the word."""
    themes = THEMES.replace("Approved changes", f"The {word.upper()} thing")
    result = check(draft(themes=themes))
    message = next(f.message for f in result.findings if f.rule == "jargon")
    assert word in message


def test_banned_word_list_covers_the_objections() -> None:
    """The list names at least the words the operator and the cofounder objected to."""
    words = set(JARGON_WORDS)
    assert {
        "lane",
        "lanes",
        "controller",
        "ledger",
        "CI",
        "hooks",
        "pytest",
        "guard",
    } <= words
    assert {
        "flake",
        "crash-loop",
        "restart count",
        "pull request",
        "merge",
        "PRs",
    } <= words


@pytest.mark.parametrize(
    "plain",
    [
        "The guardian watched the emergence of flaky-free ledgers.",
        "Specifically, the pytester plugin is unrelated.",
        "A commitment was made and the train carried on.",
        "Services reconnect and recover on their own.",
    ],
)
def test_whole_words_only(plain: str) -> None:
    """Substrings of other words never match."""
    assert jargon_found(plain) == []


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Criterion C28 is red.", "criterion id"),
        ("The `run` is quick.", "code mark"),
        ("It reads standup_check.py.", "file name"),
        ("The node_dispatch_port is up.", "identifier"),
        ("It takes --result-only now.", "command flag"),
    ],
)
def test_code_and_internal_id_shapes_fail(text: str, label: str) -> None:
    """Criterion ids, code marks, file names, snake_case names and flags are refused."""
    assert any(found.startswith(label) for found in jargon_found(text))
    assert "jargon" in rule_ids(draft(headline=text))


def test_banned_words_are_caught_in_the_last_paragraph_and_the_m4_line() -> None:
    """The ban covers the whole message, not only the themes."""
    assert "jargon" in rule_ids(draft(blocked="Still blocked: the controller is down."))
    assert "jargon" in rule_ids(draft().replace("checks pass", "CI checks pass"))


def test_m4_is_not_banned() -> None:
    """The milestone name the cofounder uses stays allowed."""
    assert jargon_found("M4 delegation: 8 of 9 checks pass.") == []


def test_all_findings_in_rule_order() -> None:
    """All violations appear together in the specified rule order."""
    text = (
        "Today I investigated the ledger. "
        + "extra " * 171
        + "\n- OMN-9001 OMN-9002 OMN-9003\n"
    )
    text += " ".join(f"https://example.test/{number}" for number in range(4))
    assert rule_ids(text) == [
        "words",
        "first-sentence",
        "still-blocked",
        "themes",
        "jargon",
        "ref-list",
        "links",
        "refs-in-body",
        "refs",
    ]


def test_cli_stdin() -> None:
    """Stdin alone returns the same passing metrics."""
    output = cli(text=REFERENCE)
    result = check(REFERENCE)
    assert output.returncode == 0
    assert not output.stderr
    assert (
        output.stdout
        == f"STANDUP-CHECK OK words={result.words} themes=3 links=1 refs=1\n"
    )


def test_cli_positional_argument() -> None:
    """A positional path is rejected with the usage error exit code."""
    output = cli("missing.md", text=REFERENCE)
    assert output.returncode == 2
    assert "unrecognized arguments: missing.md" in output.stderr
    assert not output.stdout


def test_cli_invalid_utf8() -> None:
    """Invalid UTF-8 is reported as unreadable input without a traceback."""
    output = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "omnimarket.nodes.node_standup_draft_check_compute",
        ],
        input=b"\xff",
        capture_output=True,
        check=False,
    )
    assert output.returncode == 2
    assert b"cannot read input" in output.stderr
    assert b"Traceback" not in output.stderr


@pytest.mark.parametrize("args", [("--facts-json",), ("--unknown",)])
def test_cli_usage_errors(args: tuple[str, ...]) -> None:
    """Missing option values and invalid flags use argparse's usage error exit code."""
    assert cli(*args).returncode == 2


@pytest.mark.parametrize(
    "fixture",
    [
        "pass_reference.md",
        "pass_linked_three.md",
        "fail_pr_list.md",
        "fail_internal_voice.md",
    ],
)
def test_cli_json(fixture: str) -> None:
    """JSON includes all metrics and findings and agrees with the process exit code."""
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    expected = check(text)
    output = cli("--json", text=text)
    data = json.loads(output.stdout)
    data.pop("warnings", None)
    assert data == asdict(expected)
    assert len(output.stdout.splitlines()) == 1
    assert output.returncode == (0 if expected.ok else 1)
    assert not output.stderr


def test_linked_example_passes_with_three_links() -> None:
    """The linked example carries 3 links and 1 reference and passes."""
    result = check((FIXTURES / "pass_linked_three.md").read_text("utf-8"))
    assert (result.links, result.refs) == (3, 1), result.findings
    assert result.ok, result.findings


def facts_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    facts: object = None,
    *,
    text: str | None = None,
    as_json: bool = False,
    with_facts: bool = True,
    raw_facts: str | None = None,
) -> tuple[int, str, str]:
    """Supply facts in memory and run the CLI without creating input files."""
    if facts is None:
        facts = {
            "milestone": {
                "transitions": [{"criterion": "C90", "probe": "recovery-probe"}]
            }
        }

    monkeypatch.setattr(
        sys,
        "stdin",
        TextIOWrapper(BytesIO((text if text is not None else draft()).encode("utf-8"))),
    )
    facts_json = json.dumps(facts) if raw_facts is None else raw_facts
    args = (["--facts-json", facts_json] if with_facts else []) + (
        ["--json"] if as_json else []
    )
    code = main(args)
    output = capsys.readouterr()
    return code, output.out, output.err


def test_facts_transition_omitted_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An absent transition warns without changing a passing exit code."""
    code, output, error = facts_cli(monkeypatch, capsys)
    assert code == 0
    assert not error
    lines = output.splitlines()
    assert lines[0] == MILESTONE_WARNING
    assert lines[1].startswith("STANDUP-CHECK OK")
    assert len(lines) == 2


def test_facts_transition_named_by_id_or_probe_is_silent() -> None:
    """The criterion token, or a case-insensitive probe substring, covers the transition."""
    facts = {
        "milestone": {"transitions": [{"criterion": "C90", "probe": "recovery-probe"}]}
    }
    assert milestone_warnings("(C90) recovered.", facts) == []
    assert milestone_warnings("RECOVERY-PROBE recovered.", facts) == []
    assert milestone_warnings("C900 improved.", facts) == [MILESTONE_WARNING]
    assert milestone_warnings("c90 improved.", facts) == [MILESTONE_WARNING]


def test_facts_transition_named_by_its_description_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plain draft carries no id: half of the board description's longer words cover it."""
    description = "A developer delegates a reasoning task with a bring-your-own key"
    facts = {
        "milestone": {
            "transitions": [
                {
                    "criterion": "C90",
                    "probe": "recovery-probe",
                    "description": description,
                }
            ]
        }
    }
    covered = draft(
        headline="A developer can delegate a reasoning task with their own key."
    )
    code, output, error = facts_cli(monkeypatch, capsys, facts, text=covered)
    assert code == 0
    assert not error
    assert output.startswith("STANDUP-CHECK OK")
    code, output, _ = facts_cli(
        monkeypatch, capsys, facts, text=draft(headline="Deploys work.")
    )
    assert output.startswith(MILESTONE_WARNING)


@pytest.mark.parametrize("as_json", [False, True])
def test_no_facts_option_keeps_the_plain_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    """Without facts and without a warning the output has no warnings at all."""
    code, output, error = facts_cli(
        monkeypatch, capsys, as_json=as_json, with_facts=False, text=REFERENCE
    )
    assert code == 0
    assert not error
    if as_json:
        assert json.loads(output) == asdict(check(REFERENCE))
    else:
        assert output.startswith("STANDUP-CHECK OK")
        assert "WARN" not in output


@pytest.mark.parametrize("raw_facts", ["", "not JSON", "{"])
def test_facts_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw_facts: str,
) -> None:
    """Invalid JSON facts exit two with a single input error."""
    code, output, error = facts_cli(monkeypatch, capsys, raw_facts=raw_facts)
    assert code == 2
    assert not output
    assert error.startswith("STANDUP-CHECK ERROR: cannot read facts: ")
    assert "Traceback" not in error


def test_facts_json_warnings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Facts add a warnings list to the otherwise unchanged JSON result."""
    code, output, error = facts_cli(monkeypatch, capsys, as_json=True)
    data = json.loads(output)
    assert code == 0
    assert not error
    warnings = data.pop("warnings")
    assert warnings == [MILESTONE_WARNING]
    assert data == asdict(check(draft()))


def test_facts_failure_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Warnings precede the failing result on stderr and leave exit one intact."""
    code, output, error = facts_cli(
        monkeypatch, capsys, text=f"Today I investigated. {M4_LINE}"
    )
    assert code == 1
    assert not output
    lines = error.splitlines()
    assert lines[-2] == MILESTONE_WARNING
    assert lines[-1].startswith("STANDUP-CHECK FAIL")


@pytest.mark.parametrize(
    "facts",
    [
        {},
        [],
        {"milestone": []},
        {"milestone": {"transitions": "invalid"}},
        {"milestone": {"transitions": [None, {}, {"criterion": [], "probe": {}}]}},
    ],
)
def test_facts_wrong_shapes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], facts: object
) -> None:
    """Missing or malformed transition shapes behave like an empty list."""
    code, output, error = facts_cli(monkeypatch, capsys, facts=facts, as_json=True)
    assert code == 0
    assert not error
    assert json.loads(output)["warnings"] == []


def test_facts_empty_criterion(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unknown criteria fall back to the probe name once per transition."""
    facts = {
        "milestone": {
            "transitions": [
                {"criterion": "", "probe": "recovery-probe"},
                {"criterion": "C91", "probe": "other-probe"},
            ]
        }
    }
    code, output, error = facts_cli(monkeypatch, capsys, facts=facts, as_json=True)
    assert code == 0
    assert not error
    assert json.loads(output)["warnings"] == [
        "STANDUP-CHECK WARN milestone transition absent from draft: recovery-probe",
        "STANDUP-CHECK WARN milestone transition absent from draft: C91",
    ]


def test_standup_delegation_section_missing_sentence_warns_without_changing_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A draft with no M4 delegation sentence warns; the exit code stays zero."""
    text = draft().replace(M4_LINE + "\n\n", "")
    assert m4_delegation_warnings(text) == [M4_WARNING]
    code, output, error = facts_cli(monkeypatch, capsys, with_facts=False, text=text)
    assert code == 0
    assert not error
    assert output.splitlines()[0] == M4_WARNING


@pytest.mark.parametrize(
    "sentence",
    [
        "M4 delegation: 8 of 9 checks pass, the plugin check has failed since Oct 8, 12 of 20 build tasks are done.",
        "M4 delegation: unread tonight, because the progress board was not published.",
        "m4 delegation is 8 of 9 checks passing.",
    ],
)
def test_standup_delegation_section_sentence_present_is_silent(sentence: str) -> None:
    """The sentence is found case-insensitively anywhere in the draft."""
    assert m4_delegation_warnings(draft().replace(M4_LINE, sentence)) == []


def test_standup_delegation_section_warning_in_json() -> None:
    """The JSON result lists the warning when the sentence is missing."""
    line = next(p for p in REFERENCE.split("\n\n") if p.startswith("M4 delegation"))
    text = REFERENCE.replace(line + "\n\n", "")
    assert "M4 delegation" not in text
    assert json.loads(cli("--json", text=text).stdout)["warnings"] == [M4_WARNING]


def test_standup_delegation_section_reference_carries_the_sentence_inside_the_limit() -> (
    None
):
    """The reference draft carries the plain-language sentence and stays inside the limit."""
    assert check(REFERENCE).words <= 170
    assert m4_delegation_warnings(REFERENCE) == []
    assert "build tasks are done" in REFERENCE
    assert "carrier" not in REFERENCE


def test_handler_reference_result() -> None:
    """The typed handler returns the reference draft's exact metrics and verdict."""
    result = HandlerStandupDraftCheck().handle(
        ModelStandupDraftCheckRequest(text=REFERENCE)
    )
    assert isinstance(result, ModelStandupDraftCheckResult)
    assert result.ok is True
    assert (result.words, result.themes, result.links, result.refs) == (139, 3, 1, 1)
    assert result.findings == ()
    assert result.warnings == ()
