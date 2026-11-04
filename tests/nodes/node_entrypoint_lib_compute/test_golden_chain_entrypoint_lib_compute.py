# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entrypoint helper parity, contract bindings, and golden/error bus chains."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import ValidationError

import omnimarket.nodes.node_entrypoint_lib_compute as node_package
from omnimarket.nodes.node_entrypoint_lib_compute.handlers.handler_entrypoint_lib import (
    HandlerEntrypointLib,
)
from omnimarket.nodes.node_entrypoint_lib_compute.models.model_entrypoint_lib import (
    EntrypointLibOperation,
    ModelEntrypointLibRequest,
    ModelEntrypointLibResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.entrypoint-lib-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.entrypoint-lib-completed.v1"
HANDLER = HandlerEntrypointLib()


def ask(**fields: Any) -> ModelEntrypointLibResult:
    return HANDLER.handle(ModelEntrypointLibRequest.model_validate(fields))


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
    assert handler_type is HandlerEntrypointLib
    assert node_package.HandlerEntrypointLib is handler_type
    assert issubclass(node_package.NodeEntrypointLibCompute, NodeCompute)
    for side, model in (
        ("input_model", ModelEntrypointLibRequest),
        ("output_model", ModelEntrypointLibResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
        assert binding[side] == f"{model.__module__}.{model.__name__}"
    entry = metadata["entry_points"]["onex.nodes"]["node_entrypoint_lib_compute"]
    assert importlib.import_module(entry) is node_package


def test_every_operation_is_routed_by_the_contract() -> None:
    contract = dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))
    routed = [item["operation"] for item in contract["handler_routing"]["handlers"]]
    declared = list(get_args(EntrypointLibOperation))
    assert routed == declared
    assert contract["inputs"]["operation"]["enum"] == declared


def test_refuse_prints_two_lines_exits_with_code_and_timing_goes_to_stderr() -> None:
    assert ask(
        operation="refuse",
        code=2,
        rule="env",
        reason="OMNI_HOME is required",
        fix="set OMNI_HOME to the registry root",
        timing_line="TIMING e total=0.1s",
    ).model_dump() == {
        "operation": "refuse",
        "stdout": [
            "REFUSED 2 env: OMNI_HOME is required",
            "FIX: set OMNI_HOME to the registry root",
        ],
        "stderr": ["TIMING e total=0.1s"],
        "exit_code": 2,
        "value": None,
    }


def test_refuse_without_a_started_entrypoint_prints_no_timing() -> None:
    result = ask(operation="refuse", code=75, rule="r", reason="x", fix="y")
    assert result.stderr == []
    assert result.exit_code == 75


@pytest.mark.parametrize("dry_run", [None, ""])
def test_dry_run_unset_or_empty_passes(dry_run: str | None) -> None:
    result = ask(operation="dry_run_guard", onex_dry_run=dry_run)
    assert (result.stdout, result.exit_code) == ([], None)


def test_dry_run_in_a_sandbox_passes() -> None:
    result = ask(
        operation="dry_run_guard",
        onex_dry_run="1",
        registry_root="/sandbox",
        sandbox_marker_exists=True,
    )
    assert result.exit_code is None


@pytest.mark.parametrize(
    ("registry_root", "marker"), [(None, True), ("", True), ("/real", False)]
)
def test_dry_run_outside_a_sandbox_refuses_before_any_read(
    registry_root: str | None, marker: bool
) -> None:
    result = ask(
        operation="dry_run_guard",
        onex_dry_run="1",
        registry_root=registry_root,
        sandbox_marker_exists=marker,
    )
    assert result.exit_code == 2
    assert result.stdout == [
        "REFUSED 2 dry-run-guard: ONEX_DRY_RUN set outside a sandbox registry root",
        "FIX: set OMNI_HOME to a sandbox with .dryrun-sandbox, or unset ONEX_DRY_RUN",
    ]


@pytest.mark.parametrize(
    ("facts", "reason", "fix"),
    [
        (
            {},
            "OMNI_HOME is required",
            "set OMNI_HOME to the registry root",
        ),
        (
            {"registry_root": ""},
            "OMNI_HOME is required",
            "set OMNI_HOME to the registry root",
        ),
        (
            {"registry_root": "/h"},
            "ONEX_LEDGER_PATH is required",
            "set ONEX_LEDGER_PATH to the rolling work ledger",
        ),
        (
            {"registry_root": "/h", "ledger_path": ""},
            "ONEX_LEDGER_PATH is required",
            "set ONEX_LEDGER_PATH to the rolling work ledger",
        ),
    ],
)
def test_require_env_refuses_each_missing_variable_in_order(
    facts: dict[str, str], reason: str, fix: str
) -> None:
    result = ask(operation="require_env", **facts)
    assert result.stdout == [f"REFUSED 2 env: {reason}", f"FIX: {fix}"]
    assert result.exit_code == 2


def test_require_env_passes_when_both_are_set() -> None:
    result = ask(operation="require_env", registry_root="/h", ledger_path="/h/l.md")
    assert (result.stdout, result.exit_code) == ([], None)


def test_detail_dir_override_wins_then_default_under_registry_root() -> None:
    assert (
        ask(operation="detail_dir", registry_root="/h", detail_dir_override="/x").value
        == "/x"
    )
    assert (
        ask(operation="detail_dir", registry_root="/h", detail_dir_override="").value
        == "/h/.claude_scratch/ledger-detail"
    )


@pytest.mark.parametrize(
    ("verb", "override", "expected"),
    [
        ("row", None, "90"),
        ("append-rows", None, "90"),
        ("inbox", None, "60"),
        ("catchup", None, "60"),
        ("row", "5", "5"),
        ("inbox", "5", "5"),
        ("row", "", "90"),
    ],
)
def test_verb_budget(verb: str, override: str | None, expected: str) -> None:
    assert (
        ask(operation="verb_budget", verb=verb, budget_override=override).value
        == expected
    )


def test_progress_lines_match_the_shell_helpers() -> None:
    assert ask(
        operation="start_line", entry_name="ledger-write", detail_path="/d/x.1"
    ).stderr == ["STEP ledger-write detail=/d/x.1"]
    assert ask(operation="start_line", detail_path="/d/x.1").stderr == [
        "STEP entry detail=/d/x.1"
    ]
    assert ask(operation="step_start_line", step="row", budget_seconds="90").stderr == [
        "STEP row start budget=90s"
    ]
    assert ask(operation="step_start_line", step="row").stderr == ["STEP row start"]
    assert ask(
        operation="step_end_line", step="row", rc=124, took_seconds="90.0"
    ).stderr == ["STEP row done rc=124 90.0s"]


def test_timing_line_lists_total_then_each_step() -> None:
    assert (
        ask(
            operation="timing_line",
            entry_name="ledger-write",
            total_seconds="2.5",
            steps=[
                {"name": "row", "seconds": "1.2"},
                {"name": "inbox", "seconds": "0.3"},
            ],
        ).value
        == "TIMING ledger-write total=2.5s row=1.2s inbox=0.3s"
    )
    assert (
        ask(operation="timing_line", total_seconds="0.0").value
        == "TIMING entry total=0.0s"
    )


def test_too_old_is_the_protocol_refusal() -> None:
    result = ask(operation="too_old", detail_path="/d/x.9")
    assert result.exit_code == 2
    assert result.stdout == [
        "REFUSED 2 onex-ledger-protocol: the ledger command returned no expected result; detail=/d/x.9",
        "FIX: read /d/x.9 and repair the reported command or protocol failure before retrying",
    ]


DETAIL = "\n".join(
    [
        "$ onex-ledger row",
        "OK appended row 12",
        "some noise",
        "NOT-APPENDED duplicate id",
        "FIX: pass a new id",
    ]
)


def test_verb_failed_keeps_the_verbs_own_refusal_and_exits_with_its_code() -> None:
    detail = DETAIL + "\nREFUSED 65 rule: no cells\nRETRY lock held"
    result = ask(
        operation="verb_failed",
        rc=65,
        detail_path="/d/x.1",
        detail_text=detail,
        timing_line="TIMING e total=1.0s",
    )
    assert result.stdout == [
        "OK appended row 12",
        "NOT-APPENDED duplicate id",
        "FIX: pass a new id",
        "REFUSED 65 rule: no cells",
        "RETRY lock held",
    ]
    assert result.stderr == ["TIMING e total=1.0s"]
    assert result.exit_code == 65


def test_verb_failed_without_its_own_refusal_adds_the_command_refusal() -> None:
    result = ask(
        operation="verb_failed",
        rc=1,
        detail_path="/d/x.1",
        detail_text=DETAIL,
        verb_error_suffix=": boom",
    )
    assert result.exit_code == 1
    assert result.stdout == [
        "OK appended row 12",
        "NOT-APPENDED duplicate id",
        "FIX: pass a new id",
        "REFUSED 1 onex-ledger-command: the ledger command exited 1: boom; detail=/d/x.1",
        "FIX: read /d/x.1 and repair the reported failure before retrying",
    ]


def test_last_returns_the_final_line_starting_with_the_prefix() -> None:
    text = "OK first\nnoise\nOK second\nFIX: x"
    assert ask(operation="last", detail_text=text, prefix="OK ").value == "OK second"
    assert ask(operation="last", detail_text=text, prefix="NOPE").value is None
    assert ask(operation="last", detail_text=text, prefix="(OK|FIX)").value == "FIX: x"


def test_field_returns_the_first_value_of_the_key() -> None:
    line = "OK line=14 id=a id=b empty= xline=9"
    assert ask(operation="field", line=line, key="line").value == "14"
    assert ask(operation="field", line=line, key="id").value == "a"
    assert ask(operation="field", line=line, key="empty").value == ""
    assert ask(operation="field", line=line, key="missing").value is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "unknown"},
        {"operation": "refuse", "extra": True},
        {"operation": "verb_failed", "rc": "x"},
    ],
)
def test_error_chain_refuses_invalid_requests(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelEntrypointLibRequest.model_validate(payload)


@pytest.mark.parametrize(
    "operation", ["refuse", "last", "field", "step_end_line", "verb_failed"]
)
def test_a_missing_fact_is_a_value_error_not_a_guess(operation: str) -> None:
    with pytest.raises(ValueError, match="requires"):
        ask(operation=operation)


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    request = ModelEntrypointLibRequest(operation="verb_budget", verb="row")
    with pytest.raises(ValidationError, match="frozen_instance"):
        request.verb = "inbox"
    result = HANDLER.handle(request)
    with pytest.raises(ValidationError, match="frozen_instance"):
        result.value = "1"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelEntrypointLibResult.model_validate({"operation": "last", "extra": True})


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
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"operation": "verb_budget", "verb": "append-rows"},
            {
                "operation": "verb_budget",
                "stdout": [],
                "stderr": [],
                "exit_code": None,
                "value": "90",
            },
        ),
        (
            {
                "operation": "require_env",
                "registry_root": "/h",
            },
            {
                "operation": "require_env",
                "stdout": [
                    "REFUSED 2 env: ONEX_LEDGER_PATH is required",
                    "FIX: set ONEX_LEDGER_PATH to the rolling work ledger",
                ],
                "stderr": [],
                "exit_code": 2,
                "value": None,
            },
        ),
    ],
)
async def test_golden_chain_over_bus(
    tmp_path: Path, payload: dict[str, Any], expected: dict[str, Any]
) -> None:
    runtime = await _run(tmp_path, payload)
    result = runtime.handler_result
    assert isinstance(result, ModelEntrypointLibResult)
    assert result.model_dump() == expected
    assert (
        ModelEntrypointLibResult.model_validate_json(result.model_dump_json()) == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_bus_fails_without_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {"operation": "verb_budget", "rc": "x"})
    assert runtime.handler_result is None
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
