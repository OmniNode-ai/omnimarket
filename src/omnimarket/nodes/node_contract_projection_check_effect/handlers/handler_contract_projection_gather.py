# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT handler: read contracts, handler modules and the baseline into the compute node's typed input.

This is the only place the contract projection checks touch the filesystem. The
paired COMPUTE node (``node_contract_projection_check_compute``) receives explicit
text and decides; nothing here decides.
"""

from pathlib import Path
from typing import Final

from omnibase_core.models.nodes.no_utcnow_check.model_source_file import ModelSourceFile

from omnimarket.models.contract_projection_check import (
    EnumProjectionContractRule,
    ModelProjectionContractCheckInput,
    ModelProjectionNodeSources,
)
from omnimarket.nodes.node_contract_projection_check_effect.models import (
    ModelContractProjectionGatherRequest,
)

_NODES_DIR: Final[str] = "src/omnimarket/nodes"
_DLQ_GLOB: Final[str] = "node_projection_*/handlers/handler_*.py"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _is_production(path: Path, root: Path) -> bool:
    parts = Path(_rel(path, root)).parts
    return "tests" not in parts and not path.name.startswith("test_")


class HandlerContractProjectionGather:
    def handle(
        self, request: ModelContractProjectionGatherRequest
    ) -> ModelProjectionContractCheckInput:
        root = Path(request.root)
        rule = EnumProjectionContractRule(request.rule)
        nodes: list[ModelProjectionNodeSources] = []
        handlers: list[ModelSourceFile] = []
        if rule is EnumProjectionContractRule.ACCESS:
            nodes = self._nodes(root, "*/contract.yaml", with_modules=True)
        elif rule is EnumProjectionContractRule.CURSOR:
            nodes = self._nodes(root, "node_*/contract.yaml", with_modules=False)
        else:
            handlers = self._handlers(root, list(request.filenames))
        return ModelProjectionContractCheckInput(
            rule=rule,
            nodes=tuple(nodes),
            handlers=tuple(handlers),
            cursor_baseline=tuple(self._baseline(root / request.baseline_path)),
            require_scanned=not request.filenames,
        )

    @staticmethod
    def _nodes(
        root: Path, contract_glob: str, *, with_modules: bool
    ) -> list[ModelProjectionNodeSources]:
        nodes: list[ModelProjectionNodeSources] = []
        for contract in sorted((root / _NODES_DIR).glob(contract_glob)):
            modules: list[ModelSourceFile] = []
            if with_modules:
                for module in sorted(contract.parent.rglob("*.py")):
                    if _is_production(module, root):
                        modules.append(
                            ModelSourceFile(
                                path=_rel(module, root),
                                source=module.read_text(encoding="utf-8"),
                            )
                        )
            nodes.append(
                ModelProjectionNodeSources(
                    node=contract.parent.name,
                    contract_path=_rel(contract, root),
                    contract_text=contract.read_text(encoding="utf-8"),
                    modules=tuple(modules),
                )
            )
        return nodes

    @staticmethod
    def _handlers(root: Path, filenames: list[str]) -> list[ModelSourceFile]:
        if filenames:
            paths = [Path(raw).resolve() for raw in filenames]
        else:
            paths = sorted((root / _NODES_DIR).glob(_DLQ_GLOB))
        handlers: list[ModelSourceFile] = []
        for path in paths:
            if not path.is_file() or path.suffix != ".py":
                continue
            handlers.append(
                ModelSourceFile(
                    path=_rel(path, root), source=path.read_text(encoding="utf-8")
                )
            )
        return handlers

    @staticmethod
    def _baseline(path: Path) -> list[str]:
        if not path.exists():
            return []
        return sorted(
            line.strip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        )
