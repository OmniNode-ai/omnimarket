# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20686: what the skill executor decides from an argument text, contract to bus to typed result.

Parity: tests/fixtures/skill_exec_parity.json holds cases captured by running the retired
skill_exec.py's execute() end to end (a fake process that prints a recorded stdout and stderr, a
fake session.env, fake files, a fixed clock), beside the request a caller would send for the same
argument text. Driving the node the way the executor drives it (plan, answer the facts it asks for,
run, report) must give the same exit code, the same output byte for byte, the same command, the
same files written, the same environment for the command and the same time budget. Golden chain:
the request through the registered contract on the in-memory bus. Error chain: a malformed request
is refused with no result.
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

import omnimarket.nodes.node_skill_exec_decision_compute as node_package
from omnimarket.nodes.node_skill_exec_decision_compute.handlers.handler_skill_exec_decision import (
    HandlerSkillExecDecision,
)
from omnimarket.nodes.node_skill_exec_decision_compute.models.model_skill_exec_decision import (
    ModelSkillExecRequest,
    ModelSkillExecResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.skill-exec-decision-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.skill-exec-decision-completed.v1"
PARITY = json.loads((ROOT / "tests/fixtures/skill_exec_parity.json").read_text())
FACTS = PARITY["facts"]
HANDLER = HandlerSkillExecDecision()
ENV_KEYS = (
    "ONEX_DRY_RUN",
    "OMNI_HOME",
    "ONEX_LANE_ACTOR",
    "ONEX_LANE_MODEL",
    "SESSION_LANE",
    "OMNI_SKILL_EXEC_BUDGET_S",
)


def ask(**fields: Any) -> ModelSkillExecResult:
    return HANDLER.handle(ModelSkillExecRequest.model_validate(fields))


def drive(case: dict[str, Any]) -> dict[str, Any]:
    """Run the node the way the executor does: plan, answer what it asks, run, report."""
    request = dict(case["request"], phase="plan")
    session_env: dict[str, str] | None = None
    session_error: str | None = None
    texts: dict[str, str | None] = {}
    exists: dict[str, bool] = {}
    plan = ask(**request)
    for _ in range(8):
        if plan.status != "need":
            break
        if plan.need_session_env is not None:
            found = FACTS["session_envs"][plan.need_session_env]
            if "error" in found:
                session_error = found["error"]
            else:
                session_env = found
        for path in plan.need_texts:
            texts[path] = FACTS["texts"].get(path)
        for path in plan.need_exists:
            exists[path] = path in FACTS["exists"]
        plan = ask(
            **request,
            session_env=session_env,
            session_env_error=session_error,
            texts=texts,
            exists=exists,
        )
    assert plan.status != "need", "the plan kept asking for facts"
    if plan.status == "refused":
        return {
            "plan": plan,
            "exit_code": plan.exit_code,
            "output": plan.output,
            "writes": plan.write_files,
        }
    assert plan.status == "command", plan
    env = {**case["request"]["env"], **plan.env_updates}
    done = ask(
        phase="report",
        skill=case["request"]["skill"],
        argument_text=case["request"]["argument_text"],
        command=plan.command,
        returncode=case["run"]["returncode"],
        stdout=case["run"]["stdout"],
        stderr=case["run"]["stderr"],
        timed_out=case["run"]["timed_out"],
        elapsed_s=case["elapsed_s"],
        env={k: v for k, v in env.items() if k in ENV_KEYS},
    )
    assert done.status == "done", done
    return {
        "plan": plan,
        "env": env,
        "exit_code": done.exit_code,
        "output": done.output,
        "writes": plan.write_files,
    }


@pytest.mark.parametrize("case", PARITY["cases"], ids=lambda c: str(c["name"]))
def test_old_behavior_parity(case: dict[str, Any]) -> None:
    got = drive(case)
    assert got["output"] == case["expected"]["output"]
    assert got["exit_code"] == case["expected"]["exit_code"]
    ran = case["ran"]
    if ran is None:
        assert got["writes"] == case.get("expected_writes", {})
        return
    plan = got["plan"]
    assert plan.command == ran["command"]
    assert got["writes"] == ran["write_files"]
    assert {k: v for k, v in got["env"].items() if k in ran["env"]} == ran["env"]
    assert set(ran["env"]) <= set(got["env"])
    assert plan.budget_s == ran["timeout"]


def test_parity_fixture_exercises_every_outcome() -> None:
    cases = PARITY["cases"]
    codes = {c["expected"]["exit_code"] for c in cases}
    assert codes >= {0, 1, 2, 3, 64, 65, 124}
    assert any(c["ran"] is None for c in cases)
    assert any(c["ran"] for c in cases)
    outputs = "\n".join(c["expected"]["output"] for c in cases)
    for needle in (
        "CITE-AS: ",
        "EXAMPLE (your own arguments in the accepted shape",
        "MISSING-CELL: worktree",
        "REFUSED 124 budget: ",
        "REFUSED 2 dry-run-guard: ",
        "FIX: quote the header as:",
        "TIMING skill_exec total=",
        "STDERR: ",
        "per stamp: --amends if the ruling changes it",
    ):
        assert needle in outputs, needle
    skills = {c["request"]["skill"] for c in cases}
    assert skills >= {
        "ledger-write",
        "ledger-msg",
        "operator-ruling",
        "friction-record",
    }


def test_a_plan_asks_for_the_session_file_before_it_decides() -> None:
    asked = ask(
        phase="plan",
        skill="ledger-write",
        argument_text="MSG --lane L --session-env /ok/session.env",
        args_file="/tmp/a/args.txt",
        plugin_dir="/p",
        python="/usr/bin/python3",
    )
    assert asked.status == "need"
    assert asked.need_session_env == "/ok/session.env"
    assert asked.command == []


def test_a_header_text_file_is_asked_for_and_never_guessed() -> None:
    asked = ask(
        phase="plan",
        skill="ledger-write",
        argument_text="MSG --lane L --text-file /t/ok.txt --parent P",
        args_file="/tmp/a/args.txt",
        plugin_dir="/p",
        python="/usr/bin/python3",
    )
    assert asked.status == "need"
    assert asked.need_texts == ["/t/ok.txt"]
    refused = ask(
        phase="plan",
        skill="ledger-write",
        argument_text="MSG --lane L --text-file /t/gone.txt --parent P",
        args_file="/tmp/a/args.txt",
        plugin_dir="/p",
        python="/usr/bin/python3",
        texts={"/t/gone.txt": None},
    )
    assert (refused.status, refused.exit_code) == ("refused", 64)
    assert "could not be read as UTF-8" in refused.output


@pytest.mark.parametrize("name", sorted(FACTS["usage"]))
def test_usage_matches_the_retired_usage_command(name: str) -> None:
    recorded = FACTS["usage"][name]
    result = ask(phase="usage", usage_skills=recorded["skills"])
    assert result.status == "usage"
    assert result.exit_code == recorded["exit_code"]
    assert result.output == recorded["stdout"]
    assert result.stderr == recorded["stderr"]


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
    assert issubclass(node_package.NodeSkillExecDecisionCompute, handler_type)
    for side, model in (
        ("input_model", ModelSkillExecRequest),
        ("output_model", ModelSkillExecResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_skill_exec_decision_compute"].load() is node_package


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "phase"),
        ({"phase": "sideways"}, "phase"),
        ({"phase": "plan", "elapsed_s": -1}, "greater"),
        ({"phase": "plan", "surprise": 1}, "surprise|Extra"),
    ],
)
def test_error_chain_refuses_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelSkillExecRequest.model_validate(payload)


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
async def test_golden_chain_over_the_bus_plans_a_captured_case(tmp_path: Path) -> None:
    case = next(
        c
        for c in PARITY["cases"]
        if c["ran"]
        and c["expected"]["exit_code"] == 0
        and "texts" not in c["request"]
        and not any(
            w in c["request"]["argument_text"] for w in ("--session-env", "--text-file")
        )
    )
    runtime = await _run(tmp_path, dict(case["request"], phase="plan"))
    result = runtime.handler_result
    assert isinstance(result, ModelSkillExecResult)
    assert result.status == "command"
    assert result.command == case["ran"]["command"]
    assert result.write_files == case["ran"]["write_files"]
    assert ModelSkillExecResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_refuses_a_bad_argument_text(
    tmp_path: Path,
) -> None:
    case = next(
        c
        for c in PARITY["cases"]
        if c["ran"] is None and c["expected"]["exit_code"] == 64
    )
    runtime = await _run(tmp_path, dict(case["request"], phase="plan"))
    result = runtime.handler_result
    assert isinstance(result, ModelSkillExecResult)
    assert (result.status, result.exit_code) == ("refused", 64)
    assert result.output == case["expected"]["output"]


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {})
    assert runtime.handler_result is None
    (tmp_path / "state2").mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"phase": "sideways"}))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "state2",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
