# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger reconcile command parity, contract bindings, and golden/error bus chains."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import ValidationError

import omnimarket.nodes.node_ledger_reconcile_command_compute as node_package
from omnimarket.nodes.node_ledger_reconcile_command_compute.handlers.handler_ledger_reconcile_command import (
    HandlerLedgerReconcileCommand,
)
from omnimarket.nodes.node_ledger_reconcile_command_compute.models.model_ledger_reconcile_command import (
    ModelLedgerReconcileCommandRequest,
    ModelLedgerReconcileCommandResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.ledger-reconcile-command-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.ledger-reconcile-command-completed.v1"
HANDLER = HandlerLedgerReconcileCommand()


def ask(**fields: Any) -> ModelLedgerReconcileCommandResult:
    return HANDLER.handle(ModelLedgerReconcileCommandRequest.model_validate(fields))


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
    assert handler_type is HandlerLedgerReconcileCommand
    assert node_package.HandlerLedgerReconcileCommand is handler_type
    assert issubclass(node_package.NodeLedgerReconcileCommandCompute, NodeCompute)
    for side, model in (
        ("input_model", ModelLedgerReconcileCommandRequest),
        ("output_model", ModelLedgerReconcileCommandResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
        assert binding[side] == f"{model.__module__}.{model.__name__}"
    entry = metadata["entry_points"]["onex.nodes"][
        "node_ledger_reconcile_command_compute"
    ]
    assert importlib.import_module(entry) is node_package


@pytest.mark.parametrize("cache", [None, ""])
def test_unset_or_empty_cache_uses_supplied_tempdir(cache: str | None) -> None:
    assert ask(
        project_path="/checkout/project",
        uv_path="/tools/uv",
        uv_cache_dir=cache,
        tempdir_path="/temp//root/.",
        argv=["dashboard", "--format", "json", "a b", ""],
    ).model_dump() == {
        "operation": "resolve",
        "command": [
            "/tools/uv",
            "run",
            "--project",
            "/checkout/project",
            "onex-ledger-reconcile",
            "dashboard",
            "--format",
            "json",
            "a b",
            "",
        ],
        "env_overrides": {"UV_CACHE_DIR": "/temp/root/onex-ledger-reconcile-uv-cache"},
        "exit_code": 0,
        "message": None,
    }


@pytest.mark.parametrize("cache", ["/cache/existing", " ", "relative/cache"])
def test_nonempty_cache_is_preserved_without_override(cache: str) -> None:
    assert ask(
        project_path="/checkout//project/.",
        uv_path="/tools/uv",
        uv_cache_dir=cache,
        tempdir_path="/unused",
        argv=[],
    ).model_dump() == {
        "operation": "resolve",
        "command": [
            "/tools/uv",
            "run",
            "--project",
            "/checkout/project",
            "onex-ledger-reconcile",
        ],
        "env_overrides": {},
        "exit_code": 0,
        "message": None,
    }


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            "onex-ledger resolver: no valid clone; onex-ledger resolver",
            "onex-ledger-reconcile resolver: no valid clone; onex-ledger-reconcile resolver",
        ),
        ("cannot load resolver.py", "cannot load resolver.py"),
        ("", ""),
    ],
)
def test_resolution_failure_precedes_missing_uv_and_rewrites_message(
    error: str, expected: str
) -> None:
    assert ask(
        resolve_error=error, uv_path=None, tempdir_path="/temp"
    ).model_dump() == {
        "operation": "resolve",
        "command": [],
        "env_overrides": {},
        "exit_code": 2,
        "message": expected,
    }


def test_missing_uv_is_refused_before_cache_setup() -> None:
    assert ask(
        project_path="/checkout/project", uv_path=None, tempdir_path="/temp"
    ).model_dump() == {
        "operation": "resolve",
        "command": [],
        "env_overrides": {},
        "exit_code": 127,
        "message": "onex-ledger-reconcile resolver: uv is not available on PATH",
    }


def test_unknown_flags_are_passed_through_for_the_command_to_judge() -> None:
    assert ask(
        project_path="/checkout/project",
        uv_path="uv",
        tempdir_path="/temp",
        argv=["undeclared", "--unknown"],
    ).command == [
        "uv",
        "run",
        "--project",
        "/checkout/project",
        "onex-ledger-reconcile",
        "undeclared",
        "--unknown",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"uv_path": None, "tempdir_path": "/temp"},
        {
            "project_path": "/checkout",
            "uv_path": "uv",
            "tempdir_path": "/temp",
            "argv": [1],
        },
        {
            "project_path": "/checkout",
            "uv_path": "uv",
            "tempdir_path": "/temp",
            "extra": True,
        },
    ],
)
def test_error_chain_refuses_invalid_requests(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelLedgerReconcileCommandRequest.model_validate(payload)


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    request = ModelLedgerReconcileCommandRequest(
        project_path="/checkout", uv_path="uv", tempdir_path="/temp"
    )
    with pytest.raises(ValidationError, match="frozen_instance"):
        request.uv_path = "changed"
    result = HANDLER.handle(request)
    with pytest.raises(ValidationError, match="frozen_instance"):
        result.exit_code = 2
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelLedgerReconcileCommandResult.model_validate({"extra": True})


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
            {
                "operation": "resolve",
                "project_path": "/checkout/project",
                "uv_path": "/tools/uv",
                "tempdir_path": "/temp",
                "argv": ["onboarding"],
            },
            {
                "operation": "resolve",
                "command": [
                    "/tools/uv",
                    "run",
                    "--project",
                    "/checkout/project",
                    "onex-ledger-reconcile",
                    "onboarding",
                ],
                "env_overrides": {
                    "UV_CACHE_DIR": "/temp/onex-ledger-reconcile-uv-cache"
                },
                "exit_code": 0,
                "message": None,
            },
        ),
        (
            {
                "resolve_error": "onex-ledger resolver: no valid clone",
                "uv_path": None,
                "tempdir_path": "/temp",
            },
            {
                "operation": "resolve",
                "command": [],
                "env_overrides": {},
                "exit_code": 2,
                "message": "onex-ledger-reconcile resolver: no valid clone",
            },
        ),
        (
            {
                "project_path": "/checkout/project",
                "uv_path": None,
                "tempdir_path": "/temp",
            },
            {
                "operation": "resolve",
                "command": [],
                "env_overrides": {},
                "exit_code": 127,
                "message": "onex-ledger-reconcile resolver: uv is not available on PATH",
            },
        ),
    ],
)
async def test_golden_chain_over_bus(
    tmp_path: Path, payload: dict[str, Any], expected: dict[str, Any]
) -> None:
    runtime = await _run(tmp_path, payload)
    result = runtime.handler_result
    assert isinstance(result, ModelLedgerReconcileCommandResult)
    assert result.model_dump() == expected
    assert (
        ModelLedgerReconcileCommandResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_bus_fails_without_result(tmp_path: Path) -> None:
    runtime = await _run(
        tmp_path,
        {
            "project_path": "/checkout",
            "uv_path": "uv",
            "tempdir_path": "/temp",
            "argv": [5],
        },
    )
    assert runtime.handler_result is None
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
