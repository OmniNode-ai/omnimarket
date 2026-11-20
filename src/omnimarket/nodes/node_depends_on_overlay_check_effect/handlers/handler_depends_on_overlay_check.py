# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuses a node whose declared dependency on an overlay row is absent from the overlays repository (OMN-24537).

The judgement is omnibase_core's generic depends-on validator (``check_depends_on``): a node contract declares what it
depends on, and this node supplies the facts, which are the overlay documents (and the top-level keys of each) found in the
overlays checkout the caller names. The kinds, the declaration key paths, the field separator and the diagnostics are the
validator's contract data; the layout names (contract file, overlay file, the kind the facts are supplied under) are the
command's. A node that declares nothing is not judged, so a node cannot be refused for a dependency it never stated.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from omnibase_core.models.contracts.subcontracts.model_validator_subcontract import (
    ModelValidatorSubcontract,
)
from omnibase_core.models.depends_on.model_depends_on_check_input import (
    ModelDependsOnCheckInput,
)
from omnibase_core.models.depends_on.model_depends_on_facts import ModelDependsOnFacts
from omnibase_core.models.depends_on.model_depends_on_rules import ModelDependsOnRules
from omnibase_core.validation.checker_depends_on import check_depends_on
from omnibase_core.validation.validator_depends_on import ValidatorDependsOn

from omnimarket.nodes.node_depends_on_overlay_check_effect.models import (
    EnumDependsOnFindingCode,
    ModelDependsOnFinding,
    ModelDependsOnOverlayCheckCommand,
    ModelDependsOnOverlayCheckResult,
)


class HandlerDependsOnOverlayCheck:
    """Reads node contracts and the overlays checkout, and judges each declared overlay dependency."""

    def __init__(self, rules: ModelValidatorSubcontract | None = None) -> None:
        """``rules`` is the core validator's contract; the packaged one when omitted."""
        self._rules = rules if rules is not None else ValidatorDependsOn().contract

    def handle(
        self, command: ModelDependsOnOverlayCheckCommand
    ) -> ModelDependsOnOverlayCheckResult:
        separator = (
            ModelDependsOnRules.from_contract(self._rules)
            .row("dependency_absent")
            .texts["field_separator"]
        )
        tokens = self._overlay_tokens(command, separator)
        facts = ModelDependsOnFacts(present={command.overlay_kind: tokens})
        nodes_root = Path(command.nodes_root)
        names = command.node_names or tuple(
            sorted(
                child.name
                for child in nodes_root.iterdir()
                if (child / command.contract_file_name).is_file()
            )
        )
        findings: list[ModelDependsOnFinding] = []
        nodes_checked = 0
        dependencies = 0
        for name in names:
            contract = nodes_root / name / command.contract_file_name
            if not contract.is_file():
                continue
            result = check_depends_on(
                ModelDependsOnCheckInput(
                    source_name=f"{name}/{command.contract_file_name}",
                    text=contract.read_text(encoding="utf-8"),
                    facts=facts,
                    contract=self._rules,
                )
            )
            if result.dependencies_checked:
                nodes_checked += 1
            dependencies += result.dependencies_checked
            findings.extend(
                ModelDependsOnFinding(
                    node=name,
                    code=EnumDependsOnFindingCode(issue.code or ""),
                    detail=issue.message,
                    line=issue.line_number,
                )
                for issue in result.issues
            )
        return ModelDependsOnOverlayCheckResult(
            ok=not findings,
            nodes_checked=nodes_checked,
            dependencies_checked=dependencies,
            overlays_present=sum(1 for t in tokens if separator not in t),
            findings=tuple(findings),
        )

    @staticmethod
    def _overlay_tokens(
        command: ModelDependsOnOverlayCheckCommand, separator: str
    ) -> tuple[str, ...]:
        """Every overlay id that has a document, and ``id<separator>key`` for each top-level key of its document."""
        tokens: list[str] = []
        root = Path(command.overlays_root)
        for child in sorted(root.iterdir()):
            document = child / command.overlay_file_name
            if not document.is_file():
                continue
            tokens.append(child.name)
            try:
                loaded = yaml.safe_load(document.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError):
                continue
            if isinstance(loaded, dict):
                tokens.extend(
                    f"{child.name}{separator}{key}"
                    for key in loaded
                    if isinstance(key, str)
                )
        return tuple(tokens)
