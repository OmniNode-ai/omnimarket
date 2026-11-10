# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pick the validators that apply to a change, from their contracts (OMN-23078).

A validator is canonical when its node contract declares the overlay's node type, purity and
tag. It applies when a changed path matches one of its validation contract's
``target_patterns`` and none of its ``exclude_patterns``. Its handler is the node contract's
``handler_routing.default_handler``.
"""

from __future__ import annotations

import importlib
import re
from functools import cache
from typing import Any

from omnibase_core.models.contracts.subcontracts.model_validator_subcontract import (
    ModelValidatorSubcontract,
)

from omnimarket.nodes.node_validator_workflow_orchestrator.models.model_validator_workflow import (
    EnumSelection,
    ModelValidatorChoice,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.overlay import (
    ModelCanonicalRequirement,
    ModelValidatorEntry,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.protocols import (
    ProtocolContractSource,
)


def is_canonical(
    node_contract: dict[str, Any], requirement: ModelCanonicalRequirement
) -> tuple[bool, str]:
    node_type = str(node_contract.get("node_type", "")).lower()
    descriptor = node_contract.get("descriptor") or {}
    purity = (
        str(descriptor.get("purity", "")).lower()
        if isinstance(descriptor, dict)
        else ""
    )
    tags = node_contract.get("tags") or []
    if node_type != requirement.node_type:
        return (
            False,
            f"node_type={node_type or 'absent'}, needs {requirement.node_type}",
        )
    if purity != requirement.purity:
        return False, f"purity={purity or 'absent'}, needs {requirement.purity}"
    if requirement.tag not in tags:
        return False, f"tag {requirement.tag} absent"
    return True, ""


@cache
def glob_regex(pattern: str) -> re.Pattern[str]:
    """A path glob as a regex: ``**/`` is any number of directories, ``*`` stays in one."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def matching_paths(
    contract: ModelValidatorSubcontract, paths: tuple[str, ...]
) -> tuple[str, ...]:
    def hit(path: str, patterns: list[str]) -> bool:
        return any(glob_regex(p).fullmatch(path) for p in patterns)

    return tuple(
        p
        for p in paths
        if hit(p, contract.target_patterns) and not hit(p, contract.exclude_patterns)
    )


def handler_ref(entry: ModelValidatorEntry, node_contract: dict[str, Any]) -> str:
    routing = node_contract.get("handler_routing") or {}
    ref = str(routing.get("default_handler", "")) if isinstance(routing, dict) else ""
    if ":" not in ref:
        return ""
    module, name = ref.split(":", 1)
    if "." not in module:
        module = f"{entry.node_package}.{module}"
    return f"{module}:{name}"


def resolve_handler(ref: str) -> Any:
    module, name = ref.split(":", 1)
    return getattr(importlib.import_module(module), name)()


def load_validation_contract(
    source: ProtocolContractSource, entry: ModelValidatorEntry
) -> ModelValidatorSubcontract:
    document = source.validation_contract(
        entry.validation_contract_package, entry.validation_contract_file
    )
    return ModelValidatorSubcontract.model_validate(document["validation"])


def choose(
    entry: ModelValidatorEntry,
    changed_paths: tuple[str, ...],
    source: ProtocolContractSource,
    requirement: ModelCanonicalRequirement,
) -> tuple[ModelValidatorChoice, ModelValidatorSubcontract | None]:
    try:
        node_contract = source.node_contract(entry.node_package)
        validation = load_validation_contract(source, entry)
    except Exception as exc:
        return (
            ModelValidatorChoice(
                validator_id=entry.validator_id,
                selection=EnumSelection.CONTRACT_UNREADABLE,
                reason=f"{type(exc).__name__}: {exc}",
            ),
            None,
        )
    canonical, why = is_canonical(node_contract, requirement)
    ref = handler_ref(entry, node_contract)
    if not canonical or not ref:
        return (
            ModelValidatorChoice(
                validator_id=entry.validator_id,
                selection=EnumSelection.NOT_CANONICAL,
                reason=why or "handler_routing.default_handler absent",
            ),
            validation,
        )
    matched = matching_paths(validation, changed_paths)
    return (
        ModelValidatorChoice(
            validator_id=entry.validator_id,
            selection=EnumSelection.SELECTED
            if matched
            else EnumSelection.NOT_APPLICABLE,
            reason="" if matched else "no changed path matches target_patterns",
            handler_ref=ref,
            matched_paths=matched,
        ),
        validation,
    )
