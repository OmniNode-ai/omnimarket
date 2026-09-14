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

    def test_a_lane_declared_inmemory_is_refused(self) -> None:
        """`prod` is declared `inmemory`, meaning no publisher may target it.

        Reached through the overlay rather than the lane allowlist, so this
        exercises the fail-closed branch and not the name check above.
        """
        from tests.delegation_golden.runner import (
            LaneNotPublishableError,
            resolve_lane_bus,
        )

        with pytest.raises(LaneNotPublishableError) as excinfo:
            resolve_lane_bus("prod")
        assert "prod" in str(excinfo.value)

    def test_dev_lane_resolves_address_and_transport_from_the_overlay(self) -> None:
        from tests.delegation_golden.runner import resolve_lane_bus

        bootstrap, protocol, mechanism = resolve_lane_bus("dev")
        assert ":" in bootstrap
        assert protocol == "SASL_PLAINTEXT"
        assert mechanism == "SCRAM-SHA-256"

    def test_stability_lane_resolves_to_its_own_declared_plaintext_listener(
        self,
    ) -> None:
        """The two lanes differ in BOTH address and transport.

        A single default for both is what the deleted literal was, and it was
        wrong for whichever lane it was not written for.
        """
        from tests.delegation_golden.runner import resolve_lane_bus

        dev_bootstrap, dev_protocol, _ = resolve_lane_bus("dev")
        stability_bootstrap, stability_protocol, mechanism = resolve_lane_bus(
            "stability-test"
        )
        assert stability_protocol == "PLAINTEXT"
        assert mechanism == ""
        assert stability_bootstrap != dev_bootstrap
        assert stability_protocol != dev_protocol

    def test_the_overlay_stability_key_stays_unpublishable(self) -> None:
        """The OCC publishers' accidental-lane guard is not weakened by this PR.

        `stability-test` is this runner's lane; `stability` is the id an OCC
        publisher could pass by mistake, and it must still resolve to a no-op.
        """
        from ci_bus_lanes import MODE_INMEMORY, load_lane_overlay, resolve_lane_broker

        mode, _ = resolve_lane_broker(load_lane_overlay(), "stability")
        assert mode == MODE_INMEMORY

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
    """Assert each integration case against ONE corpus run, not nine.

    OMN-18349: this class used to publish the whole corpus a SECOND time, one
    case per test, each waiting out its own projection deadline in sequence.
    That doubled the delegations the lane served every night, and with the
    deadline correctly derived from the contract-declared completion bound the
    sequential worst case exceeds the job budget outright -- the step would be
    killed by the job timeout, which skips the artifact uploads and leaves the
    night with no evidence at all. It also meant the scoreboard artifact and
    these assertions described two different runs.

    The corpus is now run once per session and every case asserts against that
    one scoreboard. The runner step's own artifact is preferred when present,
    so in CI these assertions describe exactly the run that was uploaded.
    """

    @pytest.fixture(scope="session")
    def scoreboard_results(self) -> dict[str, Any]:
        """Case id -> CaseResult for one corpus run, reused by every case."""
        import asyncio
        import json
        import os
        from pathlib import Path

        from tests.delegation_golden.runner import run_corpus

        # Prefer the scoreboard the runner step already produced: asserting
        # against a different run than the one uploaded is how a green test
        # and a red artifact come to disagree.
        artifact = Path(
            os.environ.get(
                "ONEX_E2E_SCOREBOARD_PATH", "delegation_regression_scoreboard.json"
            )
        )
        if artifact.is_file():
            payload = json.loads(artifact.read_text())
            fatal = payload.get("fatal")
            if fatal:
                # The corpus run died before any case completed. Re-running it
                # here would hit the same wall and burn the job budget doing
                # it, so every case fails now, naming the recorded cause.
                pytest.fail(
                    f"the corpus run died before any case completed: "
                    f"{fatal.get('error_class')}: {fatal.get('error')}"
                )
            if payload.get("results"):
                return {row["case_id"]: row for row in payload["results"]}

        scoreboard = asyncio.run(run_corpus())
        return {
            result.case_id: {
                "case_id": result.case_id,
                "passed": result.passed,
                "failures": result.failures,
                "error": result.error,
                "model_name": result.model_name,
                "cost_usd": result.cost_usd,
                "tokens_input": result.tokens_input,
                "tokens_output": result.tokens_output,
                "terminal": result.terminal,
            }
            for result in scoreboard.results
        }

    @pytest.mark.parametrize("case", [_case_param(c) for c in _INTEGRATION_CASES])
    def test_case_behaves_as_expected(
        self, case: ModelCorpusCase, scoreboard_results: dict[str, Any]
    ) -> None:
        result = scoreboard_results.get(case.id)
        assert result is not None, (
            f"case {case.id} is absent from the corpus run. A case the runner "
            "never attempted is not a pass; it is missing evidence."
        )
        assert result["passed"], (
            f"case {case.id} behavioral expectation failed: {result['failures']} "
            f"(model={result['model_name']} cost={result['cost_usd']} "
            f"tokens={result['tokens_input']}/{result['tokens_output']} "
            f"terminal={result['terminal']})"
        )
