# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_renderer_capability_projection (OMN-13131 / W5).

Covers the four DoD-mandated proofs for the sole-writer Renderer Capability
Registry reducer:

  1. Reducer fold — one declaration heartbeat materializes one projection row
     keyed on renderer_id; a later heartbeat from the same renderer upserts (not
     duplicates) the row.
  2. Heartbeat-TTL freshness — is_degraded is BOTH False (fresh heartbeat) AND
     True (TTL-expired heartbeat); a degraded row carries the typed
     EnumEmptyStateReason.UPSTREAM_BLOCKED (never renders blind).
  3. Real-dispatch-path — the canonical handler.handle(envelope) -> for_reducer
     output drives the fold (REDUCER node-kind, projections only), and the node
     resolves through the real auto-wiring contract loader (not handler isolation
     alone).
  4. G-E no-hardcoded-topic — the canonical onex.* command topic literal does NOT
     appear in any .py file in the node path; code references the constant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_accessibility_tier import EnumAccessibilityTier
from omnibase_core.enums.enum_empty_state_reason import EnumEmptyStateReason
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.enums.enum_renderer_interaction_model import (
    EnumRendererInteractionModel,
)
from omnibase_core.enums.enum_widget_type import EnumWidgetType
from omnibase_core.models.dashboard.model_renderer_capability_contract import (
    ModelRendererCapabilityContract,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.primitives.model_semver import ModelSemVer
from omnibase_core.validation.validator_topic_suffix import validate_topic_suffix

from omnimarket.events.topics import RENDERER_CAPABILITY_DECLARED_TOPIC_V1
from omnimarket.nodes.node_renderer_capability_projection.handlers.handler_renderer_capability_projection import (
    HandlerRendererCapabilityProjection,
)
from omnimarket.nodes.node_renderer_capability_projection.models.model_renderer_capability_declaration import (
    ModelRendererCapabilityDeclaration,
)
from omnimarket.nodes.node_renderer_capability_projection.models.model_renderer_capability_projection_state import (
    DEFAULT_HEARTBEAT_TTL_SECONDS,
    ModelRendererCapabilityProjectionRow,
    ModelRendererCapabilityProjectionState,
    is_heartbeat_degraded,
)
from omnimarket.nodes.node_renderer_capability_projection.renderer_capability_fold import (
    fold_declaration,
)

_T0 = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


def _capability(
    *,
    renderer_id: str = "ui.effect.web",
    platform: str = "web",
    kinds: tuple[EnumWidgetType, ...] = (EnumWidgetType.CHART, EnumWidgetType.TABLE),
) -> ModelRendererCapabilityContract:
    return ModelRendererCapabilityContract(
        renderer_id=renderer_id,
        platform=platform,
        supported_component_kinds=kinds,
        interaction_model=EnumRendererInteractionModel.POINTER,
        accessibility_tier=EnumAccessibilityTier.AA,
        contract_version=ModelSemVer(major=1, minor=0, patch=0),
        supports_interaction=True,
    )


def _declaration(
    *, renderer_id: str = "ui.effect.web", declared_at: datetime = _T0
) -> ModelRendererCapabilityDeclaration:
    return ModelRendererCapabilityDeclaration(
        capability=_capability(renderer_id=renderer_id), declared_at=declared_at
    )


def _envelope(
    payload: object, *, observed_at: datetime = _T0
) -> ModelEventEnvelope[object]:
    return ModelEventEnvelope(
        payload=payload,
        correlation_id=uuid4(),
        envelope_timestamp=observed_at,
        event_type=RENDERER_CAPABILITY_DECLARED_TOPIC_V1,
    )


def _row(
    state: ModelRendererCapabilityProjectionState, renderer_id: str
) -> ModelRendererCapabilityProjectionRow:
    """Fetch a row asserting it exists (keeps assertions mypy-strict-clean)."""
    row = state.row_for(renderer_id)
    assert row is not None, f"expected a projection row for {renderer_id}"
    return row


# ---------------------------------------------------------------------------
# 1. Reducer fold
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReducerFold:
    def test_single_declaration_materializes_one_row(self) -> None:
        state = fold_declaration(
            ModelRendererCapabilityProjectionState(),
            _declaration(),
            observed_at=_T0,
        )
        assert len(state.rows) == 1
        row = state.rows[0]
        assert row.renderer_id == "ui.effect.web"
        assert row.platform == "web"
        assert row.supported_component_kinds == (
            EnumWidgetType.CHART,
            EnumWidgetType.TABLE,
        )
        assert row.interaction_model == EnumRendererInteractionModel.POINTER
        assert row.accessibility_tier == EnumAccessibilityTier.AA
        assert row.contract_version == ModelSemVer(major=1, minor=0, patch=0)
        assert row.declared_at == _T0
        assert row.last_heartbeat == _T0

    def test_same_renderer_reheartbeat_upserts_not_duplicates(self) -> None:
        s1 = fold_declaration(
            ModelRendererCapabilityProjectionState(), _declaration(), observed_at=_T0
        )
        later = _T0 + timedelta(seconds=30)
        s2 = fold_declaration(s1, _declaration(declared_at=later), observed_at=later)
        assert len(s2.rows) == 1  # upsert, no duplicate
        assert _row(s2, "ui.effect.web").last_heartbeat == later

    def test_distinct_renderers_accumulate(self) -> None:
        s1 = fold_declaration(
            ModelRendererCapabilityProjectionState(),
            _declaration(renderer_id="ui.effect.web"),
            observed_at=_T0,
        )
        s2 = fold_declaration(
            s1, _declaration(renderer_id="ui.effect.cli"), observed_at=_T0
        )
        assert len(s2.rows) == 2
        assert {r.renderer_id for r in s2.rows} == {"ui.effect.web", "ui.effect.cli"}

    def test_fold_is_replay_stable_identity(self) -> None:
        decl = _declaration()
        s1 = fold_declaration(
            ModelRendererCapabilityProjectionState(), decl, observed_at=_T0
        )
        s2 = fold_declaration(s1, decl, observed_at=_T0)
        assert len(s2.rows) == 1
        assert _row(s2, "ui.effect.web").renderer_id == "ui.effect.web"


# ---------------------------------------------------------------------------
# 2. Heartbeat-TTL freshness — is_degraded BOTH directions + typed empty state
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHeartbeatTtlFreshness:
    def test_fresh_heartbeat_is_not_degraded(self) -> None:
        state = fold_declaration(
            ModelRendererCapabilityProjectionState(), _declaration(), observed_at=_T0
        )
        row = _row(state, "ui.effect.web")
        assert row.is_degraded is False
        assert row.empty_state_reason is None

    def test_ttl_expired_heartbeat_is_degraded_with_typed_reason(self) -> None:
        # Observe well past the TTL: the heartbeat is stale.
        observed = _T0 + timedelta(seconds=DEFAULT_HEARTBEAT_TTL_SECONDS + 1)
        state = fold_declaration(
            ModelRendererCapabilityProjectionState(),
            _declaration(declared_at=_T0),
            observed_at=observed,
        )
        row = _row(state, "ui.effect.web")
        assert row.is_degraded is True
        assert row.empty_state_reason == EnumEmptyStateReason.UPSTREAM_BLOCKED

    def test_ttl_boundary_is_still_fresh(self) -> None:
        observed = _T0 + timedelta(seconds=DEFAULT_HEARTBEAT_TTL_SECONDS)
        assert (
            is_heartbeat_degraded(
                last_heartbeat=_T0,
                observed_at=observed,
                ttl_seconds=DEFAULT_HEARTBEAT_TTL_SECONDS,
            )
            is False
        )

    def test_other_renderer_goes_degraded_on_unrelated_heartbeat(self) -> None:
        # web declares at T0; cli declares much later -> the cli heartbeat fold
        # must re-derive web's freshness and flip it to degraded.
        s1 = fold_declaration(
            ModelRendererCapabilityProjectionState(),
            _declaration(renderer_id="ui.effect.web", declared_at=_T0),
            observed_at=_T0,
        )
        later = _T0 + timedelta(seconds=DEFAULT_HEARTBEAT_TTL_SECONDS + 5)
        s2 = fold_declaration(
            s1,
            _declaration(renderer_id="ui.effect.cli", declared_at=later),
            observed_at=later,
        )
        assert _row(s2, "ui.effect.web").is_degraded is True
        assert (
            _row(s2, "ui.effect.web").empty_state_reason
            == EnumEmptyStateReason.UPSTREAM_BLOCKED
        )
        assert _row(s2, "ui.effect.cli").is_degraded is False

    def test_absent_renderer_has_no_row(self) -> None:
        state = fold_declaration(
            ModelRendererCapabilityProjectionState(), _declaration(), observed_at=_T0
        )
        # A renderer that never declared has no row — the consumer treats absence
        # as UPSTREAM_BLOCKED (no blind render); the projection simply has no row.
        assert state.row_for("ui.effect.never-seen") is None


# ---------------------------------------------------------------------------
# 3. Real-dispatch-path — canonical handler output + auto-wiring resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerDispatchPath:
    async def test_handler_emits_reducer_projection(self) -> None:
        handler = HandlerRendererCapabilityProjection()
        envelope = _envelope(_declaration(), observed_at=_T0)
        output = await handler.handle(envelope)
        assert output.node_kind == EnumNodeKind.REDUCER
        assert output.events == ()
        assert output.intents == ()
        assert output.result is None
        assert len(output.projections) == 1
        projected = output.projections[0]
        assert isinstance(projected, ModelRendererCapabilityProjectionState)
        assert _row(projected, "ui.effect.web").is_degraded is False

    async def test_handler_accepts_flattened_mapping_with_prior_state(self) -> None:
        # The live auto-wiring dispatcher delivers the flattened domain payload.
        handler = HandlerRendererCapabilityProjection()
        first = await handler.handle(_envelope(_declaration(), observed_at=_T0))
        prior_state = first.projections[0]
        payload = {
            **_declaration(renderer_id="ui.effect.cli").model_dump(mode="json"),
            "_state": prior_state.model_dump(mode="json"),
        }
        out = await handler.handle(_envelope(payload, observed_at=_T0))
        state = out.projections[0]
        assert {r.renderer_id for r in state.rows} == {
            "ui.effect.web",
            "ui.effect.cli",
        }

    async def test_handler_degraded_path_through_dispatch(self) -> None:
        handler = HandlerRendererCapabilityProjection()
        observed = _T0 + timedelta(seconds=DEFAULT_HEARTBEAT_TTL_SECONDS + 1)
        out = await handler.handle(
            _envelope(_declaration(declared_at=_T0), observed_at=observed)
        )
        row = _row(out.projections[0], "ui.effect.web")
        assert row.is_degraded is True
        assert row.empty_state_reason == EnumEmptyStateReason.UPSTREAM_BLOCKED

    async def test_handler_rejects_unroutable_payload(self) -> None:
        handler = HandlerRendererCapabilityProjection()
        with pytest.raises(TypeError, match="declaration payload"):
            await handler.handle(_envelope(object(), observed_at=_T0))

    async def test_node_resolves_and_dispatches_through_real_contract_loader(
        self,
    ) -> None:
        # Real-dispatch-path proof (not handler isolation): load the contract via
        # the canonical omnibase_core contract_loader.load_contract — the exact
        # loader the runtime uses — resolve the handler class declared in
        # handler_routing via importlib (the auto-wiring resolution path), then
        # instantiate and dispatch a real envelope. A green isolated handler that
        # was never wired into the contract would fail here.
        import importlib

        from omnibase_core.contracts.contract_loader import (
            load_contract,
        )

        contract_path = Path(__file__).resolve().parents[1] / "contract.yaml"
        contract = load_contract(contract_path)

        # Subscribe topic declared in the contract matches the canonical constant.
        event_bus = contract["event_bus"]
        assert isinstance(event_bus, dict)
        assert RENDERER_CAPABILITY_DECLARED_TOPIC_V1 in event_bus["subscribe_topics"]

        routing = contract["handler_routing"]
        assert isinstance(routing, dict)
        assert routing["routing_strategy"] == "operation_match"
        entry = routing["handlers"][0]
        handler_ref = entry["handler"]
        module = importlib.import_module(handler_ref["module"])
        resolved_cls = getattr(module, handler_ref["name"])
        assert resolved_cls is HandlerRendererCapabilityProjection

        # Dispatch through the resolved class exactly as the runtime would.
        resolved_handler = resolved_cls()
        output = await resolved_handler.handle(
            _envelope(_declaration(), observed_at=_T0)
        )
        assert output.node_kind == EnumNodeKind.REDUCER
        assert len(output.projections) == 1
        assert output.projections[0].row_for("ui.effect.web") is not None


# ---------------------------------------------------------------------------
# 4. Topic registration (G-E) + no hardcoded literal in the node path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTopicRegistration:
    def test_topic_constant_is_canonical_and_valid(self) -> None:
        assert (
            RENDERER_CAPABILITY_DECLARED_TOPIC_V1
            == "onex.cmd.ui.renderer-capability-declared.v1"
        )
        result = validate_topic_suffix(RENDERER_CAPABILITY_DECLARED_TOPIC_V1)
        assert result.is_valid is True
        parsed = result.parsed
        assert parsed is not None
        assert parsed.kind == "cmd"
        assert parsed.producer == "ui"
        assert parsed.event_name == "renderer-capability-declared"
        assert parsed.version == 1

    def test_no_hardcoded_topic_literal_in_node_python(self) -> None:
        # G-E: the onex.* command literal must NOT appear in any .py file in the
        # node path (handlers/models/fold). Code references the constant instead.
        node_dir = Path(__file__).resolve().parents[1]
        literal = "onex." + "cmd.ui.renderer-capability-declared.v1"
        offenders: list[str] = []
        for py in node_dir.rglob("*.py"):
            # Test files are allowlisted (they may assert the literal value).
            if py.name.startswith("test_") or py.name == "conftest.py":
                continue
            if literal in py.read_text():
                offenders.append(str(py.relative_to(node_dir)))
        assert offenders == [], f"hardcoded topic literal in node path: {offenders}"
