# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for the OmniGate check executor node."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from omnimarket.nodes.node_omnigate_check_executor.models.model_check_executor_input import (
    ModelCheckExecutorInput,
    ModelCheckExecutorResult,
    ModelOmniGateNodeCheckResult,
)

HandlerType = Literal["node_handler"]
HandlerCategory = Literal["effect"]
ConfigLoader = Callable[[Path], object]
CheckExecutor = Callable[[object, Path], Iterable[object]]

_BLOCKING_STATUSES = frozenset({"FAIL", "PENDING", "fail", "pending"})
_ADVISORY_STATUSES = frozenset({"ADVISORY", "advisory"})


def _load_config(config_path: Path) -> object:
    module = import_module("omnibase_core.gate.config_loader")

    return cast(Any, module).load_omnigate_config(config_path)


def _execute_checks(config: object, repo_path: Path) -> Iterable[object]:
    module = import_module("omnibase_infra.gate.executor")

    return cast("Iterable[object]", cast(Any, module).execute_checks(config, repo_path))


def _status_value(check: object) -> str:
    status = getattr(check, "status", "")
    value = getattr(status, "value", status)
    return str(value)


def _check_to_node_result(check: object) -> ModelOmniGateNodeCheckResult:
    if hasattr(check, "model_dump"):
        raw = cast(Any, check).model_dump(mode="json")
        if isinstance(raw, dict):
            return ModelOmniGateNodeCheckResult.model_validate(raw)
    return ModelOmniGateNodeCheckResult(
        name=str(getattr(check, "name", "")),
        command=str(getattr(check, "command", "")),
        status=_status_value(check),
        duration_ms=int(getattr(check, "duration_ms", 0)),
        stdout_preview=getattr(check, "stdout_preview", None),
        stdout_hash=getattr(check, "stdout_hash", None),
    )


def _advisory_blocks(config: object) -> bool:
    receipt_policy = getattr(config, "receipt", None)
    return bool(getattr(receipt_policy, "advisory_blocks", False))


class HandlerCheckExecutor:
    """Effect handler that executes trusted maintainer-authored OmniGate checks."""

    def __init__(
        self,
        *,
        config_loader: ConfigLoader | None = None,
        check_executor: CheckExecutor | None = None,
    ) -> None:
        self._config_loader = config_loader or _load_config
        self._check_executor = check_executor or _execute_checks

    @property
    def handler_type(self) -> HandlerType:
        return "node_handler"

    @property
    def handler_category(self) -> HandlerCategory:
        return "effect"

    async def handle(
        self,
        correlation_id: UUID,
        config_path: str,
        repo_path: str,
    ) -> ModelCheckExecutorResult:
        _ = correlation_id
        request = ModelCheckExecutorInput(config_path=config_path, repo_path=repo_path)
        config = self._config_loader(Path(request.config_path))
        raw_checks = tuple(self._check_executor(config, Path(request.repo_path)))
        checks = tuple(_check_to_node_result(check) for check in raw_checks)
        blocking = set(_BLOCKING_STATUSES)
        if _advisory_blocks(config):
            blocking.update(_ADVISORY_STATUSES)
        return ModelCheckExecutorResult(
            checks=checks,
            all_passed=all(check.status not in blocking for check in checks),
        )


__all__ = ["HandlerCheckExecutor"]
