# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S12 — the dispatch-route identity seam, and a registry row measured stale.

The registry records S12 as a live MISMATCH: ``omnibase_core``'s
``ModelDispatchRoute`` exposes ``handler_id`` while ``omnibase_infra``'s
dispatch engine reads ``dispatcher_id`` behind a ``getattr`` fallback shim.

**Driving the real symbols shows that is no longer true.** In the
``omnibase_core`` version this repo pins, ``ModelDispatchRoute`` ships BOTH
names: ``handler_id`` is the field, ``dispatcher_id`` is a property returning
it, and the field carries
``validation_alias=AliasChoices("handler_id", "dispatcher_id")`` so either
name validates on the way in. The infra shim's own docstring names exactly
this as its removal condition — "Once omnibase_core publishes a release with a
``dispatcher_id`` property this shim can be removed." That condition is met.

This module therefore does NOT construct projections that reproduce a mismatch
the live code does not have — that would be goldening a fiction and would go
green forever while the registry stayed wrong. It drives the real producer and
the real consumer, proves the two names resolve to one identity, and then pins
the registry drift explicitly so it is visible rather than absorbed.

The drift pin fails the moment ``seams.v1.yaml`` is re-derived to MATCHED,
which is the correct signal: delete the pin, the edge is genuinely healthy.
Finding is carried as evidence on the OMN-16004 receipt; re-deriving a
generated registry is a generator run, not a hand-edit, and is out of scope
for a test-only change.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_execution_shape import EnumMessageCategory
from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
from omnibase_infra.runtime.message_dispatch_engine import _get_route_dispatcher_id

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

# ``ModelDispatchRoute`` declares no wire version, so the projection records
# that rather than a made-up "1.0.0" — see harness.UNVERSIONED_MODEL.
_S12_DECLARED_MODEL = (
    "omnibase_core.models.dispatch.model_dispatch_route.ModelDispatchRoute"
)
_S12_KEY_FIELDS: tuple[tuple[str, str], ...] = (("handler_id", "str"),)


def _alias_named_route() -> ModelDispatchRoute:
    """A route validated through INFRA's field name, not core's.

    This is what makes the S12 observation two-sided rather than two readings
    of one object: the producer projection comes from a route built with
    ``handler_id``, the consumer projection from a route that entered through
    the ``dispatcher_id`` alias and was then resolved by the real infra shim.
    """

    return ModelDispatchRoute.model_validate(
        {
            "route_id": "delegation-commands",
            "topic_pattern": _TOPIC,
            "message_category": EnumMessageCategory.COMMAND,
            "dispatcher_id": _HANDLER_ID,
        }
    )


def _core_route(handler_id: str = _HANDLER_ID) -> ModelDispatchRoute:
    """A real core dispatch route, built through the production model."""

    return ModelDispatchRoute(
        route_id="delegation-commands",
        topic_pattern=_TOPIC,
        message_category=EnumMessageCategory.COMMAND,
        handler_id=handler_id,
    )


class _InfraShapedRoute:
    """A route object exposing ONLY infra's field name.

    Stands in for a legacy infra-side route, which is not importable from this
    repo's dependency closure. Used ONLY to prove the shim's preference order;
    every assertion about the actual seam runs against the real
    ``ModelDispatchRoute``.
    """

    def __init__(self, dispatcher_id: str) -> None:
        self.route_id = "delegation-commands"
        self.dispatcher_id = dispatcher_id


class TestProducerExposesBothNames:
    """The measured producer side — core reconciled the seam itself."""

    def test_slice_row_is_a_traversed_local_processing_edge(self) -> None:
        edge = slice_edge("S12")
        assert edge.traversed
        assert edge.producer_symbol_reachable

    def test_handler_id_is_the_declared_field(self) -> None:
        assert "handler_id" in ModelDispatchRoute.model_fields

    def test_dispatcher_id_is_available_as_a_property_alias(self) -> None:
        """The exact symbol the infra shim's removal condition names."""

        route = _core_route()
        assert route.dispatcher_id == _HANDLER_ID

    def test_the_two_names_are_one_identity_not_two_values(self) -> None:
        """A property that could drift from its field would be a worse seam."""

        route = _core_route("some-other-handler")
        assert route.dispatcher_id == route.handler_id == "some-other-handler"

    def test_either_name_validates_on_input_via_alias_choices(self) -> None:
        """``dispatcher_id`` is accepted on the wire, not merely readable."""

        by_alias = ModelDispatchRoute.model_validate(
            {
                "route_id": "delegation-commands",
                "topic_pattern": _TOPIC,
                "message_category": EnumMessageCategory.COMMAND,
                "dispatcher_id": _HANDLER_ID,
            }
        )

        assert by_alias.handler_id == _HANDLER_ID
        assert by_alias.dispatcher_id == _HANDLER_ID


class TestConsumerResolvesTheRealProducer:
    """The real infra dispatch-engine resolver against the real core route."""

    def test_shim_resolves_a_core_route(self) -> None:
        assert _get_route_dispatcher_id(_core_route()) == _HANDLER_ID

    def test_identity_is_preserved_across_the_seam(self) -> None:
        """The identity-seam analogue of correlation preservation.

        The value the producer set must be the value the consumer resolves —
        a shape match that resolved to a different (or empty) handler id would
        route commands to the wrong node while looking perfectly healthy.
        """

        route = _core_route("delegation-terminal-handler")
        resolved = _get_route_dispatcher_id(route)

        assert resolved == route.handler_id
        assert resolved == route.dispatcher_id

    def test_shim_still_resolves_a_legacy_infra_shaped_route(self) -> None:
        assert _get_route_dispatcher_id(_InfraShapedRoute("infra-handler")) == (
            "infra-handler"
        )

    def test_shim_raises_rather_than_defaulting_when_neither_name_exists(self) -> None:
        """Fail-closed check: an unresolvable route must not dispatch anywhere."""

        class _NamelessRoute:
            route_id = "nameless"

        with pytest.raises(
            AttributeError, match="neither dispatcher_id nor handler_id"
        ):
            _get_route_dispatcher_id(_NamelessRoute())


class TestRegistryRowIsMeasuredStale:
    """Pin the drift between the registry's MISMATCH and the live symbols."""

    def test_registry_still_records_a_mismatch(self) -> None:
        assert registry_classification("S12") == "MISMATCH"

    def test_the_live_seam_matches_despite_that_record(self) -> None:
        """Both observed sides resolve to one identity, so leg 1 passes.

        The two observed projections come from two different code paths, not
        from the declaration and not from one shared object: the producer from
        a ``ModelDispatchRoute`` built through core's own ``handler_id`` field,
        the consumer from a route that entered through infra's ``dispatcher_id``
        alias and was then read by the real
        ``_get_route_dispatcher_id`` shim. If core dropped the alias, or the
        shim stopped resolving, the consumer leg goes red here — which the
        previous ``observed=declared`` form could not detect.
        """

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

        core_named_route = _core_route()
        alias_named_route = _alias_named_route()
        resolved_identity = _get_route_dispatcher_id(alias_named_route)

        verdict = run_registry_match(
            edge_id="S12",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=observed_projection_from_instance(
                edge_id="S12",
                role=EnumSeamProjectionRole.PRODUCER,
                topic=core_named_route.topic_pattern,
                instance=core_named_route,
                field_names=("handler_id",),
            ),
            observed_consumer=observed_projection_from_mapping(
                edge_id="S12",
                role=EnumSeamProjectionRole.CONSUMER,
                topic=alias_named_route.topic_pattern,
                mapping={"handler_id": resolved_identity},
                field_names=("handler_id",),
                envelope_model=model_identity(type(alias_named_route)),
            ),
        )

        assert verdict.verdict.value == "MATCHED"
        assert_regenerable("S12", verdict)
        # The drift, stated as an assertion rather than a comment: re-deriving
        # seams.v1.yaml from a source that reflects the pinned core version
        # will flip the row and fail `test_registry_still_records_a_mismatch`.
        assert verdict.verdict.value != registry_classification("S12")

    def test_a_consumer_that_resolved_nothing_would_fail_the_observed_leg(
        self,
    ) -> None:
        """Negative control on leg 3 specifically.

        The failure this edge exists to catch is the shim resolving to a
        different or empty identity while both declarations still look aligned.
        Observing a ``None`` resolution turns the consumer key field's type from
        ``str`` to ``NoneType`` and reddens leg 3 alone.
        """

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

    def test_the_shim_removal_condition_named_in_infra_is_satisfied(self) -> None:
        """State the remediation the drift implies, executably.

        ``_get_route_dispatcher_id``'s docstring: "Once omnibase_core publishes
        a release with a ``dispatcher_id`` property this shim can be removed."
        """

        assert isinstance(getattr(type(_core_route()), "dispatcher_id", None), property)


class TestIdentityPreservationUnderBothNames:
    """The seam must carry one id, whichever name each side happens to use."""

    @pytest.mark.parametrize(
        "handler_id",
        ["delegation-command-handler", "delegation-terminal-handler", "a"],
    )
    def test_producer_id_is_observed_unchanged_at_the_consumer(
        self, handler_id: str
    ) -> None:
        route = _core_route(handler_id)

        assert _get_route_dispatcher_id(route) == handler_id

    def test_alias_input_round_trips_to_the_same_resolved_identity(self) -> None:
        """An infra-named input must resolve to the same id a core-named one does."""

        by_name = _core_route()
        by_alias = ModelDispatchRoute.model_validate(
            {
                "route_id": "delegation-commands",
                "topic_pattern": _TOPIC,
                "message_category": EnumMessageCategory.COMMAND,
                "dispatcher_id": _HANDLER_ID,
            }
        )

        assert _get_route_dispatcher_id(by_name) == _get_route_dispatcher_id(by_alias)


class TestCorrelationHelperRejectsIdentityLoss:
    """Guard the guard: the correlation assertion must be able to fail."""

    def test_a_rewritten_identity_is_reported_not_absorbed(self) -> None:
        from uuid import uuid4

        emitted = uuid4()
        with pytest.raises(AssertionError, match="was rewritten across the seam"):
            assert_correlation_preserved(
                edge_id="S12", emitted=emitted, observed=uuid4()
            )
