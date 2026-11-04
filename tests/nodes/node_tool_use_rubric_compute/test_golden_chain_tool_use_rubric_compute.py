# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tool-use rubric wrapper parity, contract bindings, and golden/error bus chains."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import ValidationError

import omnimarket.nodes.node_tool_use_rubric_compute as node_package
from omnimarket.nodes.node_tool_use_rubric_compute.handlers.handler_tool_use_rubric import (
    HandlerToolUseRubric,
)
from omnimarket.nodes.node_tool_use_rubric_compute.models.model_tool_use_rubric import (
    ModelToolUseRubricRequest,
    ModelToolUseRubricResult,
    ToolUseRubricOperation,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.tool-use-rubric-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.tool-use-rubric-completed.v1"
HANDLER = HandlerToolUseRubric()
ATTEMPT = {
    "outcome": "FAIL",
    "failed_criteria": ["c1", "c2"],
    "undetermined_criteria": ["c3"],
}


def ask(**fields: Any) -> ModelToolUseRubricResult:
    return HANDLER.handle(ModelToolUseRubricRequest.model_validate(fields))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))
    metadata = dict(yaml.safe_load((NODE_DIR / "metadata.yaml").read_text()))
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert handler_type is HandlerToolUseRubric
    assert node_package.HandlerToolUseRubric is handler_type
    assert issubclass(node_package.NodeToolUseRubricCompute, NodeCompute)
    for side, model in (
        ("input_model", ModelToolUseRubricRequest),
        ("output_model", ModelToolUseRubricResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
        assert binding[side] == f"{model.__module__}.{model.__name__}"
    entry = metadata["entry_points"]["onex.nodes"]["node_tool_use_rubric_compute"]
    assert importlib.import_module(entry) is node_package


def test_every_operation_is_routed_by_the_contract() -> None:
    contract = dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))
    routed = [item["operation"] for item in contract["handler_routing"]["handlers"]]
    declared = list(get_args(ToolUseRubricOperation))
    assert routed == declared
    assert contract["inputs"]["operation"]["enum"] == declared


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("on", False),
        ("off", True),
        ("OFF", True),
        ("Off", True),
        ("", False),
    ],
)
def test_disabled_only_for_off_in_any_case(value: str | None, expected: bool) -> None:
    assert ask(operation="disabled", rubric_env=value).flag is expected


def test_scorer_candidates_follow_the_wrapper_order_and_skip_unset_ones() -> None:
    assert ask(
        operation="scorer_candidates",
        scorer_python_env="/a/python",
        lab_work_python_env="/b/python",
        dispatch_venv="/venv/",
        registry_root="/reg",
    ).paths == [
        "/a/python",
        "/b/python",
        "/venv/bin/python",
        "/reg/.onex-dispatch-venv/bin/python",
    ]
    assert ask(operation="scorer_candidates", registry_root="/reg").paths == [
        "/reg/.onex-dispatch-venv/bin/python"
    ]
    assert ask(operation="scorer_candidates", scorer_python_env="").paths == []


def test_scorer_python_is_the_first_candidate_the_caller_found_executable() -> None:
    facts = {
        "scorer_python_env": "/a/python",
        "lab_work_python_env": "/b/python",
        "dispatch_venv": "/venv",
        "registry_root": "/reg",
    }
    assert (
        ask(
            operation="scorer_python",
            executable_paths=["/reg/.onex-dispatch-venv/bin/python", "/b/python"],
            **facts,
        ).text
        == "/b/python"
    )
    assert ask(operation="scorer_python", executable_paths=[], **facts).text is None


def test_scorer_command_drops_pythonpath_and_orders_arguments() -> None:
    result = ask(
        operation="scorer_command",
        scorer_python="/py",
        source="crush",
        args=["--session", "s1"],
        run_ref="run-7",
        out_path="/r/rubric_verdict.json",
        environ={"PYTHONPATH": "/x", "PATH": "/bin", "HOME": "/h"},
    )
    assert result.command == [
        "/py",
        "-m",
        "omnimarket.delegation.rubric.tool_use_score",
        "crush",
        "--session",
        "s1",
        "--run-ref",
        "run-7",
        "--out",
        "/r/rubric_verdict.json",
    ]
    assert result.env == {"PATH": "/bin", "HOME": "/h"}


def test_manifest_lists_readable_files_once_in_order() -> None:
    result = ask(
        operation="manifest",
        ls_returncode=0,
        ls_stdout="b.py\0a.py\0b.py\0dir\0gone.py\0\0",
        entries=[
            {"path": "a.py", "is_file": True, "line_count": 3},
            {"path": "b.py", "is_file": True, "line_count": 0},
            {"path": "dir", "is_file": False},
            {"path": "gone.py", "is_file": True, "line_count": None},
        ],
    )
    assert result.rows == [
        {"path": "b.py", "line_count": 0},
        {"path": "a.py", "line_count": 3},
    ]


def test_manifest_of_a_failed_listing_is_empty() -> None:
    assert ask(operation="manifest", ls_returncode=128, ls_stdout="a.py\0").rows == []


def test_summarize_disabled_records_why_and_asks_for_the_verdict_file() -> None:
    result = ask(operation="summarize", rubric_env="off", out_path="/r/v.json")
    assert result.summary is not None
    assert result.summary["outcome"] == "UNDETERMINED"
    assert result.summary["scored"] is False
    assert result.summary["reason"] == "disabled"
    assert result.summary["detail"] == "ONEX_TOOL_USE_RUBRIC=off"
    assert result.summary["record"] == "/r/v.json"
    assert result.verdict_file is not None
    assert result.verdict_file["schema"] == "tool-use-rubric-verdict.v1"
    not_scored = result.verdict_file["not_scored"]
    assert isinstance(not_scored, dict)
    assert "record" not in not_scored


def test_summarize_without_an_interpreter() -> None:
    result = ask(operation="summarize", out_path="/r/v.json", scorer_python=None)
    assert result.summary is not None
    assert result.summary["reason"] == "scorer_unavailable"
    assert (
        result.summary["detail"]
        == "no interpreter with omnimarket: set ONEX_TOOL_USE_SCORER_PYTHON"
    )


def test_summarize_a_run_error_and_a_failed_scorer() -> None:
    errored = ask(
        operation="summarize",
        out_path="/r/v.json",
        scorer_python="/py",
        run_error="timed out after 120 seconds",
    )
    assert errored.summary is not None
    assert (errored.summary["reason"], errored.summary["detail"]) == (
        "scorer_failed",
        "timed out after 120 seconds",
    )
    failed = ask(
        operation="summarize",
        out_path="/r/v.json",
        scorer_python="/py",
        returncode=1,
        stderr="  Traceback\n",
        record={"attempt_verdict": ATTEMPT},
    )
    assert failed.summary is not None
    assert failed.summary["detail"] == "rc=1 Traceback"
    assert failed.verdict_file is not None


@pytest.mark.parametrize("record", [None, {"measured": {}}, {"attempt_verdict": None}])
def test_summarize_an_unreadable_or_incomplete_record_is_scorer_failed(
    record: dict[str, Any] | None,
) -> None:
    result = ask(
        operation="summarize",
        out_path="/r/v.json",
        scorer_python="/py",
        returncode=0,
        record=record,
    )
    assert result.summary is not None
    assert result.summary["reason"] == "scorer_failed"
    assert result.summary["detail"] == "rc=0 "


def test_summarize_long_detail_keeps_the_last_600_characters() -> None:
    result = ask(
        operation="summarize",
        out_path="/r/v.json",
        scorer_python="/py",
        run_error="x" * 700 + "END",
    )
    assert result.summary is not None
    detail = result.summary["detail"]
    assert isinstance(detail, str)
    assert len(detail) == 600
    assert detail.endswith("END")


def test_summarize_a_scored_run_leaves_the_scorers_own_file() -> None:
    result = ask(
        operation="summarize",
        out_path="/r/v.json",
        scorer_python="/py",
        returncode=0,
        record={"attempt_verdict": ATTEMPT, "measured": {"tool_calls": 4}},
    )
    assert result.verdict_file is None
    assert result.summary == {
        "outcome": "FAIL",
        "scored": True,
        "failed_criteria": ["c1", "c2"],
        "undetermined_criteria": ["c3"],
        "measured": {"tool_calls": 4},
        "attempt_verdict": ATTEMPT,
        "record": "/r/v.json",
    }


def test_summary_line_for_scored_and_unscored_runs() -> None:
    scored = ask(
        operation="summarize",
        out_path="/r/v.json",
        scorer_python="/py",
        returncode=0,
        record={"attempt_verdict": ATTEMPT},
    ).summary
    assert (
        ask(operation="summary_line", prefix="RUBRIC", summary=scored).text
        == "RUBRIC outcome=FAIL failed=c1,c2 record=/r/v.json"
    )
    unscored = ask(
        operation="summarize", rubric_env="off", out_path="/r/v.json"
    ).summary
    assert (
        ask(operation="summary_line", prefix="RUBRIC", summary=unscored).text
        == "RUBRIC outcome=UNDETERMINED failed=- reason=disabled record=/r/v.json"
    )


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("2.1.5 (Claude Code)\n", "2.1.5"), ("", "unknown"), ("  \n", "unknown")],
)
def test_claude_version(stdout: str, expected: str) -> None:
    assert ask(operation="claude_version", version_stdout=stdout).text == expected


def test_schema_cache_target_keys_on_version_and_flags() -> None:
    key = hashlib.sha256(json.dumps(["2.1.5", ["--bare"]]).encode()).hexdigest()[:16]
    assert (
        ask(
            operation="schema_cache_target",
            version="2.1.5",
            flags=["--bare"],
            xdg_cache_home="/xdg",
        ).text
        == f"/xdg/omni/tool-schemas/claude-code-2.1.5-{key}.json"
    )
    assert (
        ask(
            operation="schema_cache_target",
            version="2.1.5",
            flags=["--bare"],
            home="/users/u",
        ).text
        == f"/users/u/.cache/omni/tool-schemas/claude-code-2.1.5-{key}.json"
    )
    assert (
        ask(
            operation="schema_cache_target",
            version="2.1.5",
            flags=["--bare"],
            cache_dir="/c",
            xdg_cache_home="/xdg",
        ).text
        == f"/c/claude-code-2.1.5-{key}.json"
    )
    other = ask(
        operation="schema_cache_target", version="2.1.5", flags=[], cache_dir="/c"
    ).text
    assert other is not None
    assert other != f"/c/claude-code-2.1.5-{key}.json"


def test_pick_captured_tools_takes_the_largest_first_on_a_tie() -> None:
    result = ask(
        operation="pick_captured_tools",
        captured_bodies=[
            {"tools": ["a"]},
            {"model": "x"},
            {"tools": ["b", "c"]},
            {"tools": ["d", "e"]},
        ],
    )
    assert result.tools == ["b", "c"]
    assert ask(operation="pick_captured_tools").tools is None


@pytest.mark.parametrize(
    "operation", ["scorer_command", "summary_line", "schema_cache_target", "summarize"]
)
def test_a_missing_fact_is_a_value_error_not_a_guess(operation: str) -> None:
    with pytest.raises(ValueError, match="requires"):
        ask(operation=operation)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "unknown"},
        {"operation": "disabled", "extra": True},
        {"operation": "scorer_command", "args": [1]},
    ],
)
def test_error_chain_refuses_invalid_requests(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelToolUseRubricRequest.model_validate(payload)


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    request = ModelToolUseRubricRequest(operation="disabled")
    with pytest.raises(ValidationError, match="frozen_instance"):
        request.rubric_env = "off"
    result = HANDLER.handle(request)
    with pytest.raises(ValidationError, match="frozen_instance"):
        result.flag = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelToolUseRubricResult.model_validate({"operation": "disabled", "extra": 1})


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "input.json"
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
async def test_golden_chain_over_bus(tmp_path: Path) -> None:
    runtime = await _run(
        tmp_path,
        {
            "operation": "summarize",
            "rubric_env": "off",
            "out_path": "/r/v.json",
        },
    )
    result = runtime.handler_result
    assert isinstance(result, ModelToolUseRubricResult)
    assert result.summary is not None
    assert result.summary["reason"] == "disabled"
    assert result.verdict_file is not None
    assert (
        ModelToolUseRubricResult.model_validate_json(result.model_dump_json()) == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_bus_fails_without_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {"operation": "disabled", "args": [5]})
    assert runtime.handler_result is None
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
