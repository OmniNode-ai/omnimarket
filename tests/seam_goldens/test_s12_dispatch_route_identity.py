# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S12 — dispatch routes have one canonical handler identity.

``ModelDispatchRoute.handler_id`` is the sole route identity from Core through
Infra. The former ``dispatcher_id`` alias/property and Infra compatibility
resolver are deliberately absent: accepting a legacy route payload would let a
consumer silently select a different contract shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.enums.enum_execution_shape import EnumMessageCategory
from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
from pydantic import ValidationError

from tests.seam_goldens.harness import (
    UNVERSIONED_MODEL,
    EnumSeamProjectionRole,
    assert_correlation_preserved,
    assert_regenerable,
    consumer_projection,
    model_identity,
    observed_projection_from_instance,
    observed_projection_from_mapping,
    producer_projection,
    registry_classification,
    run_registry_match,
)
from tests.seam_goldens.manifest import slice_edge

pytestmark = pytest.mark.unit

_HANDLER_ID = "delegation-command-handler"
_TOPIC = "onex.cmd.omnibase-infra.delegation-request.v1"
_S12_DECLARED_MODEL = (
    "omnibase_core.models.dispatch.model_dispatch_route.ModelDispatchRoute"
)
_S12_KEY_FIELDS: tuple[tuple[str, str], ...] = (("handler_id", "str"),)


def _core_route(handler_id: str = _HANDLER_ID) -> ModelDispatchRoute:
    """Build a route through Core's only accepted identity field."""

    return ModelDispatchRoute(
        route_id="delegation-commands",
        topic_pattern=_TOPIC,
        message_category=EnumMessageCategory.COMMAND,
        handler_id=handler_id,
    )


def _core_enforces_handler_id_only() -> bool:
    """Return whether the installed Core has removed its retired route alias."""

    return not hasattr(_core_route(), "dispatcher_id")


_REQUIRES_HANDLER_ID_ONLY_CORE = pytest.mark.skipif(
    not _core_enforces_handler_id_only(),
    reason="requires Core ModelDispatchRoute without the retired dispatcher_id alias",
)


class TestRouteIdentityContract:
    """The real producer and consumer agree on the canonical field only."""

    def test_slice_row_is_a_traversed_local_processing_edge(self) -> None:
        edge = slice_edge("S12")
        assert edge.traversed
        assert edge.producer_symbol_reachable

    @_REQUIRES_HANDLER_ID_ONLY_CORE
    def test_handler_id_is_the_only_declared_route_identity(self) -> None:
        assert "handler_id" in ModelDispatchRoute.model_fields
        assert "dispatcher_id" not in ModelDispatchRoute.model_fields
        assert not hasattr(_core_route(), "dispatcher_id")

    @_REQUIRES_HANDLER_ID_ONLY_CORE
    def test_legacy_dispatcher_id_payload_is_rejected(self) -> None:
        """No alias or extra-field compatibility path may revive the old shape."""

        with pytest.raises(ValidationError) as error:
            ModelDispatchRoute.model_validate(
                {
                    "route_id": "delegation-commands",
                    "topic_pattern": _TOPIC,
                    "message_category": EnumMessageCategory.COMMAND,
                    "dispatcher_id": _HANDLER_ID,
                }
            )

        error_types = {item["type"] for item in error.value.errors()}
        assert {"missing", "extra_forbidden"} <= error_types

    def test_consumer_reads_the_real_handler_id_directly(self) -> None:
        route = _core_route("delegation-terminal-handler")

        assert route.handler_id == "delegation-terminal-handler"

    def test_omnimarket_route_wiring_has_no_legacy_identity_access(self) -> None:
        """Keep the consumer on handler_id while the published Core catches up."""

        repo_root = Path(__file__).resolve().parents[2]
        source = (
            repo_root / "src/omnimarket/nodes/node_delegation_orchestrator/wiring.py"
        ).read_text(encoding="utf-8")

        assert "_get_route_dispatcher_id" not in source
        route_calls = source.split("ModelDispatchRoute(")[1:]
        assert route_calls
        for route_call in route_calls:
            route_constructor = route_call.split(")", maxsplit=1)[0]
            assert "dispatcher_id=" not in route_constructor
            assert "handler_id=" in route_constructor


class TestRegistryRowAgreesWithTheLiveSymbol:
    """The registry records the same one-field identity seam."""

    def test_registry_records_the_edge_as_matched(self) -> None:
        assert registry_classification("S12") == "MATCHED"

    def test_live_seam_matches_and_record_agrees(self) -> None:
        declared_producer = producer_projection(
            edge_id="S12",
            topic=_TOPIC,
            envelope_model=_S12_DECLARED_MODEL,
            envelope_version=UNVERSIONED_MODEL,
            key_fields=_S12_KEY_FIELDS,
        )
        declared_consumer = consumer_projection(
            edge_id="S12",
            topic=_TOPIC,
            envelope_model=_S12_DECLARED_MODEL,
            envelope_version=UNVERSIONED_MODEL,
            key_fields=_S12_KEY_FIELDS,
        )
        route = _core_route()

        verdict = run_registry_match(
            edge_id="S12",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=observed_projection_from_instance(
                edge_id="S12",
                role=EnumSeamProjectionRole.PRODUCER,
                topic=route.topic_pattern,
                instance=route,
                field_names=("handler_id",),
            ),
            observed_consumer=observed_projection_from_mapping(
                edge_id="S12",
                role=EnumSeamProjectionRole.CONSUMER,
                topic=route.topic_pattern,
                mapping={"handler_id": route.handler_id},
                field_names=("handler_id",),
                envelope_model=model_identity(type(route)),
            ),
        )

        assert verdict.verdict.value == "MATCHED"
        assert_regenerable("S12", verdict)
        assert verdict.verdict.value == registry_classification("S12")

    def test_missing_consumer_identity_fails_the_observed_leg(self) -> None:
        declared_producer = producer_projection(
            edge_id="S12",
            topic=_TOPIC,
            envelope_model=_S12_DECLARED_MODEL,
            envelope_version=UNVERSIONED_MODEL,
            key_fields=_S12_KEY_FIELDS,
        )
        declared_consumer = consumer_projection(
            edge_id="S12",
            topic=_TOPIC,
            envelope_model=_S12_DECLARED_MODEL,
            envelope_version=UNVERSIONED_MODEL,
            key_fields=_S12_KEY_FIELDS,
        )
        route = _core_route()

        verdict = run_registry_match(
            edge_id="S12",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=observed_projection_from_instance(
                edge_id="S12",
                role=EnumSeamProjectionRole.PRODUCER,
                topic=route.topic_pattern,
                instance=route,
                field_names=("handler_id",),
            ),
            observed_consumer=observed_projection_from_mapping(
                edge_id="S12",
                role=EnumSeamProjectionRole.CONSUMER,
                topic=route.topic_pattern,
                mapping={"handler_id": None},
                field_names=("handler_id",),
                envelope_model=_S12_DECLARED_MODEL,
            ),
        )

        assert verdict.leg2_observed_producer_vs_declared.passed is True
        assert verdict.leg3_observed_consumer_vs_declared.passed is False
        assert verdict.regenerability.value == "SHAPE_ONLY"


class TestIdentityPreservation:
    """One handler identity is preserved without a legacy alias."""

    @pytest.mark.parametrize(
        "handler_id",
        ["delegation-command-handler", "delegation-terminal-handler", "a"],
    )
    def test_producer_id_is_observed_unchanged_at_consumer(
        self, handler_id: str
    ) -> None:
        assert _core_route(handler_id).handler_id == handler_id


class TestCorrelationHelperRejectsIdentityLoss:
    """Guard the guard: the correlation assertion must be able to fail."""

    def test_rewritten_identity_is_reported_not_absorbed(self) -> None:
        from uuid import uuid4

        emitted = uuid4()
        with pytest.raises(AssertionError, match="was rewritten across the seam"):
            assert_correlation_preserved(
                edge_id="S12", emitted=emitted, observed=uuid4()
            )
