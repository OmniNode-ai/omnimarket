# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A row with ``terminal_ok`` NULL is named as that, not as a behaviour miss (OMN-24831).

Scheduled run 43826372809 (2026-11-16 07:48Z, job 133161076598, step "Run
Layer-2 golden-task runner (scoreboard)") published all ten corpus cases and
read back a ``delegation_events`` row for every one of them. Every row read
``tokens=0/0`` and none had a terminal: eight cases failed hard (``HARD
FAILURES: I1, I3, I5, I6, I7, I8, I9, I10``) and I2 and I4 failed as tracked
xfails. The model column was populated on eight of the ten rows
(``cohere/north-mini-code:free`` on I1 to I4 and I7 to I9, ``Qwen3.8-27B`` on
I5) and read ``none`` on I6 and I10. The pytest leg of the same job printed,
for example:

* I9: ``terminal: expected 'completed', got 'unknown'`` plus
  ``tokens: expected positive, got input=0 output=0``;
* I10: ``terminal: expected 'failed', got 'unknown'``.

``unknown`` is what ``row_terminal`` returns for a row whose ``terminal_ok`` is
NULL in the row read (OMN-13543). The old text spread that one fact over a
wrong-terminal line and a token line, which sent the reader to the delegation
ladder instead of to the row. ``evaluate_row`` now says it once. It cannot know
whether a settle window elapsed, so the text states only that ``terminal_ok``
is NULL in the row read.

The assertion is not loosened: a case that expects a terminal still FAILS on
such a row. Only the wording changes, and the gate is ``terminal_ok`` being
NULL, not the reduced terminal being ``unknown`` alone.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.delegation_golden import runner as runner_module
from tests.delegation_golden.corpus_loader import ModelCorpusCase, load_corpus

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The eight cases the nightly scored as hard failures, with the model column
# each row carried in job 133161076598 (None prints as ``none`` there).
_NIGHTLY_HARD_FAILURES: dict[str, str | None] = {
    "I1": "cohere/north-mini-code:free",
    "I3": "cohere/north-mini-code:free",
    "I5": "Qwen3.8-27B",
    "I6": None,
    "I7": "cohere/north-mini-code:free",
    "I8": "cohere/north-mini-code:free",
    "I9": "cohere/north-mini-code:free",
    "I10": None,
}


def _unterminalised_row(
    model_name: str | None = "cohere/north-mini-code:free",
) -> dict[str, Any]:
    """The shape run 43826372809 read back: a row, no outer outcome, zero telemetry."""
    return {
        "correlation_id": "6ba0cce8-7acd-4c5b-b17f-3d7e8e0d4ecc",
        "timestamp": "2026-11-16T07:57:05.000000+00:00",
        "delegated_to": model_name,
        "model_name": model_name,
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "quality_gate_passed": None,
        "quality_gate_detail": "",
        "terminal_ok": None,
        "terminal_failure_cause": None,
    }


def _completed_row() -> dict[str, Any]:
    return {
        "correlation_id": "2b70965b-aa34-41c9-8005-c0f07d8e28a2",
        "timestamp": "2026-09-24T07:41:02.000000+00:00",
        "delegated_to": "Qwen3.8-27B",
        "model_name": "Qwen3.8-27B",
        "tokens_input": 40,
        "tokens_output": 300,
        "cost_usd": 0.0,
        "quality_gate_passed": True,
        "quality_gate_detail": "",
        "terminal_ok": True,
        "terminal_failure_cause": None,
    }


@pytest.mark.unit
class TestAnUnterminalisedRowIsNamedAsSuch:
    @pytest.mark.parametrize("case_id", sorted(_NIGHTLY_HARD_FAILURES))
    def test_one_failure_names_the_null_terminal_ok(self, case_id: str) -> None:
        """RED at the base: one to four lines (I10 reads one, I1 reads four), none naming the cause."""
        case = load_corpus().by_id(case_id)
        row = _unterminalised_row(_NIGHTLY_HARD_FAILURES[case_id])
        failures = runner_module.evaluate_row(case, row)
        assert len(failures) == 1, failures
        assert "no terminal outcome" in failures[0], failures
        assert "terminal_ok is NULL in the row read" in failures[0], failures
        assert "settle window" not in failures[0], failures

    def test_it_does_not_report_telemetry_misses_it_cannot_judge(self) -> None:
        """A row with no outer outcome has no telemetry to hold to an expectation."""
        case = load_corpus().by_id("I9")
        failures = runner_module.evaluate_row(case, _unterminalised_row())
        assert not any("tokens: expected positive" in f for f in failures), failures
        assert not any("got 'unknown'" in f for f in failures), failures

    @pytest.mark.parametrize("case_id", sorted(_NIGHTLY_HARD_FAILURES))
    def test_the_case_still_fails(self, case_id: str) -> None:
        """Not loosened: an expected terminal is still unmet on a NULL terminal_ok."""
        case = load_corpus().by_id(case_id)
        row = _unterminalised_row(_NIGHTLY_HARD_FAILURES[case_id])
        assert runner_module.evaluate_row(case, row), case_id

    def test_every_nightly_hard_failure_declares_a_terminal(self) -> None:
        """The branch only short-circuits a case that declares one; all eight do."""
        corpus = load_corpus()
        undeclared = [
            case_id
            for case_id in _NIGHTLY_HARD_FAILURES
            if corpus.by_id(case_id).expected.terminal is None
        ]
        assert undeclared == []


@pytest.mark.unit
class TestTheGateIsTerminalOkNull:
    def test_unknown_terminal_with_terminal_ok_set_is_not_named_as_null(self) -> None:
        """RED if gated on ``terminal == 'unknown'`` alone.

        An explicit ``status`` of 'unknown' reduces to 'unknown' while the row
        carries ``terminal_ok=True``. That row HAS an outer outcome, so the text
        must not claim ``terminal_ok`` is NULL; it keeps the old terminal line.
        """
        case = load_corpus().by_id("I9")
        row = _completed_row()
        row["status"] = "unknown"
        assert runner_module.row_terminal(row) == "unknown"
        failures = runner_module.evaluate_row(case, row)
        assert not any("no terminal outcome" in f for f in failures), failures
        assert not any("terminal_ok is NULL" in f for f in failures), failures
        assert any(
            "terminal: expected 'completed', got 'unknown'" in f for f in failures
        ), failures

    def test_a_case_without_a_declared_terminal_keeps_the_old_path(self) -> None:
        """``expected.terminal`` None on a NULL ``terminal_ok`` row is not named.

        U1 declares no terminal, so the ``exp.terminal is not None`` guard leaves
        it out of the new branch, and it asserts nothing else: its own list is
        empty. An empty list cannot tell the guard from an early return placed
        before it, so a copy of U1 is given one expectation that only the old
        path can judge (``quality_gate: passes``). That copy must reach the old
        quality-gate line, and only it: an early return before the new branch
        yields an empty list here, and a missing guard yields ``no terminal
        outcome`` (not asserted here: that is the I9 copy below).
        """
        case = load_corpus().by_id("U1")
        assert case.expected.terminal is None
        row = _unterminalised_row()
        assert runner_module.evaluate_row(case, row) == []
        judged = _with_expected(case, quality_gate="passes")
        assert judged.expected.terminal is None
        assert runner_module.evaluate_row(judged, row) == [
            "quality_gate: expected passes, got quality_gate_passed=None"
        ]

    def test_an_undeclared_terminal_is_judged_by_the_old_lines_exactly(self) -> None:
        """I9 with its terminal undeclared reaches the old token line, and only it.

        The row is unterminalised (terminal unknown, ``terminal_ok`` NULL), so
        only the ``exp.terminal is not None`` guard keeps it out of the new
        branch. Past that guard the old path yields exactly the token line: no
        terminal line (nothing declared), no ``no terminal outcome``. A guard
        that returned early, or dropped the ``exp.terminal`` test, changes this
        list.
        """
        base = load_corpus().by_id("I9")
        case = _declaring_terminal(base, None)
        assert runner_module.evaluate_row(case, _unterminalised_row()) == [
            "tokens: expected positive, got input=0 output=0"
        ]


def _with_expected(case: ModelCorpusCase, **updates: Any) -> ModelCorpusCase:
    """A copy of ``case`` whose ``expected`` block has ``updates`` applied, unvalidated."""
    expected = case.expected.model_copy(update=updates)
    return case.model_copy(update={"expected": expected})


def _declaring_terminal(case: ModelCorpusCase, terminal: str | None) -> ModelCorpusCase:
    """A copy of ``case`` whose expected terminal is ``terminal``, unvalidated."""
    return _with_expected(case, terminal=terminal)


@pytest.mark.unit
class TestACaseDeclaringUnknownIsNotShortCircuited:
    """R is ``exp.terminal != terminal``: a declared ``unknown`` is not unmet."""

    def test_the_corpus_declares_no_unknown_terminal(self) -> None:
        """The typed corpus only allows completed and failed, and none says unknown."""
        declared = {
            case.id: case.expected.terminal
            for case in load_corpus().cases
            if case.expected.terminal is not None
        }
        assert declared, "no case declares a terminal"
        assert set(declared.values()) <= {"completed", "failed"}, declared

    def test_a_declared_unknown_on_a_null_row_has_no_terminal_failure(self) -> None:
        """At the base a case declaring ``unknown`` had no terminal line on this row.

        RED without the ``exp.terminal != terminal`` leg: the branch fires and
        names ``no terminal outcome`` for a terminal the row did show.
        """
        case = _declaring_terminal(load_corpus().by_id("I9"), "unknown")
        failures = runner_module.evaluate_row(case, _unterminalised_row())
        assert not any("no terminal outcome" in f for f in failures), failures
        assert not any("terminal: expected" in f for f in failures), failures
        assert failures == ["tokens: expected positive, got input=0 output=0"]


@pytest.mark.unit
class TestXfailTrackingIsNotKeyedOnTheFailureText:
    """I2 and I4 stay tracked whatever line their failure carries."""

    @pytest.mark.parametrize("case_id", ["I2", "I4"])
    def test_the_xfail_ticket_does_not_depend_on_the_failure_wording(
        self, case_id: str
    ) -> None:
        case = load_corpus().by_id(case_id)
        assert case.xfail is not None
        null_row = runner_module._result_from_row(case, "c-null", _unterminalised_row())
        wrong = runner_module._result_from_row(case, "c-wrong", _completed_row())
        # Two different failure texts, both non-empty, one ticket for both.
        assert null_row.failures
        assert wrong.failures
        assert null_row.failures != wrong.failures
        for result in (null_row, wrong):
            assert result.passed is False
            assert result.xfail_ticket == case.xfail.ticket

    @pytest.mark.parametrize("case_id", ["I2", "I4"])
    def test_a_tracked_case_is_not_a_hard_failure_on_either_wording(
        self, case_id: str
    ) -> None:
        case = load_corpus().by_id(case_id)
        results = [
            runner_module._result_from_row(case, "c-null", _unterminalised_row()),
            runner_module._result_from_row(case, "c-wrong", _completed_row()),
        ]
        scoreboard = runner_module.Scoreboard(
            lane=runner_module._LANE,
            corpus_version="1.0.0",
            started_at="t0",
            finished_at="t1",
            results=results,
        )
        assert scoreboard.hard_failures == []


@pytest.mark.unit
class TestATerminalisedRowIsStillJudged:
    def test_a_completed_row_with_telemetry_passes(self) -> None:
        """Positive control: a completed row is not swallowed by the new branch."""
        case = load_corpus().by_id("I9")
        assert runner_module.evaluate_row(case, _completed_row()) == []

    def test_a_completed_row_with_zero_tokens_yields_the_tokens_line(self) -> None:
        """A row with ``terminal_ok`` True and zero tokens is judged on its tokens."""
        case = load_corpus().by_id("I9")
        row = _completed_row()
        row["tokens_input"] = 0
        row["tokens_output"] = 0
        failures = runner_module.evaluate_row(case, row)
        assert any(
            "tokens: expected positive, got input=0 output=0" in f for f in failures
        ), failures
        assert not any("no terminal outcome" in f for f in failures), failures

    def test_a_wrong_terminal_is_still_a_behavioural_miss(self) -> None:
        """Positive control: a row that DID terminalise wrongly keeps its old text."""
        case = load_corpus().by_id("I10")
        failures = runner_module.evaluate_row(case, _completed_row())
        assert any("terminal: expected 'failed'" in f for f in failures), failures
        assert not any("no terminal outcome" in f for f in failures), failures


@pytest.mark.unit
class TestATimeoutRowReturnsEarlyAndOnlyATimeoutRow:
    """The early return of the budget-timeout branch, and I10 on each row shape.

    The timeout branch returns before the unterminalised-row branch. I10 (the
    corpus's deterministic must-fail case, terminal ``failed``) is green on a row
    that terminalised ``failed``, red with TWO lines on a budget-timeout row
    (the early return); on an unterminalised row it is red with ONE line, which
    ``test_one_failure_names_the_null_terminal_ok[I10]`` pins.
    """

    @staticmethod
    def _timeout_row() -> dict[str, Any]:
        row = _unterminalised_row()
        row["terminal_ok"] = False
        row["quality_gate_passed"] = False
        row["quality_gate_detail"] = (
            "delegation exceeded the handler execution budget of 240s and was cancelled"
        )
        return row

    @staticmethod
    def _failed_row() -> dict[str, Any]:
        row = _completed_row()
        row["terminal_ok"] = False
        row["quality_gate_passed"] = False
        return row

    def test_i10_is_green_on_a_row_that_terminalised_failed(self) -> None:
        case = load_corpus().by_id("I10")
        assert runner_module.row_terminal(self._failed_row()) == "failed"
        assert runner_module.evaluate_row(case, self._failed_row()) == []

    def test_a_timeout_row_returns_before_the_unterminalised_branch(self) -> None:
        case = load_corpus().by_id("I10")
        row = self._timeout_row()
        assert row["terminal_ok"] is False
        assert runner_module.row_terminal(row) == "timeout"
        failures = runner_module.evaluate_row(case, row)
        assert len(failures) == 2, failures
        assert failures[0].startswith("delegation timeout: "), failures
        assert failures[1] == "terminal: expected 'failed', got 'timeout'", failures
        assert not any("no terminal outcome" in f for f in failures), failures


# The lines the old runner printed and the Layer-2 consumers could have keyed
# on, plus the two this change introduces, as patterns: a consumer that splits
# the line, changes its case or adds a space still matches.
_FAILURE_LINE_PATTERNS = (
    re.compile(r"terminal:\s*expected", re.IGNORECASE),
    re.compile(r"tokens:\s*expected\s+positive", re.IGNORECASE),
    re.compile(r"got\s+['\"]unknown['\"]", re.IGNORECASE),
    re.compile(r"no\s+terminal\s+outcome", re.IGNORECASE),
    re.compile(r"terminal_ok\s+is\s+NULL", re.IGNORECASE),
)

# Consumers read by path. ``test_layer2_live_runner.py`` sits beside the runner
# and builds scoreboards from its output; the OMN-18349 placement and budget
# tests read the nightly's result; the health compute handler reads delegation
# events. Every workflow is scanned too, so a new one cannot key on the text
# unseen. The omnibase_infra health check reads a workflow verdict, not failure
# lines, and is not in this repository.
_CONSUMER_FILES = (
    "tests/delegation_golden/test_layer2_live_runner.py",
    "tests/test_delegation_nightly_placement.py",
    "tests/test_delegation_nightly_budget.py",
    "src/omnimarket/nodes/node_delegation_health_compute/handlers/handler_delegation_health.py",
)


def _matched_failure_lines(text: str) -> list[str]:
    return [p.pattern for p in _FAILURE_LINE_PATTERNS if p.search(text)]


def _consumer_paths(root: Path) -> list[str]:
    workflows = sorted(
        path.relative_to(root).as_posix()
        for pattern in ("*.yml", "*.yaml")
        for path in (root / ".github" / "workflows").glob(pattern)
    )
    return [*_CONSUMER_FILES, *workflows]


def _scan_consumers(root: Path) -> dict[str, list[str]]:
    """Map each consumer under ``root`` that matches a failure line to its matches."""
    found: dict[str, list[str]] = {}
    for relative in _consumer_paths(root):
        matched = _matched_failure_lines((root / relative).read_text(encoding="utf-8"))
        if matched:
            found[relative] = matched
    return found


def _consumer_tree(root: Path, planted: dict[str, str]) -> Path:
    """A tree holding every consumer file (empty) plus the ``planted`` contents."""
    for relative in _CONSUMER_FILES:
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text("", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    for relative, text in planted.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")
    return root


@pytest.mark.unit
class TestNoConsumerClassifiesFailuresByText:
    """The consumers of the failure text are read, and none matches on it."""

    def test_no_consumer_matches_the_failure_text(self) -> None:
        assert _scan_consumers(_REPO_ROOT) == {}

    def test_the_scan_reads_the_layer2_test_file_and_every_workflow(self) -> None:
        paths = _consumer_paths(_REPO_ROOT)
        assert "tests/delegation_golden/test_layer2_live_runner.py" in paths
        on_disk = sorted(
            path.relative_to(_REPO_ROOT).as_posix()
            for pattern in ("*.yml", "*.yaml")
            for path in (_REPO_ROOT / ".github" / "workflows").glob(pattern)
        )
        assert on_disk, "no workflow found: the glob reads nothing"
        assert set(on_disk) <= set(paths)
        for relative in paths:
            assert (_REPO_ROOT / relative).is_file(), relative

    @pytest.mark.parametrize(
        ("relative", "line"),
        [
            pytest.param(
                "tests/delegation_golden/test_layer2_live_runner.py",
                'failures=["terminal: expected completed"]',
                id="layer2-exact-line",
            ),
            pytest.param(
                "tests/delegation_golden/test_layer2_live_runner.py",
                'failures=["TERMINAL:   expected completed"]',
                id="layer2-case-and-spacing",
            ),
            pytest.param(
                ".github/workflows/some-new-workflow.yml",
                "run: grep -q 'tokens: expected positive' nightly.log",
                id="workflow-tokens-line",
            ),
            pytest.param(
                ".github/workflows/some-new-workflow.yml",
                "run: grep -q \"got 'unknown'\" nightly.log",
                id="workflow-got-unknown",
            ),
            pytest.param(
                "src/omnimarket/nodes/node_delegation_health_compute/handlers/handler_delegation_health.py",
                'if "no terminal outcome" in line: retry()',
                id="handler-no-terminal-outcome",
            ),
        ],
    )
    def test_the_scan_flags_a_consumer_that_matches_the_old_line(
        self, tmp_path: Path, relative: str, line: str
    ) -> None:
        """Positive control, read from a real file: the exact match, not an ``in``."""
        root = _consumer_tree(tmp_path, {relative: line + "\n"})
        found = _scan_consumers(root)
        assert list(found) == [relative], found
        assert len(found[relative]) == 1, found

    def test_the_scan_is_quiet_on_a_tree_without_a_match(self, tmp_path: Path) -> None:
        root = _consumer_tree(
            tmp_path, {".github/workflows/ok.yml": "run: echo nothing here\n"}
        )
        assert _scan_consumers(root) == {}


@pytest.mark.unit
class TestTheVerdictIsTheFailureListNotItsText:
    """A case passes iff ``evaluate_row`` returns no failure; the wording is free."""

    def test_the_result_carries_the_list_and_a_verdict_derived_from_it(self) -> None:
        case = load_corpus().by_id("I9")
        for row, expect_pass in (
            (_unterminalised_row(), False),
            (_completed_row(), True),
        ):
            result = runner_module._result_from_row(case, "c-1", row)
            assert result.failures == runner_module.evaluate_row(case, row)
            assert result.passed is expect_pass
            assert result.passed is (result.failures == [])

    @pytest.mark.parametrize(
        ("failures", "expect_pass"),
        [([], True), (["anything at all"], False), (["no terminal outcome"], False)],
    )
    def test_the_verdict_follows_the_list_whatever_it_says(
        self,
        monkeypatch: pytest.MonkeyPatch,
        failures: list[str],
        expect_pass: bool,
    ) -> None:
        """Drive ``_result_from_row`` with a stub list: only emptiness decides."""
        case = load_corpus().by_id("I9")
        monkeypatch.setattr(runner_module, "evaluate_row", lambda _c, _r: failures)
        result = runner_module._result_from_row(case, "c-2", _completed_row())
        assert result.failures == failures
        assert result.passed is expect_pass


# --- Contract checks: explicit node ids, a pinned PR head, no skip vectors ----

_CONTRACTS = (
    "contracts/OMN-24831.yaml",
    "contracts/OMN-24851.yaml",
    "contracts/OMN-24902.yaml",
    "contracts/OMN-24925.yaml",
    "contracts/OMN-24986.yaml",
)

_K_SELECTOR = re.compile(r"pytest\b[^\n]*\s-k\s")
_NODE_ID = re.compile(r"tests/[\w/.-]+\.py::[^\s'\"]+")


def _check_values(contract: str) -> list[str]:
    """Every check command and evidence-requirement command of one contract."""
    data = yaml.safe_load((_REPO_ROOT / contract).read_text(encoding="utf-8"))
    values = [str(req.get("command", "")) for req in data["evidence_requirements"]]
    for entry in data["dod_evidence"]:
        values.extend(str(check["check_value"]) for check in entry["checks"])
    return [value for value in values if value]


def _lab_checks(contract: str) -> list[str]:
    """The checks that match the Lab comment's line for the pinned head."""
    return [
        v
        for v in _check_values(contract)
        if "Lab: head=$h" in v or "_check_live_lab_evidence" in v
    ]


@pytest.fixture(scope="module")
def collected_ids() -> set[str]:
    """Every node id of the delegation golden suite, from pytest's own collector."""
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/delegation_golden",
            "--collect-only",
            "-q",
            "--no-cov",
            "-n",
            "0",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "-m",
            "not e2e",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line for line in out.splitlines() if "::" in line}


@pytest.mark.unit
class TestContractChecksNameExplicitNodeIds:
    """A check that names tests names each one, so a missing id exits 4 (usage)."""

    def test_the_k_selector_pattern_matches_an_expression_and_not_a_node_id(
        self,
    ) -> None:
        """Positive control for the pattern the scan below relies on."""
        assert _K_SELECTOR.search('uv run pytest t.py -q -k "A or B"')
        assert _K_SELECTOR.search("uv run pytest t.py -k A")
        assert not _K_SELECTOR.search("uv run pytest t.py::TestA::test_a[I1] -q")

    @pytest.mark.parametrize("contract", _CONTRACTS)
    def test_no_check_selects_tests_with_a_k_expression(self, contract: str) -> None:
        offenders = [v for v in _check_values(contract) if _K_SELECTOR.search(v)]
        assert offenders == []

    @pytest.mark.parametrize("contract", _CONTRACTS)
    def test_every_named_node_id_exists(
        self, contract: str, collected_ids: set[str]
    ) -> None:
        named = {nid for v in _check_values(contract) for nid in _NODE_ID.findall(v)}
        assert named - collected_ids == set()

    def test_the_contracts_name_node_ids_at_all(self) -> None:
        """Control: the scan above is not vacuous."""
        named = {
            nid
            for contract in _CONTRACTS
            for v in _check_values(contract)
            for nid in _NODE_ID.findall(v)
        }
        assert len(named) >= 20, sorted(named)


@pytest.mark.unit
class TestLabChecksPinTheHeadAndTheCommand:
    @pytest.mark.parametrize("contract", _CONTRACTS)
    def test_no_check_takes_the_head_from_a_local_checkout(self, contract: str) -> None:
        offenders = [v for v in _check_values(contract) if "rev-parse HEAD" in v]
        assert offenders == []

    @pytest.mark.parametrize("contract", _CONTRACTS)
    def test_a_lab_comment_check_matches_head_command_exit_and_contexts(
        self, contract: str
    ) -> None:
        checks = _lab_checks(contract)
        for value in checks:
            for needle in ("_check_live_lab_evidence",):
                assert needle in value, (needle, value)

    def test_the_lab_comment_checks_exist(self) -> None:
        """Control: at least the three contracts that read the comment are scanned."""
        assert sum(bool(_lab_checks(c)) for c in _CONTRACTS) >= 3


_SK_WORD = "ski" + "p"


_BASE_PIN = "6efbe3ee5027f6efb67d0b81a1421f0dda32b4e5"


@pytest.mark.unit
class TestContractTextAnswersTheReview:
    """What the contracts say about the base, the review heads and the diff checks."""

    def test_the_fails_on_base_checks_pin_the_base_and_require_expected_ids(
        self,
    ) -> None:
        pinned = [
            (contract, value)
            for contract in _CONTRACTS
            for value in _check_values(contract)
            if 'git show "$b:' in value
        ]
        assert len(pinned) >= 3, [c for c, _ in pinned]
        for contract, value in pinned:
            assert f"b={_BASE_PIN}" in value, contract
            assert "I(1|10|3|5|6|7|8|9)" in value, contract
            assert "(^| )10 failed" in value, contract
            assert (
                "TestAbsentTerminalOk::test_an_absent_key_is_reported_as_absent"
                in value
            ), contract
            assert "origin/dev" not in value, contract

    def test_the_nightly_ticket_contract_keeps_its_ac2_unmet(self) -> None:
        """No hosted check stands for AC2: its item is local, unbound and fails until the fix lands."""
        data = yaml.safe_load(
            (_REPO_ROOT / "contracts/OMN-24831.yaml").read_text(encoding="utf-8")
        )
        item = next(
            e
            for e in data["dod_evidence"]
            if e["id"] == "dod-omn24831-ac2-fix-at-the-source"
        )
        assert item.get("execution_scope") == "local_done_gate"
        assert "binds_ac" not in item
        (check,) = item["checks"]
        assert "--find OMN-24831" in check["check_value"]
        assert "state=MERGED" in check["check_value"]
        bound = {ac for e in data["dod_evidence"] for ac in e.get("binds_ac", [])}
        assert "AC2" not in bound
        assert "UNMET" in data["summary"]

    def test_the_second_round_contract_states_its_heads_and_its_comment(self) -> None:
        text = (_REPO_ROOT / "contracts/OMN-24902.yaml").read_text(encoding="utf-8")
        assert "carried no finding list" not in text
        assert "da6946c35ce0" in text
        assert "6922291635" in text

    def test_the_diff_checks_read_the_merge_base_and_match_every_skip_vector(
        self,
    ) -> None:
        diff_checks = [
            (contract, value)
            for contract in _CONTRACTS
            for value in _check_values(contract)
            if "git merge-base" in value and "git diff" in value
        ]
        assert len(diff_checks) >= 2, [c for c, _ in diff_checks]
        for contract, value in diff_checks:
            assert "origin/HEAD" in value, contract
            assert "import" + "or" + _SK_WORD in value, contract
            assert "mark\\.(" + _SK_WORD in value, contract


# The skip vectors, spelled from fragments so that this file does not itself
# carry a literal the diff checks of the contracts look for.
_SK = "ski" + "p"
_IMPORT_OR_SKIP = re.compile("import" + "or" + _SK)
_UNCONDITIONAL_SKIP = re.compile(rf"\bmark\.{_SK}\b(?!if)|\bpytest\.{_SK}\s*\(")


def _skip_vectors(text: str) -> list[str]:
    """Read executable syntax, including imported pytest aliases, never prose."""
    tree = ast.parse(text)
    aliases = {"pytest": "pytest", "mark": "pytest.mark"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    aliases[alias.asname or alias.name] = "pytest"
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            for alias in node.names:
                aliases[alias.asname or alias.name] = "pytest." + alias.name

    def dotted(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return dotted(node.value) + "." + node.attr
        return ""

    forbidden = {"pytest.importor" + _SK, "pytest.mark." + _SK, "pytest." + _SK}
    return sorted(
        {
            f"{node.lineno}:{dotted(node)}"
            for node in ast.walk(tree)
            if isinstance(node, (ast.Name, ast.Attribute)) and dotted(node) in forbidden
        }
    )


@pytest.mark.unit
class TestNoSkipVectorInTheDelegationGoldenTests:
    """No test here may skip itself away: neither a module skip nor a bare marker.

    The live gate of the Layer-2 file uses the conditional form and is not an
    unconditional skip. Parse every Python file recursively, including this one.
    String fixtures and comments are not executable skip syntax.
    """

    def test_no_file_in_the_suite_carries_one(self) -> None:
        offenders = {
            path.relative_to(_REPO_ROOT).as_posix(): _skip_vectors(
                path.read_text(encoding="utf-8")
            )
            for path in sorted(
                (_REPO_ROOT / "tests" / "delegation_golden").rglob("*.py")
            )
        }
        assert {k: v for k, v in offenders.items() if v} == {}

    @pytest.mark.parametrize(
        "line",
        [
            "mod = pytest." + "import" + "or" + _SK + "('x')",
            "@pytest.mark." + _SK,
            "@mark." + _SK + "(reason='x')",
            "pytest." + _SK + "('no bus')",
        ],
        ids=["module-level", "bare-marker", "mark-alias", "call"],
    )
    def test_the_scan_flags_each_vector(self, line: str) -> None:
        if line.startswith("@"):
            line += "\ndef test_example(): pass\n"
        assert _skip_vectors(line), line

    @pytest.mark.parametrize(
        "line",
        ["@pytest.mark." + _SK + "if(not LIVE, reason='x')", "@pytest.mark.unit"],
        ids=["conditional-gate", "other-marker"],
    )
    def test_the_scan_leaves_a_conditional_gate_and_other_markers(
        self, line: str
    ) -> None:
        if line.startswith("@"):
            line += "\ndef test_example(): pass\n"
        assert _skip_vectors(line) == []


@pytest.mark.unit
class TestReviewContractCommands:
    def test_ac2_check_uses_the_unbound_item_id(self) -> None:
        path = _REPO_ROOT / "contracts/OMN-24925.yaml"
        data = yaml.safe_load(path.read_text())
        entry = next(
            e
            for e in data["dod_evidence"]
            if e["id"] == "dod-omn24925-ac2-findings-1-2"
        )
        checks = [
            c["check_value"]
            for c in entry["checks"]
            if "contracts/OMN-24831.yaml" in c["check_value"]
        ]
        assert checks
        for command in checks:
            result = subprocess.run(
                command, shell=True, cwd=_REPO_ROOT, capture_output=True, text=True
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "dod-omn24831-ac2-fix-at-the-source" in command
        assert "bound to a local Done gate check" not in entry["description"]

    def test_identity_guard_reads_all_four_surfaces(self) -> None:
        checks = _check_values("contracts/OMN-24986.yaml")
        assert any("_check_live_pr_identity" in value for value in checks)

    @pytest.mark.parametrize("contract", _CONTRACTS)
    def test_every_contract_has_a_nonvacuous_lab_check(self, contract: str) -> None:
        checks = _lab_checks(contract)
        assert checks, contract
        assert all("_check_live_lab_evidence" in value for value in checks), contract

    def test_packet_check_requires_the_full_packet(self) -> None:
        checks = _check_values("contracts/OMN-24986.yaml")
        assert any("_check_live_review_packet" in value for value in checks)

    def test_diff_and_archive_checks_use_the_watcher_head(self) -> None:
        for contract in _CONTRACTS:
            for value in _check_values(contract):
                if "git archive" in value or "git diff" in value:
                    assert "head_sha" in value, (contract, value)
                    assert not re.search(r"\b(?:archive|merge-base) HEAD\b", value), (
                        value
                    )
                    assert "git archive HEAD" not in value

    def test_consumer_bindings_name_the_consumer_test_ids(self) -> None:
        for contract, item_id in (
            ("contracts/OMN-24851.yaml", "dod-omn24851-ac4-consumers"),
            ("contracts/OMN-24925.yaml", "dod-omn24925-ac7-findings-9-10"),
        ):
            data = yaml.safe_load((_REPO_ROOT / contract).read_text())
            entry = next(e for e in data["dod_evidence"] if e["id"] == item_id)
            assert any(
                "::TestNoConsumerClassifiesFailuresByText::" in c["check_value"]
                for c in entry["checks"]
            )


@pytest.mark.unit
class TestReviewSkipScan:
    @pytest.mark.parametrize(
        "relative",
        ["nested/test_added.py", "test_runner_unterminalised_row_omn24831.py"],
    )
    @pytest.mark.parametrize(
        "expression", ["pytest.importor" + _SK + "('missing')", "@pytest.mark." + _SK]
    )
    def test_recursive_scan_includes_itself(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        relative: str,
        expression: str,
    ) -> None:
        suite = tmp_path / "tests/delegation_golden"
        path = suite / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        source = "import pytest\n" + expression + "\n"
        if expression.startswith("@"):
            source += "def test_added(): pass\n"
        path.write_text(source)
        monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(
            sys.modules[__name__],
            "__file__",
            str(suite / "test_runner_unterminalised_row_omn24831.py"),
        )
        with pytest.raises(AssertionError):
            TestNoSkipVectorInTheDelegationGoldenTests().test_no_file_in_the_suite_carries_one()

    def test_ast_ignores_strings_and_comments(self) -> None:
        source = (
            '"""pytest.importor'
            + _SK
            + " and @pytest.mark."
            + _SK
            + ' are documentation."""\n# pytest.'
            + _SK
            + "()\n"
        )
        assert _skip_vectors(source) == []

    @pytest.mark.parametrize(
        "source",
        [
            "import pytest as pt\npt.importor" + _SK + "('x')\n",
            "from pytest import importor" + _SK + " as load\nload('x')\n",
            "from pytest import mark as m\n@m.skip\ndef test_x(): pass\n",
        ],
    )
    def test_ast_finds_import_aliases(self, source: str) -> None:
        assert _skip_vectors(source)


@pytest.mark.unit
class TestAbsentTerminalOk:
    def test_an_absent_key_is_reported_as_absent(self) -> None:
        row = _unterminalised_row()
        del row["terminal_ok"]
        failures = runner_module.evaluate_row(load_corpus().by_id("I9"), row)
        assert len(failures) == 1
        assert "terminal_ok is absent from the row read" in failures[0]
        assert "NULL" not in failures[0]


_SUITE_COMMAND = (
    'uv run pytest tests/delegation_golden -m "not e2e" -q -p no:cacheprovider'
)
_ORIGINAL_TICKET = re.compile(r"(?i)(?<![a-z0-9])omn[-_]24831(?![0-9])")


def _identity_offenders(facts: dict[str, Any], subjects: list[str]) -> list[str]:
    surfaces = {name: facts[name] for name in ("title", "body", "head_ref")}
    surfaces.update({f"commit[{i}]": subject for i, subject in enumerate(subjects)})
    return [
        name for name, value in surfaces.items() if _ORIGINAL_TICKET.search(str(value))
    ]


def _watcher_record() -> dict[str, Any]:
    script = (
        Path(os.environ["OMNI_PLUGIN_ROOT"])
        / "skills/merge-drain/scripts/pr_state_local.py"
    )
    out = subprocess.run(
        [sys.executable, str(script), "--pr", "OmniNode-ai/omnimarket#6326", "--json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    record: dict[str, Any] = json.loads(out[out.index("{") :])
    assert re.fullmatch(r"[0-9a-f]{40}", record["facts"]["head_sha"])
    return record


def _check_live_pr_identity() -> None:
    facts = _watcher_record()["facts"]
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", facts["base"])
    out = subprocess.run(
        ["git", "log", "--format=%s", f"origin/{facts['base']}..{facts['head_sha']}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subjects = out.splitlines()
    assert subjects, "no PR commits read"
    assert not _identity_offenders(facts, subjects), (
        "original ticket referenced by "
        + ", ".join(_identity_offenders(facts, subjects))
    )


def _validate_lab_evidence(
    comment: str, head: str, checks: dict[str, Any], required: list[str]
) -> None:
    """Use the SCM runs shape; required contexts come from settings, not pr-checks."""
    assert re.fullmatch(r"[0-9a-f]{40}", head)
    assert checks["head_sha"] == head
    assert required
    assert all(isinstance(name, str) and name for name in required)
    for key in ("red", "pending", "cancelled"):
        assert key in checks, key
        assert isinstance(checks[key], list), key
        assert checks[key] == [], key
    assert isinstance(checks.get("runs"), list)
    assert checks["runs"]
    for name in required:
        runs = [run for run in checks["runs"] if run[0] == name]
        assert runs, name
        assert all(run[1:3] == ["completed", "success"] for run in runs), name
    assert isinstance(checks.get("detail"), list)
    job_ids: set[str] = set()
    for name in required:
        detail = [row for row in checks["detail"] if row[0] == name]
        assert detail, name
        assert all(str(row[1]).isdigit() for row in detail), name
        job_ids.update(str(row[1]) for row in detail)
    status_jobs = re.findall(
        r"(?m)^.*\| STATUS \|.*job_ids=([0-9]+(?:,[0-9]+)*)(?=\s|\||$)", comment
    )
    assert any(job_ids <= set(value.split(",")) for value in status_jobs), (
        "STATUS does not name the required contexts' job ids"
    )
    contexts = json.dumps(sorted(required), separators=(",", ":"))
    lines = [
        line for line in comment.splitlines() if line.startswith(f"Lab: head={head} ")
    ]
    expected = f"command={_SUITE_COMMAND} exit=0 contexts={contexts}"
    assert any(re.search(re.escape(expected) + r"(?:\s|$)", line) for line in lines), (
        "missing exact suite command, exit or required contexts"
    )


def _review_packet() -> dict[str, Any]:
    """Local Done keeps the existing live comment source; lanes cannot run it."""
    head = _watcher_record()["facts"]["head_sha"]
    out = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            "6326",
            "--repo",
            "OmniNode-ai/omnimarket",
            "--json",
            "comments",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    comments = json.loads(out)["comments"]

    def source_body(number: str) -> str:
        matching = [
            c["body"]
            for c in comments
            if str(c.get("url", "")).endswith("#issuecomment-" + number)
        ]
        assert len(matching) == 1, "source comment unread: " + number
        return str(matching[0])

    source = source_body("6922291635")
    headings = list(re.finditer(r"(?m)^\s*(?:\*\*)?([0-9]+)[.)](?:\*\*)?\s+", source))
    assert [int(match[1]) for match in headings] == list(range(1, 11)), (
        "source findings unread"
    )
    findings = [
        source[
            match.end() : headings[i + 1].start()
            if i + 1 < len(headings)
            else len(source)
        ].strip()
        for i, match in enumerate(headings)
    ]
    candidates = [c for c in comments if c["body"].startswith(f"Lab: head={head} ")]
    assert candidates, "no posted Lab comment for live head"
    comment = candidates[-1]
    return {
        "head_sha": head,
        "comment": comment["body"],
        "comment_id": comment["id"],
        "findings_6922291635": findings,
        "prior_lab_comment_6929592004": source_body("6929592004"),
    }


def _check_live_lab_evidence() -> None:
    packet = _review_packet()
    record = _watcher_record()
    assert record["facts"]["head_sha"] == packet["head_sha"]
    assert isinstance(record.get("ci"), dict)
    assert record["ci"].get("sha") == packet["head_sha"], "CI belongs to another head"
    result = subprocess.run(
        ["onex", "scm", "pr-checks", "OmniNode-ai/omnimarket#6326"],
        check=True,
        capture_output=True,
        text=True,
    )
    checks = json.loads(result.stdout)["result"]
    script = (
        Path(os.environ["OMNI_PLUGIN_ROOT"])
        / "skills/merge-drain/scripts/pr_state_local.py"
    )
    settings = subprocess.run(
        [sys.executable, str(script), "--settings", "omnimarket:dev"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "unread" not in settings
    required = sorted(
        {
            name
            for match in re.finditer(
                r"\b(?:rule_)?contexts=(.*?) (?:required_reviews|rule_strict)=",
                settings,
            )
            for name in match[1].split(",")
            if name != "-"
        }
    )
    _validate_lab_evidence(packet["comment"], packet["head_sha"], checks, required)


def _validate_review_packet(packet: dict[str, Any]) -> None:
    comment = packet["comment"]
    assert comment.startswith(f"Lab: head={packet['head_sha']} ")
    assert "6922291635" in comment
    assert "pr-checks" in comment
    prior_lab = packet["prior_lab_comment_6929592004"]
    assert prior_lab
    assert prior_lab in comment, "prior Lab comment missing"
    for phase in ("RED", "GREEN"):
        assert re.search(rf"(?m)^{phase}:.*tests/[\w/.-]+\.py::\S+", comment), phase
    assert re.search(
        r"(?m)^.*\| STATUS \|.*job_ids=[0-9]+(?:,[0-9]+)*(?=\s|\||$)", comment
    ), "STATUS with job ids"
    quotes = packet["findings_6922291635"]
    assert isinstance(quotes, list)
    assert len(quotes) == 10
    for number, text in enumerate(quotes, 1):
        assert isinstance(text, str), number
        assert len(text.strip()) >= 10, number
        quoted = "\n".join("> " + line for line in f"{number}. {text}".splitlines())
        assert quoted in comment, number


def _check_live_review_packet() -> None:
    packet = _review_packet()
    _check_live_lab_evidence()
    _validate_review_packet(packet)


@pytest.mark.unit
class TestIdentityGuardRejectsEverySurface:
    @pytest.mark.parametrize("surface", ["title", "body", "head_ref", "commit"])
    def test_original_ticket_is_rejected(self, surface: str) -> None:
        facts = {"title": "diagnostic", "body": "diagnostic", "head_ref": "diagnostic"}
        subjects = ["diagnostic"]
        if surface == "commit":
            subjects.append("OMN-24831: diagnostic")
        else:
            facts[surface] += " omn-24831-diagnostic"
        assert _identity_offenders(facts, subjects)

    def test_unrelated_identity_is_accepted(self) -> None:
        assert (
            _identity_offenders(
                {
                    "title": "OMN-25078",
                    "body": "Closes OMN-25078",
                    "head_ref": "diagnostic",
                },
                ["OMN-25078: diagnostic"],
            )
            == []
        )


def _complete_packet() -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Synthetic evidence fixture, never represented as historical PR evidence."""
    head = "a" * 40
    required = ["CI Summary"]
    checks: dict[str, Any] = {
        "head_sha": head,
        "red": [],
        "pending": [],
        "cancelled": [],
        "runs": [["CI Summary", "completed", "success", "timestamp"]],
        "detail": [["CI Summary", "123", "timestamp"]],
    }
    quotes = [f"Synthetic review finding number {i}" for i in range(1, 11)]
    comment = (
        f'Lab: head={head} command={_SUITE_COMMAND} exit=0 contexts=["CI Summary"]\n'
    )
    comment += (
        "RED: tests/example.py::test_guard\nGREEN: tests/example.py::test_guard\n"
    )
    comment += (
        "timestamp | STATUS | lane=fixture | job_ids=123,456 | pr-checks 6922291635\n"
    )
    comment += "\n".join(f"> {i}. {text}" for i, text in enumerate(quotes, 1))
    prior_lab = "Synthetic previous Lab comment 6929592004"
    comment += "\n" + prior_lab
    return (
        {
            "head_sha": head,
            "comment": comment,
            "findings_6922291635": quotes,
            "prior_lab_comment_6929592004": prior_lab,
        },
        checks,
        required,
    )


@pytest.mark.unit
class TestLabEvidenceRejectsEmptyOrUnrelatedProof:
    def test_complete_synthetic_proof_passes(self) -> None:
        packet, checks, required = _complete_packet()
        _validate_lab_evidence(packet["comment"], packet["head_sha"], checks, required)
        _validate_review_packet(packet)

    @pytest.mark.parametrize(
        "mutation",
        [
            "missing-red",
            "null-red",
            "missing-pending",
            "missing-cancelled",
            "empty-required",
            "empty-runs",
            "missing-context",
            "failed-context",
            "pending-context",
            "wrong-head",
            "wrong-command",
            "empty-contexts",
            "missing-detail",
            "empty-detail",
            "unread-job-id",
            "wrong-status-jobs",
            "missing-status",
        ],
    )
    def test_incomplete_lab_proof_fails(self, mutation: str) -> None:
        packet, checks, required = _complete_packet()
        if mutation.startswith("missing-") and mutation not in (
            "missing-context",
            "missing-status",
        ):
            del checks[mutation.removeprefix("missing-")]
        elif mutation == "null-red":
            checks["red"] = None
        elif mutation == "empty-required":
            required.clear()
        elif mutation == "empty-runs":
            checks["runs"] = []
        elif mutation == "missing-context":
            checks["runs"][0][0] = "some other check"
        elif mutation == "failed-context":
            checks["runs"][0][2] = "failure"
        elif mutation == "pending-context":
            checks["runs"][0][1] = "in_progress"
        elif mutation == "wrong-head":
            checks["head_sha"] = "b" * 40
        elif mutation == "empty-detail":
            checks["detail"] = []
        elif mutation == "unread-job-id":
            checks["detail"][0][1] = "unread"
        elif mutation == "wrong-status-jobs":
            packet["comment"] = packet["comment"].replace(
                "job_ids=123,456", "job_ids=7"
            )
        elif mutation == "missing-status":
            packet["comment"] = packet["comment"].replace("| STATUS |", "| fixture |")
        elif mutation == "wrong-command":
            packet["comment"] = packet["comment"].replace(_SUITE_COMMAND, "true")
        else:
            packet["comment"] = packet["comment"].replace(
                'contexts=["CI Summary"]', "contexts="
            )
        with pytest.raises(AssertionError):
            _validate_lab_evidence(
                packet["comment"], packet["head_sha"], checks, required
            )

    @pytest.mark.parametrize(
        "missing",
        [
            "job_ids=123,456",
            "> 1. Synthetic review finding number 1",
            "> 10. Synthetic review finding number 10",
            "RED: tests/example.py::test_guard",
            "GREEN: tests/example.py::test_guard",
        ],
    )
    def test_incomplete_review_packet_fails(self, missing: str) -> None:
        packet, _, _ = _complete_packet()
        packet["comment"] = packet["comment"].replace(missing, "")
        with pytest.raises(AssertionError):
            _validate_review_packet(packet)


@pytest.mark.unit
class TestUnboundLocalItemBlocksDone:
    def test_failing_unbound_item_fails_and_the_passing_control_runs(
        self, tmp_path: Path
    ) -> None:
        from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
            HandlerDodVerify,
        )
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
            ModelDodVerifyStartCommand,
        )
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
            EnumDodVerifyStatus,
            ModelDodVerifyState,
        )

        marker = tmp_path / "source-fix-observed"
        contract = tmp_path / "contract.yaml"
        item = {
            "id": "dod-source-fix-unbound-control",
            "description": "An unbound local item must execute and retain its failing verdict.",
            "execution_scope": "local_done_gate",
            "checks": [{"check_type": "command", "check_value": f"test -f {marker}"}],
        }
        assert "binds_ac" not in item
        contract.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1.0.0",
                    "ticket_id": "OMN-25078",
                    "dod_evidence": [item],
                }
            )
        )
        command = ModelDodVerifyStartCommand(
            ticket_id="OMN-25078",
            contract_path=str(contract),
            execution_audience="local_done_gate",
        )
        failed = HandlerDodVerify().handle(command)
        assert isinstance(failed, ModelDodVerifyState)
        assert failed.status is EnumDodVerifyStatus.FAILED
        assert failed.failed_count == 1
        marker.write_text("the command now has its required file")
        passed = HandlerDodVerify().handle(command)
        assert isinstance(passed, ModelDodVerifyState)
        assert passed.status is EnumDodVerifyStatus.VERIFIED
        assert passed.verified_count == 1

    def test_hosted_command_checks_execute_the_corrected_grep(
        self, tmp_path: Path
    ) -> None:
        from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
            HandlerDodVerify,
        )
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
            ModelDodVerifyStartCommand,
        )
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
            EnumDodVerifyStatus,
            ModelDodVerifyState,
        )

        contract = tmp_path / "contract.yaml"
        command_text = f"grep -nF dod-omn24831-ac2-fix-at-the-source {_REPO_ROOT / 'contracts/OMN-24831.yaml'}"
        contract.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1.0.0",
                    "ticket_id": "OMN-25078",
                    "dod_evidence": [
                        {
                            "id": "dod-hosted-command-control",
                            "checks": [
                                {"check_type": "command", "check_value": command_text}
                            ],
                        }
                    ],
                }
            )
        )
        state = HandlerDodVerify().handle(
            ModelDodVerifyStartCommand(
                ticket_id="OMN-25078",
                contract_path=str(contract),
                execution_audience="hosted",
            )
        )
        assert isinstance(state, ModelDodVerifyState)
        assert state.status is EnumDodVerifyStatus.VERIFIED
        assert state.verified_count == 1


@pytest.mark.unit
class TestLiveEvidenceAdapter:
    def test_settings_names_with_spaces_are_matched_to_scm_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        packet, checks, _ = _complete_packet()
        checks["runs"].append(
            ["Architecture Check", "completed", "success", "timestamp"]
        )
        checks["detail"].append(["Architecture Check", "456", "timestamp"])
        packet["comment"] = packet["comment"].replace(
            'contexts=["CI Summary"]', 'contexts=["Architecture Check","CI Summary"]'
        )
        monkeypatch.setattr(sys.modules[__name__], "_review_packet", lambda: packet)
        monkeypatch.setattr(
            sys.modules[__name__],
            "_watcher_record",
            lambda: {
                "facts": {"head_sha": packet["head_sha"]},
                "ci": {"sha": packet["head_sha"]},
            },
        )
        monkeypatch.setenv("OMNI_PLUGIN_ROOT", str(_REPO_ROOT))
        calls: list[list[str]] = []

        def read(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[:3] == ["onex", "scm", "pr-checks"]:
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"result": checks}), ""
                )
            assert "--settings" in args
            return subprocess.CompletedProcess(
                args,
                0,
                "omnimarket:dev protection=read contexts=CI Summary required_reviews=0 rules=required rule_contexts=Architecture Check rule_strict=False read_at=timestamp",
                "",
            )

        monkeypatch.setattr(subprocess, "run", read)
        _check_live_lab_evidence()
        assert len(calls) == 2

    def test_hosted_contract_commands_exit_zero(self) -> None:
        executed: list[str] = []
        for relative in _CONTRACTS:
            data = yaml.safe_load((_REPO_ROOT / relative).read_text())
            for entry in data["dod_evidence"]:
                if entry.get("execution_scope") == "local_done_gate":
                    continue
                for check in entry["checks"]:
                    if check["check_type"] != "command":
                        continue
                    result = subprocess.run(
                        check["check_value"],
                        shell=True,
                        cwd=_REPO_ROOT,
                        capture_output=True,
                        text=True,
                    )
                    assert result.returncode == 0, (
                        relative,
                        entry["id"],
                        result.stdout,
                        result.stderr,
                    )
                    executed.append(entry["id"])
        assert len(executed) >= 5, executed


@pytest.mark.unit
class TestPinnedDiffSurvivesMerge:
    def test_diff_checks_pin_the_original_base_as_well_as_the_head(self) -> None:
        found = 0
        for relative in _CONTRACTS:
            for value in _check_values(relative):
                if "git diff" in value:
                    found += 1
                    assert f"b={_BASE_PIN}" in value, relative
                    assert 'git merge-base "$h" "$b"' in value, relative
        assert found >= 2


@pytest.mark.unit
class TestReviewPacketSource:
    def test_source_quotes_are_read_from_the_historical_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        packet, _, _ = _complete_packet()
        comments = [
            {
                "id": "source",
                "url": "#issuecomment-6922291635",
                "body": "\n".join(
                    f"**{i}.** {text}"
                    for i, text in enumerate(packet["findings_6922291635"], 1)
                ),
            },
            {
                "id": "previous-lab",
                "url": "#issuecomment-6929592004",
                "body": packet["prior_lab_comment_6929592004"],
            },
            {"id": "current-lab", "url": "#issuecomment-7", "body": packet["comment"]},
        ]
        monkeypatch.setattr(
            sys.modules[__name__],
            "_watcher_record",
            lambda: {"facts": {"head_sha": packet["head_sha"]}},
        )
        calls: list[list[str]] = []

        def read(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"comments": comments}), ""
            )

        monkeypatch.setattr(subprocess, "run", read)
        actual = _review_packet()
        assert actual["findings_6922291635"] == packet["findings_6922291635"]
        assert actual["comment_id"] == "current-lab"
        _validate_review_packet(actual)
        assert len(calls) == 1
        comments.pop(0)
        with pytest.raises(AssertionError, match="source comment unread"):
            _review_packet()

    def test_ci_for_another_head_cannot_supply_the_lab_proof(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        packet, _, _ = _complete_packet()
        monkeypatch.setattr(sys.modules[__name__], "_review_packet", lambda: packet)
        monkeypatch.setattr(
            sys.modules[__name__],
            "_watcher_record",
            lambda: {
                "facts": {"head_sha": packet["head_sha"]},
                "ci": {"sha": "b" * 40},
            },
        )
        with pytest.raises(AssertionError, match="CI belongs to another head"):
            _check_live_lab_evidence()


@pytest.mark.unit
class TestContractShellSyntax:
    def test_all_six_contracts_have_valid_shell_commands(self) -> None:
        for relative in (*_CONTRACTS, "contracts/OMN-25078.yaml"):
            for value in _check_values(relative):
                result = subprocess.run(
                    ["bash", "-n", "-c", value], capture_output=True, text=True
                )
                assert result.returncode == 0, (relative, result.stderr)
