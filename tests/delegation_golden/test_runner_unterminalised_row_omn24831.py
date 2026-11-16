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
    return [v for v in _check_values(contract) if "Lab: head=$h" in v]


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
            for needle in ("head_sha", "command=", "exit=0", "contexts=", "pr-checks"):
                assert needle in value, (needle, value)

    def test_the_lab_comment_checks_exist(self) -> None:
        """Control: at least the three contracts that read the comment are scanned."""
        assert sum(bool(_lab_checks(c)) for c in _CONTRACTS) >= 3


_SK_WORD = "ski" + "p"


_BASE_PIN = "6efbe3ee5027f6efb67d0b81a1421f0dda32b4e5"


@pytest.mark.unit
class TestContractTextAnswersTheReview:
    """What the contracts say about the base, the review heads and the diff checks."""

    def test_the_fails_on_base_checks_pin_the_base_and_require_the_nine_ids(
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
            assert "(^| )9 failed" in value, contract
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
    return [p.pattern for p in (_IMPORT_OR_SKIP, _UNCONDITIONAL_SKIP) if p.search(text)]


@pytest.mark.unit
class TestNoSkipVectorInTheDelegationGoldenTests:
    """No test here may skip itself away: neither a module skip nor a bare marker.

    The live gate of the Layer-2 file uses the conditional form and is not an
    unconditional skip. Run on the files of the suite, not on this one, whose own
    pattern text is the control below.
    """

    def test_no_file_in_the_suite_carries_one(self) -> None:
        here = Path(__file__).resolve()
        offenders = {
            path.name: _skip_vectors(path.read_text(encoding="utf-8"))
            for path in sorted(
                (_REPO_ROOT / "tests" / "delegation_golden").glob("*.py")
            )
            if path.resolve() != here
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
        assert _skip_vectors(line), line

    @pytest.mark.parametrize(
        "line",
        ["@pytest.mark." + _SK + "if(not LIVE, reason='x')", "@pytest.mark.unit"],
        ids=["conditional-gate", "other-marker"],
    )
    def test_the_scan_leaves_a_conditional_gate_and_other_markers(
        self, line: str
    ) -> None:
        assert _skip_vectors(line) == []
