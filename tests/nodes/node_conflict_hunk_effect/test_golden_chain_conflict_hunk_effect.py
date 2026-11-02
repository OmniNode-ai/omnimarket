# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: conflict-hunk command wire payload -> contract-bound handler -> resolved event.

The command arrives as JSON on the contract's subscribe topic, the handler is the
class the contract binds, the shared routing-policy helper resolves the command's
routing policy, and the resolved event round-trips through JSON for the contract's
publish topic. Git and the model call are injected fakes, so the chain is
deterministic and touches no network.
"""

from __future__ import annotations

import importlib
import json
import shutil
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

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
REPO = "OmniNode-ai/omnimarket"
CONFLICTED = (
    "def foo():\n<<<<<<< HEAD\n    return 1\n=======\n    return 2\n>>>>>>> topic\n"
)
RESOLVED = "def foo():\n    return 1\n"


def _command_wire(routing_policy: dict[str, Any]) -> str:
    return json.dumps(
        {
            "pr_number": 77,
            "repo": REPO,
            "head_ref_name": "feature/some-work",
            "base_ref_name": "main",
            "conflict_files": ["src/foo.py"],
            "correlation_id": "00000000-0000-4000-8000-000000000077",
            "run_id": "golden-chain-run",
            "routing_policy": routing_policy,
        }
    )


ROUTING_POLICY: dict[str, Any] = {
    "primary": "conflict_resolver_primary",
    "fallback": "conflict_resolver_fallback",
    "fallback_allowed_roles": ["conflict_resolver"],
    "max_tokens": 8192,
}


def _fake_git(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    if "worktree" in cmd and "add" in cmd:
        worktree = Path(cmd[-2])
        (worktree / ".git").mkdir(parents=True, exist_ok=True)
        (worktree / "src").mkdir(exist_ok=True)
        (worktree / "src" / "foo.py").write_text(CONFLICTED, encoding="utf-8")
        return 0, "", ""
    if "worktree" in cmd and "remove" in cmd:
        shutil.rmtree(Path(cmd[-1]), ignore_errors=True)
        return 0, "", ""
    if "diff" in cmd and "--name-only" in cmd:
        return 0, "src/foo.py\n", ""
    if "rev-parse" in cmd:
        return 0, "feedc0de", ""
    return 0, "", ""


def _bound_handler_type() -> type[HandlerConflictHunk]:
    module_name, class_name = CONTRACT["handler_routing"]["default_handler"].split(":")
    bound: type[HandlerConflictHunk] = getattr(
        importlib.import_module(module_name), class_name
    )
    return bound


def test_contract_binds_the_handler_and_the_command_model() -> None:
    assert _bound_handler_type() is HandlerConflictHunk
    binding = CONTRACT["handler"]
    module_name, _, class_name = binding["input_model"].rpartition(".")
    assert getattr(importlib.import_module(module_name), class_name) is (
        ModelConflictHunkCommand
    )
    assert CONTRACT["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.pr-conflict-hunk.v1"
    ]
    assert CONTRACT["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.pr-conflict-resolved.v1"
    ]


@pytest.mark.asyncio
async def test_wire_command_resolves_and_emits_a_round_tripping_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / REPO.replace("/", "__") / ".git").mkdir(parents=True)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    monkeypatch.setenv("ONEX_CONFLICT_SOURCE_CLONE_ROOT", str(tmp_path))
    monkeypatch.setenv("ONEX_CONFLICT_WORKTREE_ROOT", str(worktrees))
    seen_policies: list[dict[str, Any]] = []

    def fake_model(
        file_path: str, hunk_context: str, routing_policy: dict[str, Any]
    ) -> tuple[str, bool]:
        seen_policies.append(routing_policy)
        return RESOLVED, False

    command = ModelConflictHunkCommand.model_validate_json(
        _command_wire(ROUTING_POLICY)
    )
    handler = _bound_handler_type()(llm_call_fn=fake_model, subprocess_run_fn=_fake_git)
    event = await handler.handle(command)

    assert event.success is True
    assert event.resolution_committed is True
    assert event.commit_sha == "feedc0de"
    assert event.resolved_files == ["src/foo.py"]
    assert event.correlation_id == command.correlation_id
    assert [policy["primary"] for policy in seen_policies] == [
        "conflict_resolver_primary"
    ]
    assert ModelConflictResolvedEvent.model_validate_json(event.model_dump_json()) == (
        event
    )


@pytest.mark.asyncio
async def test_error_leg_a_command_with_an_invalid_routing_policy_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_CONFLICT_SOURCE_CLONE_ROOT", str(tmp_path))
    monkeypatch.setenv("ONEX_CONFLICT_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    command = ModelConflictHunkCommand.model_validate_json(_command_wire({}))
    handler = _bound_handler_type()(subprocess_run_fn=_fake_git)

    with pytest.raises(ValueError, match="routing_policy schema invalid"):
        await handler.handle(command)
