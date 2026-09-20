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
