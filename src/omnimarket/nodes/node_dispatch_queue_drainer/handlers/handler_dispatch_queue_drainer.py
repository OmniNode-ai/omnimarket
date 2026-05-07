# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compile-only drainer for legacy dispatch queue YAML files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_dispatch_queue_drainer.models import (
    ModelDispatchQueueDrainerResult,
    ModelDispatchQueueItem,
)
from omnimarket.nodes.node_dispatch_worker import ModelDispatchWorkerCommand
from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
    HandlerDispatchWorker,
)


class HandlerDispatchQueueDrainer:
    """Read one legacy queue item and compile it through node_dispatch_worker."""

    def __init__(self, dispatch_worker: HandlerDispatchWorker | None = None) -> None:
        self._dispatch_worker = dispatch_worker or HandlerDispatchWorker()

    def handle(
        self,
        *,
        queue_item_path: Path | None = None,
        queue_dir: Path | None = None,
        limit: int = 1,
        state_dir: Path | None = None,
        tasks_dir: Path | None = None,
        omni_home: Path | None = None,
    ) -> ModelDispatchQueueDrainerResult:
        """Compile one queue item and persist a terminal result artifact."""
        if limit != 1:
            raise ValueError("first drainer slice supports limit=1 only")

        resolved_state_dir = _resolve_state_dir(state_dir)
        resolved_queue_dir = queue_dir or resolved_state_dir / "dispatch_queue"
        selected_path = queue_item_path or self._oldest_queue_item(resolved_queue_dir)
        if selected_path is None:
            result = ModelDispatchQueueDrainerResult(status="empty")
            return self._write_result(result, resolved_state_dir)

        try:
            raw = self._read_yaml(selected_path)
        except (OSError, yaml.YAMLError) as exc:
            result = ModelDispatchQueueDrainerResult(
                status="blocked",
                queue_item_path=str(selected_path),
                blocked_reason=f"queue item could not be read: {exc!s}",
            )
            return self._write_result(result, resolved_state_dir)
        if not isinstance(raw, dict):
            result = ModelDispatchQueueDrainerResult(
                status="blocked",
                queue_item_path=str(selected_path),
                blocked_reason="queue item YAML must contain a mapping",
            )
            return self._write_result(result, resolved_state_dir)

        try:
            item = ModelDispatchQueueItem.model_validate(raw)
            command = _to_dispatch_worker_command(item)
        except ValidationError as exc:
            result = ModelDispatchQueueDrainerResult(
                status="blocked",
                queue_item_path=str(selected_path),
                blocked_reason=f"invalid queue item: {exc.errors()[0]['msg']}",
            )
            return self._write_result(result, resolved_state_dir)

        missing_repo_reason = self._missing_repo_reason(item, omni_home=omni_home)
        if missing_repo_reason:
            result = ModelDispatchQueueDrainerResult(
                status="blocked",
                queue_item_path=str(selected_path),
                blocked_reason=missing_repo_reason,
                dispatch_worker_command=command.model_dump(mode="json"),
            )
            return self._write_result(result, resolved_state_dir)

        previous_state_dir = os.environ.get("ONEX_STATE_DIR")
        os.environ["ONEX_STATE_DIR"] = str(resolved_state_dir)
        try:
            compiled = self._dispatch_worker.handle(command, tasks_dir=tasks_dir)
        finally:
            if previous_state_dir is None:
                os.environ.pop("ONEX_STATE_DIR", None)
            else:
                os.environ["ONEX_STATE_DIR"] = previous_state_dir

        if compiled.rejected_reason:
            result = ModelDispatchQueueDrainerResult(
                status="blocked",
                queue_item_path=str(selected_path),
                blocked_reason=f"dispatch worker rejected: {compiled.rejected_reason}",
                dispatch_worker_command=command.model_dump(mode="json"),
                dispatch_worker_result=compiled.model_dump(mode="json"),
            )
        else:
            result = ModelDispatchQueueDrainerResult(
                status="compiled",
                queue_item_path=str(selected_path),
                dispatch_worker_command=command.model_dump(mode="json"),
                dispatch_worker_result=compiled.model_dump(mode="json"),
            )
        return self._write_result(result, resolved_state_dir)

    def _read_yaml(self, path: Path) -> Any:
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def _oldest_queue_item(self, queue_dir: Path) -> Path | None:
        if not queue_dir.exists():
            return None
        candidates = sorted(
            (
                path
                for path in queue_dir.glob("*.yaml")
                if path.is_file() and path.parent == queue_dir
            ),
            key=lambda path: (path.stat().st_mtime, path.name),
        )
        return candidates[0] if candidates else None

    def _missing_repo_reason(
        self, item: ModelDispatchQueueItem, *, omni_home: Path | None
    ) -> str:
        repo = item.resolved_repo
        if not repo:
            return "queue item does not declare a repo and no repo target could be inferred"
        root = omni_home or _resolve_omni_home()
        repo_path = root / repo
        if not repo_path.is_dir():
            return f"repo {repo!r} not found under {root}"
        return ""

    def _write_result(
        self, result: ModelDispatchQueueDrainerResult, state_dir: Path
    ) -> ModelDispatchQueueDrainerResult:
        artifact_dir = state_dir / "dispatch_queue" / "drainer_results"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stem = "empty"
        if result.queue_item_path:
            stem = Path(result.queue_item_path).stem
        out_path = artifact_dir / f"{stem}-result.json"
        payload = result.model_copy(update={"result_artifact_path": str(out_path)})
        out_path.write_text(
            json.dumps(payload.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return payload


def _resolve_state_dir(state_dir: Path | None) -> Path:
    if state_dir is not None:
        return state_dir
    raw_state_dir = os.environ.get("ONEX_STATE_DIR")
    if raw_state_dir:
        return Path(raw_state_dir)
    return _resolve_omni_home() / ".onex_state"


def _resolve_omni_home() -> Path:
    return Path(os.environ["OMNI_HOME"])


def _to_dispatch_worker_command(
    item: ModelDispatchQueueItem,
) -> ModelDispatchWorkerCommand:
    return ModelDispatchWorkerCommand(
        name=item.name,
        team=item.team,
        role=item.role,
        scope=item.scope,
        targets=item.targets,
        collision_fences=item.collision_fences,
        reports_to=item.reports_to,
        wall_clock_cap_min=item.wall_clock_cap_min,
        model=item.model,
        replace=item.replace,
    )


__all__: list[str] = ["HandlerDispatchQueueDrainer"]
