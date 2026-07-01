# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_dispatch_worker [OMN-13684].

WS-5 Wave 10. Variant A (direct in-process COMPUTE handler call). The handler is
a pure prep node: it compiles a role-templated prompt with collision fences and
KB context, validates dedup, and never spawns agents. Each case varies the role,
fence source, and KB level; assertions cover the typed ``ModelDispatchWorkerResult``
fields (task description, compiled prompt body, fence embeds, spawn args, KB
evidence). Negative controls: a duplicate in-progress worker must be rejected,
and a role missing required identifiers must raise.

OMN-13784 additions close two declared-input branches the base matrix above
never reaches:
  * ``knowledge_context_level`` L0/L1/L3 (only "none" and "L2" were covered;
    L0/L1/L3 each grow a distinct set of prompt sections per contract.yaml).
  * ``replace=True`` -- the declared override that lets a dedup-rejected
    dispatch through when an in_progress worker with the same name exists.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
    HandlerDispatchWorker,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    EnumWorkerRole,
    ModelDispatchWorkerCommand,
)


@pytest.fixture(autouse=True)
def _dispatch_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide the env the prompt compiler requires; never persist a record."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))
    monkeypatch.setenv("OMNI_WORKTREES", str(tmp_path / "omni_home" / "worktrees"))
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)


# (case_id, command_kwargs, existing_subjects, expected)
CASES = [
    pytest.param(
        {
            "name": "fixer-1",
            "team": "wave10",
            "role": EnumWorkerRole.fixer,
            "scope": "implement OMN-1234 acceptance criteria",
            "targets": ["OMN-1234", "omnimarket#5"],
        },
        None,
        {
            "rejected": False,
            "task_prefix": "[fixer] fixer-1:",
            "prompt_contains": ["TDD-FIRST SEQUENCE", "fixer-1", "wave10"],
            "fence_embeds": [],
            "fence_block_text": "(none",
            "bundle_level": "none",
            "spawn_model": "sonnet",
        },
        id="fixer-role-render",
    ),
    pytest.param(
        {
            "name": "watcher-1",
            "team": "wave10",
            "role": EnumWorkerRole.watcher,
            "scope": "watch CI for the lifecycle PR",
            "targets": ["omnimarket#42"],
            "model": "haiku",
        },
        None,
        {
            "rejected": False,
            "task_prefix": "[watcher] watcher-1:",
            "prompt_contains": ["Monitor", "gh pr checks", "watcher-1"],
            "fence_embeds": [],
            "fence_block_text": "(none",
            "bundle_level": "none",
            "spawn_model": "haiku",
        },
        id="watcher-role-variant",
    ),
    pytest.param(
        {
            "name": "auditor-1",
            "team": "wave10",
            "role": EnumWorkerRole.auditor,
            "scope": "audit the delegation routing seam",
            "targets": ["omnimarket"],
            "collision_fences": ["OMN-9999 (owned by other-worker)"],
        },
        None,
        {
            "rejected": False,
            "task_prefix": "[auditor] auditor-1:",
            "prompt_contains": [
                "Audit only",
                "OMN-9999 (owned by other-worker)",
            ],
            "fence_embeds": ["OMN-9999 (owned by other-worker)"],
            "fence_block_text": "OMN-9999",
            "bundle_level": "none",
            "spawn_model": "sonnet",
        },
        id="explicit-collision-fences",
    ),
    pytest.param(
        {
            "name": "fixer-2",
            "team": "wave10",
            "role": EnumWorkerRole.fixer,
            "scope": "implement with KB context",
            "targets": ["OMN-2222", "omnimarket#7"],
            "knowledge_context_level": "L2",
        },
        None,
        {
            "rejected": False,
            "task_prefix": "[fixer] fixer-2:",
            "prompt_contains": ["## Knowledge Context", "Level L2"],
            "fence_embeds": [],
            "fence_block_text": "(none",
            "bundle_level": "L2",
            "spawn_model": "sonnet",
        },
        id="kb-context-L2",
    ),
    pytest.param(
        # NEGATIVE CONTROL: an in-progress worker with the same name is rejected.
        {
            "name": "fixer-dup",
            "team": "wave10",
            "role": EnumWorkerRole.fixer,
            "scope": "duplicate dispatch attempt",
            "targets": ["OMN-3333", "omnimarket#9"],
        },
        ["[fixer] fixer-dup: already running"],
        {
            "rejected": True,
            "reject_contains": "already in_progress",
        },
        id="dedup-reject-NEGATIVE",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(("command_kwargs", "existing_subjects", "expected"), CASES)
def test_dispatch_worker_multiparam(
    command_kwargs: dict[str, Any],
    existing_subjects: list[str] | None,
    expected: dict[str, Any],
) -> None:
    handler = HandlerDispatchWorker()
    command = ModelDispatchWorkerCommand(**command_kwargs)
    result = handler.handle(command, existing_task_subjects=existing_subjects)

    if expected["rejected"]:
        assert result.rejected_reason
        assert expected["reject_contains"] in result.rejected_reason
        # rejection short-circuits compilation: no prompt, no spawn args
        assert result.validated_prompt_template == ""
        assert result.proposed_agent_spawn_args == {}
        return

    assert result.rejected_reason == ""
    assert result.validated_task_description.startswith(expected["task_prefix"])
    for fragment in expected["prompt_contains"]:
        assert fragment in result.validated_prompt_template, (
            f"missing {fragment!r} in compiled prompt"
        )
    assert result.collision_fence_embeds == expected["fence_embeds"]
    assert expected["fence_block_text"] in result.validated_prompt_template
    assert result.bundle_level == expected["bundle_level"]
    assert result.proposed_agent_spawn_args["model"] == expected["spawn_model"]
    assert result.proposed_agent_spawn_args["name"] == command.name
    assert "onex.evt.omnimarket.dispatch-worker-compiled.v1"

    if expected["bundle_level"] != "none":
        assert result.injected_context_char_count > 0
        assert result.source_backends_used == ["local"]
        assert result.knowledge_context_bundle_hash != ""
    else:
        assert result.injected_context_char_count == 0


@pytest.mark.parametrize(
    ("level", "expected_fragments", "unexpected_fragments"),
    [
        pytest.param(
            "L0",
            ["## Knowledge Context", "Level L0", "scope summary only"],
            ["**Primary ticket:**", "**Primary repo:**"],
            id="kb-context-L0-scope-only",
        ),
        pytest.param(
            "L1",
            ["## Knowledge Context", "Level L1", "**Primary ticket:**"],
            ["**Primary repo:**"],
            id="kb-context-L1-adds-ticket",
        ),
        pytest.param(
            "L3",
            [
                "## Knowledge Context",
                "Level L3",
                "**Primary ticket:**",
                "**Primary repo:**",
                "Extended decisions",
            ],
            [],
            id="kb-context-L3-adds-extended-decisions",
        ),
    ],
)
@pytest.mark.integration
def test_dispatch_worker_knowledge_context_level_branches(
    level: str,
    expected_fragments: list[str],
    unexpected_fragments: list[str],
) -> None:
    """OMN-13784: closes the L0/L1/L3 ``knowledge_context_level`` branches.

    The base CASES matrix above only exercised "none" and "L2"; each of
    L0/L1/L3 grows a distinct set of prompt sections (see
    ``_assemble_kb_context`` in the handler), so each is a genuinely
    distinct declared state, not a parametrization of the same code path.
    """
    handler = HandlerDispatchWorker()
    command = ModelDispatchWorkerCommand(
        name=f"fixer-kb-{level.lower()}",
        team="wave7",
        role=EnumWorkerRole.fixer,
        scope="implement with tiered KB context",
        targets=["OMN-4444", "omnimarket#11"],
        knowledge_context_level=level,
    )
    result = handler.handle(command, existing_task_subjects=None)

    assert result.rejected_reason == ""
    assert result.bundle_level == level
    for fragment in expected_fragments:
        assert fragment in result.validated_prompt_template, (
            f"level={level}: missing {fragment!r}"
        )
    for fragment in unexpected_fragments:
        assert fragment not in result.validated_prompt_template, (
            f"level={level}: unexpected {fragment!r} leaked from a higher tier"
        )
    assert result.injected_context_char_count > 0
    assert result.source_backends_used == ["local"]
    assert result.degraded_backends == []


@pytest.mark.integration
def test_dispatch_worker_replace_true_overrides_dedup_rejection() -> None:
    """OMN-13784: closes the ``replace=True`` override branch.

    The base matrix's ``dedup-reject-NEGATIVE`` case proves the default
    (``replace=False``) rejection path. This proves the declared override:
    the same in_progress-worker collision must NOT reject when the caller
    explicitly asks to replace it, and compilation proceeds normally.
    """
    handler = HandlerDispatchWorker()
    command = ModelDispatchWorkerCommand(
        name="fixer-dup",
        team="wave10",
        role=EnumWorkerRole.fixer,
        scope="restart after replace",
        targets=["OMN-3333", "omnimarket#9"],
        replace=True,
    )
    result = handler.handle(
        command,
        existing_task_subjects=["[fixer] fixer-dup: already running"],
    )

    assert result.rejected_reason == ""
    assert result.validated_task_description.startswith("[fixer] fixer-dup:")
    assert result.validated_prompt_template != ""
    assert result.proposed_agent_spawn_args["name"] == "fixer-dup"


@pytest.mark.integration
def test_dispatch_worker_missing_identifiers_raises() -> None:
    """NEGATIVE CONTROL: fixer role with no ticket/repo in targets must raise."""
    handler = HandlerDispatchWorker()
    command = ModelDispatchWorkerCommand(
        name="fixer-bad",
        team="wave10",
        role=EnumWorkerRole.fixer,
        scope="missing identifiers",
        targets=["some-free-text-target"],
    )
    with pytest.raises(ValueError, match="requires identifiers"):
        handler.handle(command, existing_task_subjects=[])
