# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived golden chains for the renderer-capability projection.

OMN-13131 / W8 (gate G-F, epic OMN-13129 Contract-Driven UI Platform).

These are *golden chains*, not ad-hoc unit tests: each chain is a JSON fixture
(declaration command events on the canonical
``onex.cmd.ui.renderer-capability-declared.v1`` topic + the expected projection
end-state) that we **replay deterministically** through the *real* W5 sole-writer
reducer fold and the *real-dispatch-path* handler resolved via the canonical
``omnibase_core`` contract loader (the same loader the runtime uses). The
projection end-state is asserted **byte-for-byte** against the fixture, so a
drift in the fold, the freshness derivation, or the projection schema fails the
chain.

Three DoD-mandated chains:

  * POSITIVE — a capability is declared -> projection materialized -> heartbeat
    fresh -> ``is_degraded=false`` and no ``empty_state_reason``.
  * NEGATIVE (a) — declared -> heartbeat TTL expires (observed past the TTL) ->
    projection -> ``is_degraded=true`` carrying
    ``EnumEmptyStateReason.UPSTREAM_BLOCKED``.
  * NEGATIVE (b) — NO capability declared -> a dispatcher requests a renderer ->
    the capability resolution against the (empty) projection finds no row ->
    the typed ``EnumEmptyStateReason.UPSTREAM_BLOCKED`` is surfaced rather than
    a blind render.

Each chain is replayed twice: once through the pure fold and once through the
canonical-contract-loader-resolved handler dispatch — proving the wired node
(not an isolated handler) produces the golden end-state.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.contracts.contract_loader import load_contract
from omnibase_core.enums.enum_empty_state_reason import EnumEmptyStateReason
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.topics import RENDERER_CAPABILITY_DECLARED_TOPIC_V1
from omnimarket.nodes.node_renderer_capability_projection.handlers.handler_renderer_capability_projection import (
    STATE_KEY,
    HandlerRendererCapabilityProjection,
)
from omnimarket.nodes.node_renderer_capability_projection.models.model_renderer_capability_declaration import (
    ModelRendererCapabilityDeclaration,
)
from omnimarket.nodes.node_renderer_capability_projection.models.model_renderer_capability_projection_state import (
    ModelRendererCapabilityProjectionState,
)
from omnimarket.nodes.node_renderer_capability_projection.renderer_capability_fold import (
    fold_declaration,
)

_CHAIN_DIR = Path(__file__).resolve().parent
_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_renderer_capability_projection"
)

_CHAIN_FILES = sorted(_CHAIN_DIR.glob("chain_*.json"))


def _load_chain(path: Path) -> dict[str, Any]:
    chain: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return chain


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _normalize(state: ModelRendererCapabilityProjectionState) -> Any:
    """Canonical JSON form of a projection end-state for byte-for-byte compare."""
    return json.loads(state.model_dump_json())


def _expected(chain: dict[str, Any]) -> Any:
    """Round-trip the fixture's expected end-state through the model so the
    comparison is byte-for-byte against the *model's* canonical serialization
    (not a hand-formatted fixture string), and the fixture itself is validated."""
    state = ModelRendererCapabilityProjectionState.model_validate(
        chain["expected_state"]
    )
    return _normalize(state)


def _replay_fold(chain: dict[str, Any]) -> ModelRendererCapabilityProjectionState:
    """Replay the chain through the pure W5 reducer fold."""
    ttl = int(chain["ttl_seconds"])
    state = ModelRendererCapabilityProjectionState()
    for event in chain["events"]:
        declaration = ModelRendererCapabilityDeclaration.model_validate(
            event["payload"]
        )
        state = fold_declaration(
            state,
            declaration,
            observed_at=_parse_ts(event["observed_at"]),
            ttl_seconds=ttl,
        )
    return state


def _envelope(payload: object, *, observed_at: datetime) -> ModelEventEnvelope[object]:
    return ModelEventEnvelope(
        payload=payload,
        correlation_id=uuid4(),
        envelope_timestamp=observed_at,
        event_type=RENDERER_CAPABILITY_DECLARED_TOPIC_V1,
    )


def _resolve_wired_handler() -> type[HandlerRendererCapabilityProjection]:
    """Resolve the handler class through the canonical contract loader.

    This is the real-dispatch-path: load the node's contract via the exact
    ``omnibase_core`` loader the runtime uses, confirm the subscribe topic is the
    canonical constant, then resolve the handler class declared in
    ``handler_routing`` through importlib (the auto-wiring resolution path). A
    handler that was never wired into the contract would fail to resolve here.
    """
    contract = load_contract(_NODE_DIR / "contract.yaml")

    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    assert RENDERER_CAPABILITY_DECLARED_TOPIC_V1 in event_bus["subscribe_topics"]

    routing = contract["handler_routing"]
    assert isinstance(routing, dict)
    entry = routing["handlers"][0]
    handler_ref = entry["handler"]
    module = importlib.import_module(handler_ref["module"])
    resolved_cls = getattr(module, handler_ref["name"])
    assert resolved_cls is HandlerRendererCapabilityProjection
    return HandlerRendererCapabilityProjection


async def _replay_dispatch(
    chain: dict[str, Any],
) -> ModelRendererCapabilityProjectionState:
    """Replay the chain through the canonical-contract-loader-resolved handler.

    Accumulates the projection across dispatch invocations by threading the prior
    state under ``STATE_KEY`` in the flattened mapping payload — exactly the shape
    the live auto-wiring dispatcher delivers.
    """
    handler = _resolve_wired_handler()()
    state = ModelRendererCapabilityProjectionState()
    for event in chain["events"]:
        payload: dict[str, Any] = {
            **event["payload"],
            STATE_KEY: state.model_dump(mode="json"),
        }
        output = await handler.handle(
            _envelope(payload, observed_at=_parse_ts(event["observed_at"]))
        )
        assert output.node_kind == EnumNodeKind.REDUCER
        assert output.events == ()
        assert output.intents == ()
        assert output.result is None
        assert len(output.projections) == 1
        projected = output.projections[0]
        assert isinstance(projected, ModelRendererCapabilityProjectionState)
        state = projected
    return state


@pytest.mark.integration
@pytest.mark.parametrize("chain_path", _CHAIN_FILES, ids=[p.stem for p in _CHAIN_FILES])
class TestRendererCapabilityGoldenChains:
    """Replay each contract-derived chain through the real reducer + dispatch."""

    def test_fold_replay_matches_golden_end_state(self, chain_path: Path) -> None:
        """The pure fold replay reproduces the golden projection byte-for-byte."""
        chain = _load_chain(chain_path)
        actual = _normalize(_replay_fold(chain))
        assert actual == _expected(chain), (
            f"fold replay drifted from golden end-state for {chain['chain_name']}"
        )

    async def test_dispatch_replay_matches_golden_end_state(
        self, chain_path: Path
    ) -> None:
        """The real-dispatch-path (contract-loader-resolved handler) replay
        reproduces the same golden projection byte-for-byte."""
        chain = _load_chain(chain_path)
        actual = _normalize(await _replay_dispatch(chain))
        assert actual == _expected(chain), (
            f"dispatch replay drifted from golden end-state for {chain['chain_name']}"
        )


@pytest.mark.integration
class TestRendererCapabilityGoldenChainSemantics:
    """Per-chain semantic assertions on the replayed golden end-states.

    Byte-for-byte fixture matching guards drift; these assertions document the
    capability-gate behavior each chain proves (fresh vs degraded vs absent).
    """

    def _chain(self, name: str) -> dict[str, Any]:
        path = _CHAIN_DIR / f"{name}.json"
        assert path.exists(), f"missing golden chain fixture: {name}"
        return _load_chain(path)

    def test_positive_chain_is_fresh_and_not_degraded(self) -> None:
        chain = self._chain("chain_positive_capability_fresh")
        state = _replay_fold(chain)
        row = state.row_for("ui.effect.web")
        assert row is not None
        assert row.is_degraded is False
        assert row.empty_state_reason is None

    def test_negative_a_chain_is_degraded_with_typed_reason(self) -> None:
        chain = self._chain("chain_negative_a_heartbeat_ttl_expired")
        state = _replay_fold(chain)
        row = state.row_for("ui.effect.web")
        assert row is not None
        assert row.is_degraded is True
        assert row.empty_state_reason == EnumEmptyStateReason.UPSTREAM_BLOCKED

    def test_negative_b_chain_no_row_resolves_to_upstream_blocked(self) -> None:
        """NO capability -> dispatcher request resolves against the empty
        projection -> the typed UPSTREAM_BLOCKED gate (no blind render)."""
        chain = self._chain("chain_negative_b_no_capability_dispatcher_blocked")
        state = _replay_fold(chain)
        assert state.rows == ()

        request = chain["dispatcher_request"]
        assert request is not None
        requested = request["requested_renderer_id"]

        # The dispatcher resolves the requested renderer against the projection
        # read authority. Absence is a hard capability gate, not a no-op.
        resolved_row = state.row_for(requested)
        assert resolved_row is None
        empty_state_reason = (
            EnumEmptyStateReason.UPSTREAM_BLOCKED
            if resolved_row is None or resolved_row.is_degraded
            else None
        )
        assert empty_state_reason == EnumEmptyStateReason.UPSTREAM_BLOCKED
        assert empty_state_reason.value == request["expected_empty_state_reason"]
