# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Layer-2 delegation regression tests — live golden tasks, NIGHTLY (OMN-13540).

Each integration corpus case is published to the live bus via the delegate-skill
command topic, the ``delegation_events`` projection row is read, and the case's
``expected`` block is asserted behaviorally (STRUCTURE/BEHAVIOR only — never exact
LLM output). Known-broken cases are marked xfail with the tracking ticket so the
nightly is actionable, not perpetually-red noise; the assertion text still
encodes the CORRECT (intended) expectation.

Live tests are gated on OMN_ALLOW_LIVE_E2E_PROBE=true and skip without it, so the
module is import-safe and the pure-logic tests below run in the standard PR CI.

Run nightly against the stability-test lane (set OMN_ALLOW_LIVE_E2E_PROBE=true,
ONEX_E2E_LANE=stability-test, and the ONEX_E2E_* connection env vars — see
``tests/delegation_golden/runner.py`` for the full lane-config env list and
defaults):

    uv run pytest tests/delegation_golden/test_layer2_live_runner.py -v -m e2e
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tests.delegation_golden.corpus_loader import (
    ModelCorpusCase,
    load_corpus,
)
from tests.delegation_golden.runner import (
    CaseResult,
    Scoreboard,
    evaluate_row,
)

_ALLOW_FLAG = "OMN_ALLOW_LIVE_E2E_PROBE"

_CORPUS = load_corpus()
_INTEGRATION_CASES = list(_CORPUS.integration_cases())


# ---------------------------------------------------------------------------
# Pure-logic coverage — runs in standard PR CI (no live lane).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunnerEvaluationLogic:
    """The behavioral evaluator is deterministic and testable without a bus."""

    def test_free_local_zero_cost_pass(self) -> None:
        case = _CORPUS.by_id("I1")
        row = {
            "delegated_to": "local-coder",
            "model_name": "Qwen3.6-35B-A3B",
            "tokens_input": 12,
            "tokens_output": 30,
            "cost_usd": 0.0,
            "quality_gate_passed": True,
        }
        assert evaluate_row(case, row) == []

    def test_free_local_nonzero_cost_fails_the_expectation(self) -> None:
        """I1 expects cost=zero on the free local tier; a billed row must fail."""
        case = _CORPUS.by_id("I1")
        row = {
            "delegated_to": "local-coder",
            "model_name": "Qwen3.6-35B-A3B",
            "tokens_input": 12,
            "tokens_output": 30,
            "cost_usd": 0.004,
            "quality_gate_passed": True,
        }
        failures = evaluate_row(case, row)
        assert any("cost" in f for f in failures), failures

    def test_metered_escalation_requires_positive_cost(self) -> None:
        """I4 encodes the INTENDED metered-cost expectation (OMN-13408).

        A cloud/metered escalation with cost_usd=0.0 is the exact regression this
        case guards — it must register as a failure (RED until OMN-13408 lands).
        """
        case = _CORPUS.by_id("I4")
        row = {
            "delegated_to": "cloud-glm",
            "model_name": "glm-5.2",
            "tokens_input": 800,
            "tokens_output": 2400,
            "cost_usd": 0.0,  # the bug
            "quality_gate_passed": True,
        }
        failures = evaluate_row(case, row)
        assert any("cost" in f for f in failures), failures

        row["cost_usd"] = 0.0123  # the fixed behavior
        assert evaluate_row(case, row) == []

    def test_cross_cutting_completed_requires_model_and_tokens(self) -> None:
        """I9 invariant: a completed row with empty telemetry must fail."""
        case = _CORPUS.by_id("I9")
        empty = {
            "model_name": "",
            "tokens_input": 0,
            "tokens_output": 0,
            "cost_usd": 0.0,
        }
        failures = evaluate_row(case, empty)
        assert failures, "empty completed row should fail the cross-cutting invariant"

        good = {
            "model_name": "Qwen3.6-35B-A3B",
            "tokens_input": 10,
            "tokens_output": 20,
            "cost_usd": 0.0,
        }
        assert evaluate_row(case, good) == []

    def test_scoreboard_hard_failures_exclude_xfail(self) -> None:
        """An xfail-marked failing case is not a hard failure; a plain one is."""
        scoreboard = Scoreboard(
            lane="stability-test",
            corpus_version="1.0.0",
            started_at="t0",
            finished_at="t1",
            results=[
                CaseResult(
                    case_id="I4",
                    task_type="code_generation",
                    correlation_id="c1",
                    passed=False,
                    xfail_ticket="OMN-13408",
                    failures=["cost: expected positive"],
                ),
                CaseResult(
                    case_id="I3",
                    task_type="code_generation",
                    correlation_id="c2",
                    passed=False,
                    xfail_ticket=None,
                    failures=["terminal: expected completed"],
                ),
            ],
        )
        hard = {r.case_id for r in scoreboard.hard_failures}
        assert hard == {"I3"}

    def test_scoreboard_xpass_flags_stale_xfail(self) -> None:
        """An xfail case that PASSED signals the tracked fix has landed."""
        scoreboard = Scoreboard(
            lane="stability-test",
            corpus_version="1.0.0",
            started_at="t0",
            finished_at="t1",
            results=[
                CaseResult(
                    case_id="I4",
                    task_type="code_generation",
                    correlation_id="c1",
                    passed=True,
                    xfail_ticket="OMN-13408",
                ),
            ],
        )
        assert {r.case_id for r in scoreboard.xpass} == {"I4"}


@pytest.mark.unit
class TestLaneWiringFailsClosed:
    """OMN-18349: an unpublishable lane raises; it never falls back to a literal."""

    def test_unknown_lane_is_refused(self) -> None:
        from tests.delegation_golden.runner import (
            LaneNotPublishableError,
            resolve_lane_bus,
        )

        with pytest.raises(LaneNotPublishableError) as excinfo:
            resolve_lane_bus("does-not-exist")
        assert "does-not-exist" in str(excinfo.value)

    def test_inmemory_lane_is_refused(self) -> None:
        """The overlay declares `stability: inmemory`, meaning do not publish.

        The predecessor of this code would have published to a hardcoded
        stability address regardless of what the overlay said.
        """
        from tests.delegation_golden.runner import (
            LaneNotPublishableError,
            resolve_lane_bus,
        )

        with pytest.raises(LaneNotPublishableError) as excinfo:
            resolve_lane_bus("stability-test")
        assert "inmemory" in str(excinfo.value)

    def test_dev_lane_resolves_address_and_transport_from_the_overlay(self) -> None:
        from tests.delegation_golden.runner import resolve_lane_bus

        bootstrap, protocol, mechanism = resolve_lane_bus("dev")
        assert ":" in bootstrap
        assert protocol == "SASL_PLAINTEXT"
        assert mechanism == "SCRAM-SHA-256"

    def test_the_module_carries_no_broker_literal(self) -> None:
        """The positive control for the two assertions above.

        A hardcoded bootstrap default is exactly what let this runner publish
        to the wrong lane over the wrong transport for 60 consecutive nights.
        Executable string constants only: the header comment and the
        docstrings deliberately QUOTE the address that used to be here, and a
        naive text scan would forbid the module from explaining itself.
        """
        import ast
        import inspect

        from tests.delegation_golden import runner as runner_module

        tree = ast.parse(inspect.getsource(runner_module))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node,
                ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        code_strings = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        offenders = [s for s in code_strings if ":39092" in s or ":19092" in s]
        assert offenders == [], offenders


@pytest.mark.unit
class TestDeclaredPollBound:
    """The projection deadline comes from the contract, not from a literal."""

    def test_poll_bound_is_the_declared_completion_bound_plus_margin(self) -> None:
        from omnimarket.cloud.completion_bound import read_declared_completion_bound
        from tests.delegation_golden.runner import (
            _DEFAULT_PROJECTION_MARGIN_S,
            poll_timeout_s,
        )

        declared = read_declared_completion_bound().max_wall_seconds
        assert poll_timeout_s() == float(declared) + _DEFAULT_PROJECTION_MARGIN_S

    def test_poll_bound_is_not_shorter_than_the_platform_bound(self) -> None:
        """The defect: a 330s probe against a 900s platform reports noise.

        Same shape as the hardcoded 300s OMN-18296 removed from the CLI.
        """
        from omnimarket.cloud.completion_bound import read_declared_completion_bound
        from tests.delegation_golden.runner import poll_timeout_s

        assert poll_timeout_s() >= float(
            read_declared_completion_bound().max_wall_seconds
        )

    def test_explicit_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.delegation_golden.runner import poll_timeout_s

        monkeypatch.setenv("ONEX_E2E_POLL_TIMEOUT_S", "7")
        assert poll_timeout_s() == 7.0


@pytest.mark.unit
class TestFatalScoreboardIsAlwaysWritten:
    """A red night that uploads nothing cannot be told from a night that never ran."""

    def test_fatal_scoreboard_has_the_normal_envelope_plus_the_cause(self) -> None:
        from tests.delegation_golden.runner import fatal_scoreboard

        board = fatal_scoreboard(RuntimeError("no route to the lane"))
        assert board["summary"] == {
            "total": 0,
            "passed": 0,
            "hard_failures": 0,
            "xpass": 0,
        }
        assert board["results"] == []
        assert board["fatal"]["error_class"] == "RuntimeError"
        assert "no route to the lane" in board["fatal"]["error"]

    def test_main_writes_the_artifact_when_the_run_dies(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The falsification control for the fix: force a fatal and demand a file."""
        import json
        import sys

        from tests.delegation_golden import runner as runner_module

        async def _explode(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("lane unreachable in this test")

        monkeypatch.setattr(runner_module, "run_corpus", _explode)
        out = tmp_path / "scoreboard.json"
        monkeypatch.setattr(sys, "argv", ["runner", "--out", str(out)])

        assert runner_module._main() == 1
        written = json.loads(out.read_text())
        assert written["fatal"]["error_class"] == "RuntimeError"
        assert "lane unreachable in this test" in written["fatal"]["error"]


# ---------------------------------------------------------------------------
# Live golden-task assertions — NIGHTLY, gated on the live-lane flag.
# ---------------------------------------------------------------------------


def _case_param(case: ModelCorpusCase) -> Any:
    """Wrap a case in a pytest.param, applying xfail when known-broken today."""
    marks = []
    if case.xfail is not None:
        marks.append(
            pytest.mark.xfail(
                reason=f"{case.xfail.reason} (tracking {case.xfail.ticket})",
                strict=False,
            )
        )
    return pytest.param(case, id=case.id, marks=marks)


@pytest.mark.integration
@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get(_ALLOW_FLAG, "").lower() != "true",
    reason=(
        f"Requires {_ALLOW_FLAG}=true to run against the live bus. "
        "Set it explicitly to execute the nightly golden-task probe."
    ),
)
class TestDelegationGoldenTasksLive:
    """Publish each integration case to the live bus; assert the expected block."""

    @pytest.mark.parametrize("case", [_case_param(c) for c in _INTEGRATION_CASES])
    async def test_case_behaves_as_expected(self, case: ModelCorpusCase) -> None:
        # Imported lazily so the module imports without asyncpg/aiokafka present
        # in a unit-only environment.
        from tests.delegation_golden.runner import (
            _command_topic,
            connect_lane_postgres,
            run_case,
        )

        # OMN-18349: the shared fail-fast connect, not a bare asyncpg.connect
        # and not a skip. An unreachable lane now fails in seconds naming the
        # address and the runner placement, instead of nine consecutive
        # 60-second hangs each surfacing as a bare TimeoutError that reads like
        # a delegation regression. A missing lane password is likewise a
        # failure here, not a silent skip: this module only ever runs behind
        # the nightly live-probe flag, where a skip is the absence of evidence.
        conn = await connect_lane_postgres()
        try:
            result = await run_case(conn, _command_topic(), case)
        finally:
            await conn.close()

        assert result.passed, (
            f"case {case.id} behavioral expectation failed: {result.failures} "
            f"(model={result.model_name} cost={result.cost_usd} "
            f"tokens={result.tokens_input}/{result.tokens_output} "
            f"terminal={result.terminal})"
        )
