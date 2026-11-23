# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``handle(ModelTriggerCheckRequest) -> ModelTriggerCheckResult``: does anything run each node?

A node needs a trigger when its contract sets ``runtime_dispatch.external_trigger`` or subscribes to no topic: the
event bus never starts it. Such a node declares what does, in one of:

* a subscription to the platform runtime tick together with a ``schedule`` (top level or under ``config``);
* a top-level ``schedule`` block;
* ``runtime_dispatch.host_unit``: a unit of the host installer registry that has a schedule and runs a module of the node;
* ``runtime_dispatch.on_demand``: the command that runs it and the owner who answers for it;
* ``runtime_dispatch.callers``: files of the repository that exist and name the node (the node's own directory and
  the tests are no caller).

A declaration that names something that does not hold is refused as a bad declaration, never passed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.models.model_node_trigger import (
    INSTALLER_REGISTRY_NODE,
    ModelRuntimeDispatchTriggers,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.models import (
    EnumTriggerFindingReason,
    ModelTriggerCheckRequest,
    ModelTriggerCheckResult,
    ModelTriggerFinding,
)

NODES_DIR = "src/omnimarket/nodes"
CONTRACT_FILE = "contract.yaml"
TICK_TOPIC_MARK = "runtime-tick"
_TESTS_DIR = "tests"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _has_schedule(contract: Mapping[str, Any]) -> bool:
    if contract.get("schedule"):
        return True
    return any(
        isinstance(block, dict) and block.get("schedule")
        for block in _mapping(contract.get("config")).values()
    )


def _subscribes_to_tick(subscribe: object) -> bool:
    topics = subscribe if isinstance(subscribe, list) else []
    return any(TICK_TOPIC_MARK in str(topic) for topic in topics)


def _caller_problem(root: Path, node: str, path: str) -> str | None:
    """Why this caller path runs nothing, or None when it is a real caller."""
    relative = Path(path)
    own_dir = Path(NODES_DIR) / node
    if relative.is_absolute() or ".." in relative.parts:
        return f"{path}: not a path inside the repository"
    if relative.parts[:1] == (_TESTS_DIR,):
        return f"{path}: a test runs nothing"
    if own_dir in (relative, *relative.parents):
        return f"{path}: the node's own directory is not its caller"
    target = root / relative
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return f"{path}: no such readable file"
    if node not in text:
        return f"{path}: does not name {node}"
    return None


class HandlerTriggerDeclarationCheck:
    def handle(self, request: ModelTriggerCheckRequest) -> ModelTriggerCheckResult:
        root = Path(request.root)
        contracts = (
            self._all_contracts(root) if request.all_nodes else request.contracts
        )
        units = self._installer_units(root)
        findings: list[ModelTriggerFinding] = []
        needing = 0
        for contract_path in contracts:
            node = Path(contract_path).parent.name
            try:
                loaded = yaml.safe_load((root / contract_path).read_text("utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                loaded, why = None, f"unreadable: {type(exc).__name__}"
            else:
                why = "not a mapping"
            if not isinstance(loaded, dict):
                findings.append(
                    self._finding(
                        node,
                        contract_path,
                        EnumTriggerFindingReason.UNREADABLE_CONTRACT,
                        why,
                    )
                )
                continue
            if not self._needs_trigger(loaded):
                continue
            needing += 1
            finding = self._judge(root, node, contract_path, loaded, units)
            if finding is not None:
                findings.append(finding)
        return ModelTriggerCheckResult(
            checked=len(contracts), needing_trigger=needing, findings=tuple(findings)
        )

    @staticmethod
    def _finding(
        node: str, contract: str, reason: EnumTriggerFindingReason, detail: str
    ) -> ModelTriggerFinding:
        return ModelTriggerFinding(
            node=node, contract=contract, reason=reason, detail=detail
        )

    @staticmethod
    def _all_contracts(root: Path) -> tuple[str, ...]:
        return tuple(
            sorted(
                path.relative_to(root).as_posix()
                for path in (root / NODES_DIR).glob(f"*/{CONTRACT_FILE}")
            )
        )

    @staticmethod
    def _installer_units(root: Path) -> Mapping[str, Mapping[str, Any]]:
        path = root / NODES_DIR / INSTALLER_REGISTRY_NODE / CONTRACT_FILE
        try:
            loaded = yaml.safe_load(path.read_text("utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return {}
        units = _mapping(loaded).get("units")
        return units if isinstance(units, dict) else {}

    @staticmethod
    def _needs_trigger(contract: Mapping[str, Any]) -> bool:
        dispatch = _mapping(contract.get("runtime_dispatch"))
        subscribe = _mapping(contract.get("event_bus")).get("subscribe_topics")
        return bool(dispatch.get("external_trigger")) or not subscribe

    def _judge(
        self,
        root: Path,
        node: str,
        contract_path: str,
        contract: Mapping[str, Any],
        units: Mapping[str, Mapping[str, Any]],
    ) -> ModelTriggerFinding | None:
        subscribe = _mapping(contract.get("event_bus")).get("subscribe_topics")
        if _has_schedule(contract) and (
            _subscribes_to_tick(subscribe) or contract.get("schedule")
        ):
            return None
        try:
            triggers = ModelRuntimeDispatchTriggers.model_validate(
                _mapping(contract.get("runtime_dispatch"))
            )
        except ValidationError as exc:
            fields = ", ".join(
                ".".join(str(part) for part in err["loc"]) for err in exc.errors()
            )
            return self._finding(
                node,
                contract_path,
                EnumTriggerFindingReason.INVALID_DECLARATION,
                f"runtime_dispatch declares a trigger with an invalid field: {fields}",
            )
        problems: list[str] = []
        if triggers.on_demand is not None:
            return None
        if triggers.host_unit is not None:
            if triggers.host_unit.runs_node(units, node):
                return None
            problems.append(
                f"host unit {triggers.host_unit.unit} does not exist, has no schedule or runs no module of {node}"
            )
        if triggers.callers:
            caller_problems = [
                problem
                for caller in triggers.callers
                if (problem := _caller_problem(root, node, caller.path)) is not None
            ]
            if not caller_problems:
                return None
            problems.extend(caller_problems)
        if problems:
            return self._finding(
                node,
                contract_path,
                EnumTriggerFindingReason.INVALID_DECLARATION,
                "; ".join(problems),
            )
        return self._finding(
            node,
            contract_path,
            EnumTriggerFindingReason.NO_TRIGGER_DECLARED,
            "no runtime-tick subscription with a schedule, schedule block, host_unit, on_demand or callers",
        )
