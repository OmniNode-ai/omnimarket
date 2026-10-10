# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20885: the pr-title / check-title decision, rehomed from the change-control reusable.

The parity fixture holds cases captured by running the pinned change-control
check-title bash step. Recorded cases replay the title and author of a real
check-title job, and the fixture's capture asserted that the step's first output
line and admit or refuse equal that job's log. No change-control source is needed
to run these tests.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_pr_title_check_compute as node_package
from omnimarket.nodes.node_pr_title_check_compute.handlers.handler_pr_title_check import (
    HandlerPrTitleCheck,
)
from omnimarket.nodes.node_pr_title_check_compute.models.model_pr_title_check import (
    EnumPrTitleCheckReason,
    ModelPrTitleCheckRequest,
    ModelPrTitleCheckResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.pr-title-check-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.pr-title-check-completed.v1"
PARITY = json.loads((ROOT / "tests/fixtures/pr_title_check_parity.json").read_text())
CASES: list[dict[str, Any]] = PARITY["cases"]
HANDLER = HandlerPrTitleCheck()


def decide(payload: dict[str, Any]) -> ModelPrTitleCheckResult:
    return HANDLER.handle(ModelPrTitleCheckRequest.model_validate(payload))


@pytest.mark.parametrize("case", CASES, ids=lambda c: str(c["name"]))
def test_decision_matches_the_change_control_step(case: dict[str, Any]) -> None:
    result = decide(case["request"])
    expected = case["expected"]
    assert result.admitted is (expected["exit_code"] == 0)
    assert result.exit_code == expected["exit_code"]
    assert list(result.output_lines) == expected["stdout_lines"]


def test_fixture_replays_recorded_admits_refusals_and_bot_exemptions() -> None:
    recorded = [c for c in CASES if c["source"]["kind"] == "recorded_check_title_job"]
    for case in recorded:
        source = case["source"]
        result = decide(case["request"])
        assert result.admitted is source["recorded_admitted"]
        assert result.output_lines[0] == source["recorded_first_line"]
    reasons = {decide(c["request"]).reason for c in recorded}
    assert {
        EnumPrTitleCheckReason.TICKET_REFERENCE,
        EnumPrTitleCheckReason.BOT_AUTHOR,
        EnumPrTitleCheckReason.MISSING_TICKET_REFERENCE,
    } <= reasons
    assert any(c["source"]["repository"] == "OmniNode-ai/omniclaude" for c in recorded)
    every = {decide(c["request"]).reason for c in CASES}
    assert every == set(EnumPrTitleCheckReason)


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]
    assert contract["handler_routing"]["handlers"][0]["operation"] == "check_pr_title"
    assert contract["metadata"]["related_tickets"] == ["OMN-20885"]
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodePrTitleCheckCompute, handler_type)
    for side, model in (
        ("input_model", ModelPrTitleCheckRequest),
        ("output_model", ModelPrTitleCheckResult),
    ):
        module = importlib.import_module(contract[side]["module"])
        assert getattr(module, contract[side]["name"]) is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_pr_title_check_compute"].load() is node_package


@pytest.mark.parametrize(
    "payload",
    [{}, {"title": "x"}, {"author": "x"}, {"title": "x", "author": "y", "extra": 1}],
)
def test_error_chain_refuses_malformed_requests(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelPrTitleCheckRequest.model_validate(payload)


def _cli(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "omnimarket.nodes.node_pr_title_check_compute"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "name",
    [
        "recorded-omniclaude-2649-ticket",
        "recorded-omniclaude-2646-writer-bot",
        "recorded-omnimarket-refuse",
        "synthetic-two-tickets",
    ],
)
def test_cli_prints_the_step_output_and_exits_with_its_code(name: str) -> None:
    case = next(c for c in CASES if c["name"] == name)
    proc = _cli(json.dumps(case["request"]))
    assert proc.returncode == case["expected"]["exit_code"]
    assert proc.stdout.splitlines() == case["expected"]["stdout_lines"]
    assert proc.stderr == ""


def test_cli_refuses_a_malformed_request_with_exit_two() -> None:
    proc = _cli('{"title": "OMN-1"}')
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "PR_TITLE_CHECK_BAD_REQUEST" in proc.stderr


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
@pytest.mark.parametrize(
    "name",
    [
        "recorded-omniclaude-2649-ticket",
        "recorded-omniclaude-2555-dependabot",
        "recorded-omnibase_infra-refuse",
    ],
)
async def test_golden_chain_over_bus_matches_captured_case(
    tmp_path: Path, name: str
) -> None:
    case = next(c for c in CASES if c["name"] == name)
    runtime = await _run(tmp_path, case["request"])
    result = runtime.handler_result
    assert isinstance(result, ModelPrTitleCheckResult)
    assert list(result.output_lines) == case["expected"]["stdout_lines"]
    assert result.exit_code == case["expected"]["exit_code"]


@pytest.mark.asyncio
async def test_error_chain_over_bus_fails_without_result(tmp_path: Path) -> None:
    # RuntimeLocal injects a run-id string for an absent required str field
    # (OMN-13591), so the bus error chain uses a field the model forbids.
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"title": "OMN-1", "author": "x", "surprise": 1}))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
