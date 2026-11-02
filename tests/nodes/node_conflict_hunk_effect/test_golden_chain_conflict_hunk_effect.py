# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: conflict-hunk command JSON -> HandlerConflictHunk -> resolved event.

The command is read off the wire, its routing_policy is resolved through the
shared routing helper (``omnimarket.handlers.routing.routing_policy_helpers``),
the resolved policy reaches the LLM seam, and the terminal event round-trips as
the payload of the contract's declared publish topic.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_conflict_hunk_effect as node_package
from omnimarket.nodes.node_conflict_hunk_effect.handlers.handler_conflict_hunk import (
    HandlerConflictHunk,
)
from omnimarket.nodes.node_conflict_hunk_effect.models.model_conflict_resolved_event import (
    ModelConflictResolvedEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelConflictHunkCommand,
)
from tests.nodes.node_conflict_hunk_effect.test_handler_conflict_hunk import (
    _RESOLVED_CONTENT,
    ROUTING_POLICY,
    _make_command,
    _make_subprocess_fn,
    _setup_source_clone,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.pr-conflict-hunk.v1"
RESULT_TOPIC = "onex.evt.omnimarket.pr-conflict-resolved.v1"


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_contract_binds_the_handler_input_model_and_topics() -> None:
    contract = _contract()
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert handler_type is HandlerConflictHunk
    module_name, _, class_name = binding["input_model"].rpartition(".")
    assert getattr(importlib.import_module(module_name), class_name) is (
        ModelConflictHunkCommand
    )
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [RESULT_TOPIC]


@pytest.mark.asyncio
async def test_golden_chain_command_json_to_resolved_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_source_clone(tmp_path)
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    monkeypatch.setenv("ONEX_CONFLICT_SOURCE_CLONE_ROOT", str(tmp_path))
    monkeypatch.setenv("ONEX_CONFLICT_WORKTREE_ROOT", str(wt_root))

    seen_policies: list[dict[str, Any]] = []

    def llm_call(
        file_path: str, hunk_context: str, routing_policy: dict[str, Any]
    ) -> tuple[str, bool]:
        _ = file_path, hunk_context
        seen_policies.append(routing_policy)
        return _RESOLVED_CONTENT, False

    wire_command = _make_command().model_dump_json()
    command = ModelConflictHunkCommand.model_validate_json(wire_command)
    handler = HandlerConflictHunk(
        llm_call_fn=llm_call,
        subprocess_run_fn=_make_subprocess_fn(wt_root, commit_sha="0123abcd"),
    )
    event = await handler.handle(command)

    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.success is True
    assert event.resolution_committed is True
    assert event.commit_sha == "0123abcd"
    assert event.correlation_id == command.correlation_id
    assert event.resolved_files == ["src/foo.py"]
    assert len(seen_policies) == 1
    assert seen_policies[0]["primary"] == ROUTING_POLICY["primary"]
    assert seen_policies[0]["fallback"] == ROUTING_POLICY["fallback"]

    wire_event = ModelConflictResolvedEvent.model_validate_json(event.model_dump_json())
    assert wire_event == event
