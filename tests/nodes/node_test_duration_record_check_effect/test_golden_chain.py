# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test duration record check: a typed command through the in-memory runtime (OMN-23666)."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.nodes.node_test_duration_record_check_effect.adapters.policy_file import (
    load_policy,
)
from omnimarket.nodes.node_test_duration_record_check_effect.models.model_test_duration_record_check import (
    ModelTestDurationRecordCheckResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(str(files("omnimarket.nodes.node_test_duration_record_check_effect")))
    / "contract.yaml"
)
NEW_FILE = "tests/unit/test_new_thing.py"


def _checkout(root: Path, record: dict[str, float]) -> None:
    policy = load_policy()
    (root / policy.record_path).parent.mkdir(parents=True, exist_ok=True)
    (root / policy.record_path).write_text(json.dumps(record), encoding="utf-8")
    (root / NEW_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / NEW_FILE).write_text("def test_x():\n    pass\n", encoding="utf-8")


async def _run(
    tmp_path: Path, request_body: dict[str, object]
) -> tuple[RuntimeLocal, EnumWorkflowResult]:
    request = tmp_path / "request.json"
    request.write_text(json.dumps(request_body))
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        input_path=request,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    status = await runtime.run_async()
    return runtime, status


def test_golden_chain_declares_the_command_and_terminal_topics() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.test-duration-record-check-completed.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )


@pytest.mark.asyncio
async def test_golden_chain_admits_a_recorded_added_test_file(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    _checkout(root, {NEW_FILE: 1.0})
    runtime, _status = await _run(
        tmp_path, {"repo_root": str(root), "added_files": [NEW_FILE]}
    )
    result = runtime.handler_result
    assert isinstance(result, ModelTestDurationRecordCheckResult)
    assert result.admitted is True
    assert result.added_test_files == 1


@pytest.mark.asyncio
async def test_golden_chain_refuses_an_unrecorded_added_test_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "checkout"
    _checkout(root, {})
    runtime, _status = await _run(
        tmp_path, {"repo_root": str(root), "added_files": [NEW_FILE]}
    )
    result = runtime.handler_result
    assert isinstance(result, ModelTestDurationRecordCheckResult)
    assert result.admitted is False
    assert result.unrecorded == (NEW_FILE,)


@pytest.mark.asyncio
async def test_error_chain_refuses_an_invalid_request(tmp_path: Path) -> None:
    runtime, status = await _run(
        tmp_path, {"repo_root": str(tmp_path), "unexpected": True}
    )
    assert status is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None


@pytest.mark.asyncio
async def test_error_chain_types_an_unreadable_record(tmp_path: Path) -> None:
    runtime, _status = await _run(
        tmp_path, {"repo_root": str(tmp_path), "added_files": [NEW_FILE]}
    )
    result = runtime.handler_result
    assert isinstance(result, ModelTestDurationRecordCheckResult)
    assert result.exit_code == 2
