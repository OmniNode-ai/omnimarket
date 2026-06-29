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
  3. Real-dispatch-path — the canonical projection-runner protocol
     handler.handle(input_data) -> dict UPSERTs the folded rows through the
     injected DatabaseAdapter (the exact path the runtime wires for a db_io
     projection), and the node resolves through the real auto-wiring contract
     loader (not handler isolation alone).
  4. G-E no-hardcoded-topic — the canonical onex.* command topic literal does NOT
     appear in any .py file in the node path; code references the constant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from omnibase_core.enums.enum_accessibility_tier import EnumAccessibilityTier
from omnibase_core.enums.enum_empty_state_reason import EnumEmptyStateReason
from omnibase_core.enums.enum_renderer_interaction_model import (
    EnumRendererInteractionModel,
)
from omnibase_core.enums.enum_widget_type import EnumWidgetType
from omnibase_core.models.dashboard.model_renderer_capability_contract import (
    ModelRendererCapabilityContract,
)
from omnibase_core.models.primitives.model_semver import ModelSemVer
from omnibase_core.validation.validator_topic_suffix import validate_topic_suffix

from omnimarket.events.topics import RENDERER_CAPABILITY_DECLARED_TOPIC_V1
from omnimarket.nodes.node_renderer_capability_projection.handlers.handler_renderer_capability_projection import (
    TABLE,
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
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

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
# 3. Real-dispatch-path — projection-runner materialization + auto-wiring resolution
#
# The contract declares db_io.db_tables + projection_api, so the runtime wires
# this node through the projection dispatch path: it delivers the flattened
# declaration payload + an injected DatabaseAdapter under input_data['_db'] and
# calls handle(input_data), which UPSERTs the folded rows. These tests drive that
# exact protocol (not the prior handle(envelope) -> ModelHandlerOutput shape that
# the runtime never invokes for a db_io projection and that crashed live on
# dict.payload).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerDispatchPath:
    def test_handle_materializes_one_row(self) -> None:
        handler = HandlerRendererCapabilityProjection()
        db = InmemoryDatabaseAdapter()
        # handle() takes the runtime payload (no observed_at) and uses the wall
        # clock as the observer, so a fresh heartbeat (declared_at=now) is the
        # not-degraded case — mirroring the live W-cap producer (declared_at=now).
        out = handler.handle(
            {
                **_declaration(declared_at=datetime.now(tz=UTC)).model_dump(
                    mode="json"
                ),
                "_db": db,
                "_event_type": RENDERER_CAPABILITY_DECLARED_TOPIC_V1,
            }
        )
        assert out["rows_upserted"] == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["renderer_id"] == "ui.effect.web"
        assert rows[0]["is_degraded"] is False
        assert rows[0]["empty_state_reason"] is None

    def test_project_accumulates_across_heartbeats(self) -> None:
        # The durable accumulator is the table itself: a second renderer's
        # heartbeat folds onto the rows already persisted, keeping both rows.
        handler = HandlerRendererCapabilityProjection()
        db = InmemoryDatabaseAdapter()
        handler.project(_declaration(renderer_id="ui.effect.web"), db, observed_at=_T0)
        handler.project(_declaration(renderer_id="ui.effect.cli"), db, observed_at=_T0)
        rows = db.query(TABLE)
        assert {r["renderer_id"] for r in rows} == {"ui.effect.web", "ui.effect.cli"}

    def test_reheartbeat_upserts_not_duplicates(self) -> None:
        handler = HandlerRendererCapabilityProjection()
        db = InmemoryDatabaseAdapter()
        handler.project(_declaration(declared_at=_T0), db, observed_at=_T0)
        later = _T0 + timedelta(seconds=30)
        handler.project(_declaration(declared_at=later), db, observed_at=later)
        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["last_heartbeat"] == later.isoformat()

    def test_degraded_path_persists_typed_reason(self) -> None:
        handler = HandlerRendererCapabilityProjection()
        db = InmemoryDatabaseAdapter()
        observed = _T0 + timedelta(seconds=DEFAULT_HEARTBEAT_TTL_SECONDS + 1)
        handler.project(_declaration(declared_at=_T0), db, observed_at=observed)
        row = db.query(TABLE)[0]
        assert row["is_degraded"] is True
        assert row["empty_state_reason"] == EnumEmptyStateReason.UPSTREAM_BLOCKED.value

    def test_handle_requires_db_adapter(self) -> None:
        handler = HandlerRendererCapabilityProjection()
        with pytest.raises(TypeError, match="DatabaseAdapter"):
            handler.handle(_declaration().model_dump(mode="json"))

    def test_node_resolves_and_materializes_through_real_contract_loader(
        self,
    ) -> None:
        # Real-dispatch-path proof (not handler isolation): load the contract via
        # the canonical omnibase_core contract_loader.load_contract — the exact
        # loader the runtime uses — resolve the handler class declared in
        # handler_routing via importlib (the auto-wiring resolution path), then
        # instantiate and drive its projection-runner protocol. A green isolated
        # handler that was never wired into the contract would fail here.
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

        # The contract declares the projection materialization surface the runtime
        # uses to wire this node through the projection dispatch path.
        db_io = contract["db_io"]
        assert isinstance(db_io, dict)
        assert TABLE in {t["name"] for t in db_io["db_tables"]}

        routing = contract["handler_routing"]
        assert isinstance(routing, dict)
        assert routing["routing_strategy"] == "operation_match"
        entry = routing["handlers"][0]
        handler_ref = entry["handler"]
        module = importlib.import_module(handler_ref["module"])
        resolved_cls = getattr(module, handler_ref["name"])
        assert resolved_cls is HandlerRendererCapabilityProjection

        # Materialize through the resolved class exactly as the runtime would.
        db = InmemoryDatabaseAdapter()
        out = resolved_cls().handle(
            {
                **_declaration(declared_at=_T0).model_dump(mode="json"),
                "_db": db,
                "_event_type": RENDERER_CAPABILITY_DECLARED_TOPIC_V1,
            }
        )
        assert out["rows_upserted"] == 1
        assert db.query(TABLE)[0]["renderer_id"] == "ui.effect.web"


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
