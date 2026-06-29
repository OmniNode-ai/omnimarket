# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_dispatch_queue_drainer [OMN-13684].

WS-5 Wave 10. Variant A (direct in-process COMPUTE handler call). The drainer
reads one legacy queue YAML item and compiles it through node_dispatch_worker.
The dispatch-worker collaborator is injected (the ``_MockDispatchWorker`` pattern)
so the real selection / validation / blocking / artifact-write logic runs while
the worker boundary stays deterministic — NEVER monkeypatches subprocess. All
filesystem roots (queue dir, state dir, omni_home) live under ``tmp_path``.

Each case asserts the typed ``ModelDispatchQueueDrainerResult.status`` and the
written terminal artifact. Negative control: an item whose repo does not exist
under omni_home must produce a ``blocked`` result with a real reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dispatch_queue_drainer.handlers.handler_dispatch_queue_drainer import (
    HandlerDispatchQueueDrainer,
)
from omnimarket.nodes.node_dispatch_queue_drainer.models import (
    ModelDispatchQueueDrainerResult,
)
from omnimarket.nodes.node_dispatch_worker import (
    ModelDispatchWorkerCommand,
    ModelDispatchWorkerResult,
)


class _MockDispatchWorker:
    """Injected dispatch-worker boundary with a configurable rejection."""

    def __init__(self, rejected_reason: str = "") -> None:
        self._rejected_reason = rejected_reason
        self.calls: list[ModelDispatchWorkerCommand] = []

    def handle(
        self, command: ModelDispatchWorkerCommand, **kwargs: Any
    ) -> ModelDispatchWorkerResult:
        self.calls.append(command)
        return ModelDispatchWorkerResult(
            validated_task_description=f"[{command.role}] {command.name}: {command.scope}",
            validated_prompt_template=""
            if self._rejected_reason
            else "COMPILED PROMPT",
            proposed_agent_spawn_args={}
            if self._rejected_reason
            else {"name": command.name, "model": command.model},
            collision_fence_embeds=[],
            rejected_reason=self._rejected_reason,
        )


def _valid_item(repo: str = "omniclaude") -> dict[str, Any]:
    return {
        "name": "drained-worker",
        "team": "wave10",
        "role": "auditor",
        "scope": "audit something",
        "targets": ["OMN-1234"],
        "repo": repo,
    }


def _write_item(queue_dir: Path, payload: object, name: str = "item.yaml") -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    path = queue_dir / name
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


@pytest.mark.integration
def test_empty_queue_returns_empty(tmp_path: Path) -> None:
    """No queue item -> status=empty, artifact still written."""
    handler = HandlerDispatchQueueDrainer(dispatch_worker=_MockDispatchWorker())
    result = handler.handle(
        queue_dir=tmp_path / "queue",
        state_dir=tmp_path / "state",
        omni_home=tmp_path / "omni_home",
    )
    assert result.status == "empty"
    assert result.result_artifact_path
    assert Path(result.result_artifact_path).exists()


@pytest.mark.integration
def test_valid_item_compiles(tmp_path: Path) -> None:
    """Valid item + existing repo -> status=compiled, worker result captured."""
    omni_home = tmp_path / "omni_home"
    (omni_home / "omniclaude").mkdir(parents=True)
    queue_dir = tmp_path / "queue"
    _write_item(queue_dir, _valid_item(repo="omniclaude"))

    worker = _MockDispatchWorker()
    handler = HandlerDispatchQueueDrainer(dispatch_worker=worker)
    result = handler.handle(
        queue_dir=queue_dir,
        state_dir=tmp_path / "state",
        omni_home=omni_home,
    )

    assert result.status == "compiled"
    assert result.dispatch_worker_command is not None
    assert result.dispatch_worker_result is not None
    assert (
        result.dispatch_worker_result["validated_prompt_template"] == "COMPILED PROMPT"
    )
    assert len(worker.calls) == 1
    assert worker.calls[0].name == "drained-worker"
    assert Path(result.result_artifact_path).exists()
    # the persisted artifact must round-trip to the same terminal status
    reloaded = ModelDispatchQueueDrainerResult.model_validate_json(
        Path(result.result_artifact_path).read_text(encoding="utf-8")
    )
    assert reloaded.status == "compiled"


@pytest.mark.integration
def test_missing_repo_is_blocked(tmp_path: Path) -> None:
    """NEGATIVE CONTROL: repo absent under omni_home -> status=blocked."""
    omni_home = tmp_path / "omni_home"
    omni_home.mkdir(parents=True)  # repo dir intentionally NOT created
    queue_dir = tmp_path / "queue"
    _write_item(queue_dir, _valid_item(repo="omniclaude"))

    worker = _MockDispatchWorker()
    handler = HandlerDispatchQueueDrainer(dispatch_worker=worker)
    result = handler.handle(
        queue_dir=queue_dir,
        state_dir=tmp_path / "state",
        omni_home=omni_home,
    )

    assert result.status == "blocked"
    assert "not found" in result.blocked_reason
    # the worker boundary must never be invoked once blocked on missing repo
    assert worker.calls == []
    assert Path(result.result_artifact_path).exists()


@pytest.mark.integration
def test_non_mapping_yaml_is_blocked(tmp_path: Path) -> None:
    """A YAML list (not a mapping) -> status=blocked with a structural reason."""
    queue_dir = tmp_path / "queue"
    _write_item(queue_dir, ["not", "a", "mapping"])

    handler = HandlerDispatchQueueDrainer(dispatch_worker=_MockDispatchWorker())
    result = handler.handle(
        queue_dir=queue_dir,
        state_dir=tmp_path / "state",
        omni_home=tmp_path / "omni_home",
    )
    assert result.status == "blocked"
    assert "mapping" in result.blocked_reason


@pytest.mark.integration
def test_worker_rejection_is_blocked(tmp_path: Path) -> None:
    """Dispatch worker rejects the compiled command -> status=blocked."""
    omni_home = tmp_path / "omni_home"
    (omni_home / "omniclaude").mkdir(parents=True)
    queue_dir = tmp_path / "queue"
    _write_item(queue_dir, _valid_item(repo="omniclaude"))

    worker = _MockDispatchWorker(rejected_reason="worker already in_progress")
    handler = HandlerDispatchQueueDrainer(dispatch_worker=worker)
    result = handler.handle(
        queue_dir=queue_dir,
        state_dir=tmp_path / "state",
        omni_home=omni_home,
    )
    assert result.status == "blocked"
    assert "dispatch worker rejected" in result.blocked_reason
    assert len(worker.calls) == 1


@pytest.mark.integration
def test_limit_not_one_raises(tmp_path: Path) -> None:
    """NEGATIVE CONTROL: the first drainer slice only supports limit=1."""
    handler = HandlerDispatchQueueDrainer(dispatch_worker=_MockDispatchWorker())
    with pytest.raises(ValueError, match="limit=1 only"):
        handler.handle(
            queue_dir=tmp_path / "queue",
            state_dir=tmp_path / "state",
            omni_home=tmp_path / "omni_home",
            limit=2,
        )
