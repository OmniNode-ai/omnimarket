# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime composition of the contract-declared context tier sources."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator_runtime import (
    HandlerContextPackOrchestratorRuntime,
    resolve_tier_source,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)
from omnimarket.nodes.node_context_source_guidance_effect.handlers.handler_context_source_guidance import (
    HandlerContextSourceGuidance,
)
from omnimarket.nodes.node_context_source_ledger_effect.handlers.handler_context_source_ledger import (
    HandlerContextSourceLedger,
)
from omnimarket.nodes.node_context_source_repo_doctrine_effect.handlers.handler_context_source_repo_doctrine import (
    HandlerContextSourceRepoDoctrine,
)
from omnimarket.nodes.node_context_source_work_state_effect.handlers.handler_context_source_work_state import (
    HandlerContextSourceWorkState,
)
from omnimarket.nodes.node_session_context_sources_effect.handlers.handler_session_context_sources import (
    HandlerSessionContextSources,
)

pytestmark = pytest.mark.unit

_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_pack_orchestrator"
)
_CONTRACT = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
_RUNTIME_MODULE = (
    "omnimarket.nodes.node_context_pack_orchestrator.handlers."
    "handler_context_pack_orchestrator_runtime"
)

_EXPECTED: dict[EnumContextFactor, type] = {
    EnumContextFactor.OPERATOR_RULINGS: HandlerContextSourceLedger,
    EnumContextFactor.OPEN_WORK: HandlerContextSourceWorkState,
    EnumContextFactor.ORCHESTRATION_STATE: HandlerContextSourceWorkState,
    EnumContextFactor.OPERATOR_WORDS_UNRECORDED: HandlerSessionContextSources,
    EnumContextFactor.REPO_STATE: HandlerSessionContextSources,
    EnumContextFactor.LANE_RULES: HandlerContextSourceGuidance,
    EnumContextFactor.ARCHITECTURE_PATTERNS: HandlerContextSourceGuidance,
    EnumContextFactor.INVARIANTS: HandlerContextSourceRepoDoctrine,
    EnumContextFactor.CANONICAL_SHAPES: HandlerContextSourceRepoDoctrine,
    EnumContextFactor.EXEMPLAR: HandlerContextSourceRepoDoctrine,
}


def test_contract_names_the_runtime_handler() -> None:
    handler = _CONTRACT["handler"]
    assert handler["module"] == _RUNTIME_MODULE
    assert handler["class"] == "HandlerContextPackOrchestratorRuntime"
    routes = _CONTRACT["handler_routing"]["handlers"]
    assert len(routes) == 1
    assert routes[0]["handler"] == {
        "module": _RUNTIME_MODULE,
        "name": "HandlerContextPackOrchestratorRuntime",
    }
    assert HandlerContextPackOrchestratorRuntime.__module__ == _RUNTIME_MODULE
    assert issubclass(
        HandlerContextPackOrchestratorRuntime, HandlerContextPackOrchestrator
    )


def test_zero_argument_runtime_binds_each_source_once_per_node() -> None:
    handler = HandlerContextPackOrchestratorRuntime()
    block = _CONTRACT["context_tier_sources"]
    assert set(handler._sources) == {EnumContextFactor(factor) for factor in block}
    assert {factor: type(source) for factor, source in handler._sources.items()} == (
        _EXPECTED
    )
    nodes: dict[str, ProtocolContextTierSource] = {}
    for factor, node_name in block.items():
        source = handler._sources[EnumContextFactor(factor)]
        assert source is nodes.setdefault(node_name, source)
    assert len({id(source) for source in nodes.values()}) == len(nodes)


def test_unknown_runtime_source_is_refused() -> None:
    with pytest.raises(
        ValueError, match="No runtime tier source for 'node_not_a_source'"
    ):
        resolve_tier_source("node_not_a_source")
