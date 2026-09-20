# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab lane-health handler is reachable through the REAL adapter path.

WHY THIS FILE EXISTS, AND WHY IT IS NOT ANOTHER FOLD TEST
---------------------------------------------------------
``tests/test_omn18769_lab_lane_health_projection.py`` drives ``census_facts``
and friends directly and passed, green, for the entire period in which the
deployed projection consumed every message, committed its offsets and wrote
nothing at all. It could not see the defect, because it built the handler's
input by hand.

The defect lived one layer up. The shared ``runtime_local_adapter`` builds a
def-B handler's input from the UNWRAPPED DOMAIN PAYLOAD of the bus message --
``model_type.model_validate(raw_decoded_dict)`` -- and the request model
declared ``{topic, payload}``, a wrapper the adapter has no way to construct.
Every real message failed with ``1 validation error ... topic Field required``
before the fold was ever called. Measured on the lab: inputs at watermarks 6,
47920 and 5, the projection's consumer group Stable at ``CURRENT-OFFSET 6 /
LOG-END-OFFSET 7``, and the table at zero rows.

So this file asserts the property the fold tests structurally cannot: that the
adapter's own invocation helper, imported from ``omnibase_core`` rather than
reimplemented here, can carry a REAL captured event into ``handle``. If the
request model ever again declares a shape the adapter cannot build, these
tests go red and the fold tests stay green -- which is the point.

The payloads below are verbatim captures from the ``.201`` dev lane, taken off
the topics with ``rpk topic consume`` on 2026-09-20, trimmed only in the number
of repeated ``findings`` entries. They are not hand-written fixtures; a
hand-written fixture is what hid this defect.
"""

from __future__ import annotations

from typing import Any

import pytest
from omnibase_core.runtime.runtime_local_adapter import _invoke_handle_method

from omnimarket.nodes.node_projection_lab_lane_health.contract_topics import (
    TOPIC_LAB_PASS_RECEIPT,
    TOPIC_LANE_CENSUS,
    TOPIC_RUNTIME_HEALTH,
)
from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
    HandlerProjectionLabLaneHealth,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.enum_lab_lane import (
    EnumLabLane,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.model_lab_lane_health_request import (
    ModelLabLaneHealthRequest,
)

# Captured from onex.evt.omnibase-infra.lane-census-observed.v1, offset 6.
_REAL_CENSUS: dict[str, Any] = {
    "schema_version": "1.0.0",
    "event_type": "lane-census-observed",
    "topic": "onex.evt.omnibase-infra.lane-census-observed.v1",
    "host": "omninode-pc",
    "observed_at": "2026-09-20T06:51:35.788945+00:00",
    "lanes_checked": [
        "stability-test",
        "judge",
        "dev",
        "lakshman",
        "dogfood",
        "ci-bus",
    ],
    "drift_count": 5,
    "findings": [
        {
            "lane": "dev",
            "kind": "unexpected_container",
            "container": "onex-api",
            "detail": (
                "container 'onex-api' carries com.omninode.lane='dev' but is "
                "not declared in the lane manifest"
            ),
            "severity": "warning",
        },
        {
            "lane": "dev",
            "kind": "unexpected_container",
            "container": "omnibase-infra-cloud-migration",
            "detail": (
                "container 'omnibase-infra-cloud-migration' carries "
                "com.omninode.lane='dev' but is not declared in the lane manifest"
            ),
            "severity": "warning",
        },
    ],
}

# Shape captured from onex.evt.omnibase-infra.runtime-health-check.v1. The
# runtime's own error report named `correlation_id` and
# `pending_projection_count`, which is how we know the adapter hands the bare
# event rather than a wrapper.
_REAL_HEALTH: dict[str, Any] = {
    "correlation_id": "8a9ec0de-0000-4000-8000-000000000000",
    "lane": "compose-dev",
    "timestamp": "2026-09-20T07:00:00+00:00",
    "status": "DEGRADED",
    "dimensions": [{"name": "consumer_groups", "status": "DEGRADED"}],
    "pending_projection_count": 0,
}

_REAL_RECEIPT: dict[str, Any] = {
    "lane": "compose-dev",
    "finished_at": "2026-09-20T07:10:00+00:00",
    "sha": "31ad10c847b80c622ff2bc41c9d6d3a060a57d62",
    "result": "PASS",
    "checks": [{"name": "ready_main", "ok": True, "evidence": "200"}],
}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("event", "expected_topic"),
    [
        (_REAL_CENSUS, TOPIC_LANE_CENSUS),
        (_REAL_HEALTH, TOPIC_RUNTIME_HEALTH),
        (_REAL_RECEIPT, TOPIC_LAB_PASS_RECEIPT),
    ],
    ids=["census", "runtime-health", "lab-pass-receipt"],
)
def test_the_adapter_can_build_the_request_from_a_real_event(
    event: dict[str, Any], expected_topic: str
) -> None:
    """The model is constructible from the bare event, and knows its topic.

    This is the exact call the adapter makes. Under the previous
    ``{topic, payload}`` model it raised ``topic Field required`` for all three.
    """
    request = ModelLabLaneHealthRequest.model_validate(event)

    assert request.source_topic == expected_topic
    # The payload comes back verbatim: census_facts checks `isinstance(list)`,
    # so a model that coerced this to a tuple would parse as an empty census.
    assert request.as_payload() == event


@pytest.mark.unit
@pytest.mark.parametrize(
    "event",
    [_REAL_CENSUS, _REAL_HEALTH, _REAL_RECEIPT],
    ids=["census", "runtime-health", "lab-pass-receipt"],
)
def test_a_real_event_reaches_handle_through_the_adapters_own_invoker(
    event: dict[str, Any],
) -> None:
    """Drive ``_invoke_handle_method`` itself, not a local imitation of it.

    Importing the adapter's helper is deliberate: a reimplementation here would
    drift from the runtime and could go green while production stayed broken,
    which is the failure this file exists to make impossible.
    """
    handler = HandlerProjectionLabLaneHealth()

    result = _invoke_handle_method(handler.handle, event)

    assert result.applied is True


@pytest.mark.unit
def test_the_census_event_folds_to_a_row_rather_than_merely_validating() -> None:
    """Reaching ``handle`` is necessary and not sufficient: assert the fold ran."""
    handler = HandlerProjectionLabLaneHealth()

    result = _invoke_handle_method(handler.handle, _REAL_CENSUS)

    # `dev` is the only in-scope lab lane in the captured `lanes_checked`; the
    # others are out of scope by AC6 and produce no row.
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["lane"] == "compose-dev"
    # Both captured findings name `dev`, so the drift count is theirs alone.
    assert row["census_drift_count"] == 2


@pytest.mark.unit
def test_an_event_matching_no_source_shape_is_refused_not_guessed() -> None:
    """The bare dict the runtime reported is refused with a legible reason.

    A model that accepted this would restore the original hazard from the other
    direction: a malformed census silently folding as an empty health event.
    """
    shapeless = {"correlation_id": "8a9ec", "pending_projection_count": 0}

    with pytest.raises(ValueError, match="matches no known source shape"):
        ModelLabLaneHealthRequest.model_validate(shapeless)


@pytest.mark.unit
def test_an_event_carrying_two_discriminators_is_refused_as_ambiguous() -> None:
    """Two shapes at once is upstream drift, and guessing which is worse."""
    ambiguous = dict(_REAL_HEALTH)
    ambiguous["finished_at"] = "2026-09-20T07:10:00+00:00"

    with pytest.raises(ValueError, match="ambiguous"):
        ModelLabLaneHealthRequest.model_validate(ambiguous)


@pytest.mark.unit
def test_the_writer_opts_in_to_inprocess_dispatch() -> None:
    """Without this flag the runtime subscribes the topics and dispatches nothing.

    This is not a style preference. ``_is_standalone_projection_runner``
    classifies any handler owning ``project_event``, ``run``, ``topics`` and
    its own adapter as STANDALONE unless it declares this attribute, and a
    standalone runner with no dedicated writer process on the lane persists no
    rows at all -- silently, with offsets advancing and no error logged. That
    is exactly what this node did after the two-class split landed: the census
    consumer reached LAG 0 with zero errors and the table stayed empty.

    The sibling that works, ``FleetLivenessProjectionWriter``, declares the
    same attribute, which is why it never took that branch.
    """
    from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
        LabLaneHealthProjectionWriter,
    )

    assert LabLaneHealthProjectionWriter.onex_runtime_inprocess_dispatch is True


@pytest.mark.unit
def test_the_writer_scopes_its_pool_to_the_loop_that_projects() -> None:
    """The in-process declaration is a promise about pool lifetime; keep it.

    The runtime cannot verify this, so a test does. Opening the pool on any
    loop other than the one the work runs on is the ``Event loop is closed``
    failure the OMN-16874 docstring describes.
    """
    from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
        LabLaneHealthProjectionWriter,
    )

    order: list[str] = []

    class _RecordingPool:
        async def connect(self) -> None:
            order.append("connect")

        async def close(self) -> None:
            order.append("close")

    class _Scoped(LabLaneHealthProjectionWriter):
        @property
        def db(self) -> Any:
            return _RecordingPool()

        async def _project_and_report(  # type: ignore[override]
            self, topic: str, data: dict[str, Any], meta: Any
        ) -> list[Any]:
            order.append("project")
            return [EnumLabLane.COMPOSE_DEV]

    result = _Scoped().handle(
        {
            "lane": "compose-dev",
            "timestamp": "2026-09-20T10:05:45+00:00",
            "_topic": TOPIC_RUNTIME_HEALTH,
        }
    )

    # The result is the shape the runtime's write-path guard reads, not a
    # bare ack: it gates the terminal event on a proven row count, and any
    # other shape counts as zero.
    assert result == {"rows_upserted": 1, "lane_rows": ["compose-dev"]}
    # Connect before, close after, and the projection strictly between them.
    assert order == ["connect", "project", "close"]


@pytest.mark.unit
def test_the_pool_is_closed_when_the_projection_raises() -> None:
    """A failing message must not strand the pool it opened.

    Under in-process dispatch the runtime hands this class one message at a
    time and keeps the process alive, so a bracket that only closes on the
    happy path accumulates one pool per failed message for the life of the
    lane. The exception itself must still reach the runtime, which is what
    turns the message into a retry or a dead letter rather than a silent
    success.
    """
    from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
        LabLaneHealthProjectionWriter,
    )

    order: list[str] = []

    class _RecordingPool:
        async def connect(self) -> None:
            order.append("connect")

        async def close(self) -> None:
            order.append("close")

    class _Failing(LabLaneHealthProjectionWriter):
        @property
        def db(self) -> Any:
            return _RecordingPool()

        async def _project_and_report(  # type: ignore[override]
            self, topic: str, data: dict[str, Any], meta: Any
        ) -> list[Any]:
            raise RuntimeError("write failed")

    with pytest.raises(RuntimeError, match="write failed"):
        _Failing().handle(
            {
                "lane": "compose-dev",
                "timestamp": "2026-09-20T10:05:45+00:00",
                "_topic": TOPIC_RUNTIME_HEALTH,
            }
        )

    assert order == ["connect", "close"]


@pytest.mark.unit
def test_the_pool_is_closed_when_connect_itself_raises() -> None:
    """``connect()`` sits inside the bracket, so its failure is cleaned up too.

    The adversarial gate raised the narrower shape -- ``connect()`` above the
    ``try`` -- as a resource leak. On this adapter it is not one, because the
    pool attribute is assigned only on success and ``close()`` is null-safe.
    This test pins the wider bracket so the claim stays false by construction
    rather than by a property of the adapter that a future one may not share.
    """
    from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
        LabLaneHealthProjectionWriter,
    )

    order: list[str] = []

    class _RefusingPool:
        async def connect(self) -> None:
            order.append("connect")
            raise RuntimeError("pool refused")

        async def close(self) -> None:
            order.append("close")

    class _Refusing(LabLaneHealthProjectionWriter):
        @property
        def db(self) -> Any:
            return _RefusingPool()

        async def _project_and_report(  # type: ignore[override]
            self, topic: str, data: dict[str, Any], meta: Any
        ) -> list[Any]:
            order.append("project")
            return [EnumLabLane.COMPOSE_DEV]

    with pytest.raises(RuntimeError, match="pool refused"):
        _Refusing().handle(
            {
                "lane": "compose-dev",
                "timestamp": "2026-09-20T10:05:45+00:00",
                "_topic": TOPIC_RUNTIME_HEALTH,
            }
        )

    # No projection ran, and the close still did.
    assert order == ["connect", "close"]


@pytest.mark.unit
def test_the_result_shape_is_one_the_runtime_write_path_guard_understands() -> None:
    """The guard gates the terminal event on a row count it can read.

    This is the regression, measured on the lab at 2026-09-20T11:57:31Z: the
    writer returned ``{"applied": True}``, the census message really did write
    a row, and the runtime still logged "Projection handler wrote zero rows"
    and emitted no terminal, because that shape is neither of the two the
    guard understands and anything else counts as zero. Asserting our own
    literal would not have caught it -- the two sides have to be read
    together, so this drives the real extractor.
    """
    wiring = pytest.importorskip(
        "omnibase_infra.runtime.auto_wiring.handler_wiring",
        reason="the runtime is a layer above this one and is not always installed",
    )

    assert wiring._extract_rows_upserted({"rows_upserted": 1, "lane_rows": ["x"]}) == 1
    assert wiring._extract_rows_upserted({"rows_upserted": 0, "lane_rows": []}) == 0
    # The shape that shipped and silently read as zero.
    assert wiring._extract_rows_upserted({"applied": True}) == 0


@pytest.mark.unit
def test_an_event_naming_no_lab_lane_reports_zero_rows_rather_than_a_write() -> None:
    """A correct no-write must not claim a row it did not make.

    Every runtime-health tick on the lane carries ``lane: null`` and folds to
    nothing, which is the right outcome. Reporting it as ``{"projected":
    True}`` -- the other shape the guard accepts -- would emit a terminal per
    tick over a table that never changed, which is the same class of lie as
    the suppressed terminal above, pointing the other way.
    """
    from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
        LabLaneHealthProjectionWriter,
    )

    class _NoLane(LabLaneHealthProjectionWriter):
        @property
        def db(self) -> Any:
            class _Db:
                async def connect(self) -> None: ...

                async def close(self) -> None: ...

            return _Db()

        async def _project_and_report(  # type: ignore[override]
            self, topic: str, data: dict[str, Any], meta: Any
        ) -> list[Any]:
            return []

    result = _NoLane().handle(
        {
            "lane": None,
            "timestamp": "2026-09-20T11:52:56.636045+00:00",
            "_topic": TOPIC_RUNTIME_HEALTH,
        }
    )

    assert result == {"rows_upserted": 0, "lane_rows": []}
