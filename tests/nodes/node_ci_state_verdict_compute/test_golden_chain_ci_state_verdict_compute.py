# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20686: what ci-watch prints for a PR's CI, contract to bus to typed result.

Parity: tests/fixtures/ci_state_verdict_parity.json holds cases captured by running the retired
ci_state.py on the facts in each request (the watcher record, or the PR, required contexts, check-run
copies, statuses, workflow runs and check suites a GitHub read returned, or a failing log). The node's
stdout, stderr, exit code, scratch files and watcher fallback reason must equal what the retired script
did. Golden chain: a request through the registered contract on the in-memory bus. Error chain: a
malformed request is refused with no result.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_ci_state_verdict_compute as node_package
from omnimarket.nodes.node_ci_state_verdict_compute.handlers.handler_ci_state_verdict import (
    HandlerCiStateVerdict,
)
from omnimarket.nodes.node_ci_state_verdict_compute.models.model_ci_state_verdict import (
    ModelCiStateVerdictRequest,
    ModelCiStateVerdictResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.ci-state-verdict-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.ci-state-verdict-completed.v1"
PARITY = json.loads((ROOT / "tests/fixtures/ci_state_verdict_parity.json").read_text())
HANDLER = HandlerCiStateVerdict()
SHA = "0123456789abcdef0123456789abcdef01234567"
NOW = "2026-10-09T12:00:00+00:00"


def decide(payload: dict[str, Any]) -> ModelCiStateVerdictResult:
    return HANDLER.handle(ModelCiStateVerdictRequest.model_validate(payload))


def _as_old(result: ModelCiStateVerdictResult, watcher: bool = False) -> dict[str, Any]:
    """The result in the shape the retired script's captured run is stored in."""
    seen: dict[str, Any] = {
        "stdout_lines": result.stdout_lines,
        "stderr_lines": result.stderr_lines,
        "exit_code": result.exit_code,
        "files": result.files,
    }
    if watcher:
        seen["fallback_reason"] = result.fallback_reason
    return seen


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeCiStateVerdictCompute, handler_type)
    for side, model in (
        ("input_model", ModelCiStateVerdictRequest),
        ("output_model", ModelCiStateVerdictResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_ci_state_verdict_compute"].load() is node_package


@pytest.mark.parametrize("case", PARITY["cases"], ids=lambda c: str(c["name"]))
def test_old_behavior_parity(case: dict[str, Any]) -> None:
    watcher = case["request"]["operation"] == "watcher"
    assert _as_old(decide(case["request"]), watcher) == case["expected"]


def test_parity_fixture_exercises_every_branch() -> None:
    operations = {c["request"]["operation"] for c in PARITY["cases"]}
    assert operations == {"watcher", "live", "classify-log"}
    text = "\n".join(
        line
        for c in PARITY["cases"]
        for line in c["expected"]["stdout_lines"] + c["expected"]["stderr_lines"]
    )
    for needle in (
        "VERDICT GREEN",
        "VERDICT RED",
        "VERDICT PENDING",
        "source=watcher",
        "PENDING (red copy's run",
        "STUCK-CHECK-SUITE",
        "MISMATCH: required contexts read green",
        "MISMATCH: a required context reads red",
        "ERROR: zero check-runs",
        "WARNING: no required contexts",
        "MISSING (not reported",
        "RED status:",
        "PENDING (status)",
        "C5 candidates",
        "CLASS C13",
        "CLASS C10",
        "CLASS UNCLASSIFIED",
    ):
        assert needle in text, needle
    fallbacks = {
        c["expected"].get("fallback_reason")
        for c in PARITY["cases"]
        if c["request"]["operation"] == "watcher"
    }
    assert None in fallbacks
    assert any(r and "is not in the watcher state" in r for r in fallbacks)
    assert any(r and "never reads the CI of a draft PR" in r for r in fallbacks)
    exits = {c["expected"]["exit_code"] for c in PARITY["cases"]}
    assert {0, 1, 2, 3} <= exits


def _live(**overrides: Any) -> dict[str, Any]:
    run = {
        "id": 1,
        "name": "CI Summary",
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-10-09T11:00:00Z",
        "completed_at": "2026-10-09T11:05:00Z",
        "details_url": "https://github.com/o/r/actions/runs/9/job/8",
        "suite": 5,
    }
    payload: dict[str, Any] = {
        "operation": "live",
        "full": "OmniNode-ai/omnimarket",
        "number": "42",
        "now": NOW,
        "live_flag": True,
        "pr": {
            "headRefOid": SHA,
            "isDraft": False,
            "baseRefName": "dev",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "autoMergeRequest": None,
            "updatedAt": "2026-10-09T11:00:00Z",
            "state": "OPEN",
            "labels": [],
        },
        "required": {"CI Summary": "ruleset"},
        "check_runs": [run],
        "check_runs_total": 1,
    }
    payload.update(overrides)
    return payload


def test_a_green_required_context_is_green_and_exits_zero() -> None:
    result = decide(_live())
    assert (result.action, result.exit_code, result.verdict) == ("answer", 0, "GREEN")
    assert result.stdout_lines[-1] == (
        f"VERDICT GREEN {SHA} required=1 checkruns=1/1 mergeStateStatus=CLEAN read_at=2026-10-09T12:00:00Z"
    )
    assert result.files["required.txt"] == "CI Summary\truleset\n"
    assert "reds.tsv" in result.files


def test_the_newest_copy_decides_not_an_older_red_one() -> None:
    older = {
        "id": 0,
        "name": "CI Summary",
        "status": "completed",
        "conclusion": "failure",
        "started_at": "2026-10-09T10:00:00Z",
        "completed_at": "2026-10-09T10:01:00Z",
        "details_url": None,
        "suite": 4,
    }
    base = _live()
    result = decide(
        {**base, "check_runs": [older, *base["check_runs"]], "check_runs_total": 2}
    )
    assert result.verdict == "GREEN"
    assert any("1 older non-green copies" in line for line in result.stdout_lines)


def test_a_copy_without_timestamps_sorts_newest_so_it_never_hides_behind_an_older_success() -> (
    None
):
    base = _live()
    queued = {
        **base["check_runs"][0],
        "id": 2,
        "status": "queued",
        "conclusion": None,
        "started_at": None,
        "completed_at": None,
    }
    result = decide(
        {**base, "check_runs": [*base["check_runs"], queued], "check_runs_total": 2}
    )
    assert result.verdict == "PENDING"
    assert result.exit_code == 2


def test_zero_check_runs_and_zero_statuses_is_untrusted_exit_three() -> None:
    result = decide(_live(check_runs=[], check_runs_total=0))
    assert result.exit_code == 3
    assert result.stderr_lines[0].startswith("ERROR: zero check-runs and zero statuses")
    assert "reds.tsv" not in result.files
    assert not any(line.startswith("VERDICT") for line in result.stdout_lines)


def test_a_green_that_github_calls_blocked_asks_for_the_check_suites_once() -> None:
    blocked = _live()
    blocked["pr"]["mergeStateStatus"] = "BLOCKED"
    first = decide(blocked)
    assert (first.action, first.exit_code, first.stdout_lines) == (
        "read-check-suites",
        None,
        [],
    )
    stuck = {
        "id": 77,
        "app": "codecov",
        "status": "queued",
        "conclusion": None,
        "created_at": "2026-10-09T09:00:00Z",
        "updated_at": "2026-10-09T09:00:00Z",
    }
    second = decide({**blocked, "check_suites": [stuck], "check_suites_read": True})
    assert second.action == "answer"
    assert any(
        line.startswith(
            "   STUCK-CHECK-SUITE app=codecov suite=77 status=queued age=3:00:00 "
        )
        for line in second.stdout_lines
    )
    fresh = decide(
        {
            **blocked,
            "check_suites": [{**stuck, "created_at": "2026-10-09T11:50:00Z"}],
            "check_suites_read": True,
        }
    )
    assert not any("STUCK-CHECK-SUITE" in line for line in fresh.stdout_lines)


def test_a_red_or_clean_github_never_asks_for_check_suites() -> None:
    assert decide(_live()).action == "answer"
    red = _live()
    red["check_runs"][0]["conclusion"] = "failure"
    red["pr"]["mergeStateStatus"] = "BLOCKED"
    assert decide(red).action == "answer"


def _watcher(**ci: Any) -> dict[str, Any]:
    return {
        "operation": "watcher",
        "full": "OmniNode-ai/omnimarket",
        "number": "42",
        "now": NOW,
        "reads_line": "tick 2026-10-09T11:59:30Z age=30s",
        "watcher": {
            "facts": {
                "state": "OPEN",
                "draft": False,
                "head_sha": SHA,
                "base": "dev",
                "labels": ["hold"],
                "armed": True,
            },
            "ci": {
                "sha": SHA,
                "verdict": "RED",
                "runs": [["lint", "completed", "failure", "2026-10-09T11:00:00Z"]],
                "red": ["lint"],
                "pending": [],
                "read_at": "2026-10-09T11:59:00Z",
                "total": 1,
                **ci,
            },
        },
    }


def test_a_fresh_watcher_answers_without_github() -> None:
    result = decide(_watcher())
    assert (result.action, result.exit_code, result.verdict) == ("answer", 1, "RED")
    assert result.stdout_lines[-1] == (
        f"VERDICT RED {SHA} required=unread names=1 copies=1 mergeStateStatus=unread "
        "read_at=2026-10-09T11:59:00Z source=watcher"
    )
    assert json.loads(result.files["pr.json"])["autoMergeRequest"] == {"enabled": True}


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"state": "MERGED"}, "watcher state has the PR as MERGED"),
        ({"draft": True}, "the watcher never reads the CI of a draft PR"),
        (
            {"head_sha": "f" * 40},
            "the watcher has not read CI at head ffffffffffff yet (ci=UNREAD)",
        ),
    ],
)
def test_a_watcher_that_cannot_answer_sends_the_caller_to_github(
    mutation: dict[str, Any], reason: str
) -> None:
    payload = _watcher()
    payload["watcher"]["facts"].update(mutation)
    result = decide(payload)
    assert (result.action, result.fallback_reason, result.exit_code) == (
        "read-github",
        reason,
        None,
    )
    assert not result.stdout_lines
    assert not result.files


def test_a_watcher_without_a_verdict_or_a_record_sends_the_caller_to_github() -> None:
    assert (
        decide(_watcher(verdict="UNREAD")).fallback_reason
        == "the watcher read no check-runs at this head (ci=UNREAD)"
    )
    gone = {
        **_watcher(),
        "watcher": None,
        "watcher_unavailable": "omnimarket#42 is not in the watcher state (state.json)",
    }
    assert (
        decide(gone).fallback_reason
        == "omnimarket#42 is not in the watcher state (state.json)"
    )


def test_the_log_classifier_names_the_missing_evidence_line() -> None:
    log = "RECEIPT GATE FAILED: missing Evidence-Ticket line"
    result = decide({"operation": "classify-log", "log_text": log})
    assert result.exit_code == 1
    assert result.stdout_lines[0].startswith("CLASS C13 missing=Evidence-Ticket: ")
    none = decide({"operation": "classify-log", "log_text": "all fine"})
    assert (none.exit_code, none.stdout_lines) == (
        2,
        ["CLASS UNCLASSIFIED: use the remaining ci-watch class table"],
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "operation"),
        ({"operation": "merge"}, "operation"),
        ({"operation": "live", "check_runs_total": "many"}, "check_runs_total"),
        ({"operation": "live", "surprise": 1}, "surprise|Extra"),
        ({"operation": "live", "check_runs": [{"id": 1}]}, "name"),
    ],
)
def test_error_chain_refuses_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelCiStateVerdictRequest.model_validate(payload)


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_matches_a_captured_case(
    tmp_path: Path,
) -> None:
    case = next(
        c
        for c in PARITY["cases"]
        if c["request"]["operation"] == "live" and c["expected"]["exit_code"] == 1
    )
    runtime = await _run(tmp_path, case["request"])
    result = runtime.handler_result
    assert isinstance(result, ModelCiStateVerdictResult)
    assert _as_old(result) == case["expected"]
    assert (
        ModelCiStateVerdictResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {})
    assert runtime.handler_result is None
    (tmp_path / "state2").mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"operation": "merge"}))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "state2",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
