# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-declared tier source binding of node_context_pack_orchestrator (TA.8).

The orchestrator resolves the source node for each tier from its contract's
``context_tier_sources`` block, so the runtime's zero-argument construction reads every
bound tier from its live source instead of answering every tier ``unavailable``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.tier_source_binding import (
    load_tier_sources,
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
from omnimarket.nodes.node_context_source_work_state_effect.handlers.handler_context_source_work_state import (
    HandlerContextSourceWorkState,
)
from omnimarket.nodes.node_session_context_sources_effect.handlers.handler_session_context_sources import (
    HandlerSessionContextSources,
)

from .conftest import PROFILES_DIR

pytestmark = pytest.mark.unit

_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_pack_orchestrator"
)
_CONTRACT_PATH = _NODE_DIR / "contract.yaml"
_CONTRACT = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))

_EXPECTED: dict[EnumContextFactor, type] = {
    EnumContextFactor.OPERATOR_RULINGS: HandlerContextSourceLedger,
    EnumContextFactor.OPEN_WORK: HandlerContextSourceWorkState,
    EnumContextFactor.ORCHESTRATION_STATE: HandlerContextSourceWorkState,
    EnumContextFactor.OPERATOR_WORDS_UNRECORDED: HandlerSessionContextSources,
    EnumContextFactor.REPO_STATE: HandlerSessionContextSources,
    EnumContextFactor.LANE_RULES: HandlerContextSourceGuidance,
    EnumContextFactor.ARCHITECTURE_PATTERNS: HandlerContextSourceGuidance,
}


def _write_contract(tmp_path: Path, block: object) -> Path:
    raw = dict(_CONTRACT)
    raw["context_tier_sources"] = block
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_contract_binds_each_served_factor_to_its_source_node() -> None:
    block = _CONTRACT["context_tier_sources"]
    assert block == {
        "operator_rulings": "node_context_source_ledger_effect",
        "open_work": "node_context_source_work_state_effect",
        "orchestration_state": "node_context_source_work_state_effect",
        "operator_words_unrecorded": "node_session_context_sources_effect",
        "repo_state": "node_session_context_sources_effect",
        "lane_rules": "node_context_source_guidance_effect",
        "architecture_patterns": "node_context_source_guidance_effect",
    }


def test_every_bound_source_node_is_a_declared_dependency() -> None:
    declared = {d["name"] for d in _CONTRACT["dependencies"]}
    assert set(_CONTRACT["context_tier_sources"].values()) <= declared


def test_load_tier_sources_instantiates_each_node_handler() -> None:
    bound = load_tier_sources()
    assert {f: type(s) for f, s in bound.items()} == _EXPECTED
    assert all(isinstance(s, ProtocolContextTierSource) for s in bound.values())


def test_a_node_bound_to_several_factors_is_one_instance() -> None:
    bound = load_tier_sources()
    assert (
        bound[EnumContextFactor.OPEN_WORK]
        is bound[EnumContextFactor.ORCHESTRATION_STATE]
    )
    assert (
        bound[EnumContextFactor.LANE_RULES]
        is bound[EnumContextFactor.ARCHITECTURE_PATTERNS]
    )


def test_loading_makes_no_connection_without_a_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OMNIBASE_INFRA_DB_URL", "OMNIMARKET_READ_DB_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert load_tier_sources()


def test_zero_argument_orchestrator_binds_the_contract_sources() -> None:
    handler = HandlerContextPackOrchestrator()
    assert {f: type(s) for f, s in handler._sources.items()} == _EXPECTED


def test_explicit_empty_sources_bind_nothing() -> None:
    handler = HandlerContextPackOrchestrator({})
    assert handler._sources == {}


def test_compaction_required_factors_are_all_bound() -> None:
    profile = yaml.safe_load(
        (PROFILES_DIR / "compaction_orchestrator.yaml").read_text(encoding="utf-8")
    )
    required = {EnumContextFactor(f) for f in profile["required_factors"]}
    assert required <= set(load_tier_sources())


def test_unknown_factor_is_refused(tmp_path: Path) -> None:
    path = _write_contract(
        tmp_path, {"not_a_factor": "node_context_source_ledger_effect"}
    )
    with pytest.raises(ValueError, match="not_a_factor"):
        load_tier_sources(path)


def test_unknown_source_node_is_refused(tmp_path: Path) -> None:
    path = _write_contract(tmp_path, {"operator_rulings": "node_does_not_exist"})
    with pytest.raises(ValueError, match="node_does_not_exist"):
        load_tier_sources(path)


def test_source_node_without_the_read_is_refused(tmp_path: Path) -> None:
    path = _write_contract(
        tmp_path, {"operator_rulings": "node_context_pack_render_compute"}
    )
    with pytest.raises(ValueError, match="node_context_pack_render_compute"):
        load_tier_sources(path)


def test_missing_block_binds_nothing(tmp_path: Path) -> None:
    raw = {k: v for k, v in _CONTRACT.items() if k != "context_tier_sources"}
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    assert load_tier_sources(path) == {}
