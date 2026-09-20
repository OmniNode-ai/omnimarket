# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18880: the emitter's REAL payload must parse against the consumer model.

Why this file exists rather than another unit test over the request model: the
node already had those, and they passed, because every one of them built the
request the way the model wanted it rather than the way the emitter sends it.
That is the OMN-18868 class — a producer and a consumer each correct at their
own sha and never exercised together — and the only test that can catch it is
one driven by a payload captured off the live topic.

The fixture below is a verbatim message from
``onex.evt.omnibase-infra.runner-fleet.v1`` on the ``.201`` dev lane, captured
2026-09-20T13:30:08Z. Only the ``runners`` list is abridged, from 69 entries to
three chosen to cover the distinct shapes: a runner whose host label differs
from the observing host, one where they agree, and a busy one. The busy entry
is lifted from the 13:36:07Z message in the same capture, because no runner in
the 13:30:08Z one was anything but online; its ``observed_at`` is therefore its
own and differs from the envelope's, which is itself the per-runner freshness
the row carries. Every top-level key is present exactly as sent, because the
top-level key SET is the thing that was wrong.

Two values ARE altered and nothing else: the observing host's real name is a
tailnet FQDN, which the leaked-literals gate blocks from source, so it reads
``observer-host.invalid`` here. The gate is right and this file is not the
place to argue with it -- the defect was never about that string's value, only
about which level of the document it sits at, and that is preserved exactly.

An earlier draft of this file invented the busy runner instead of finding one,
and gave it an integer ``current_job_id``. The model refused it, correctly --
the field is a string -- and the live emitter turns out to send ``null`` there
even for a BUSY runner, so the handler's "drop a job id on an idle runner"
branch never fires in production. That is a separate observation and not this
ticket; it is recorded here because inventing the fixture is what hid it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_runner_fleet.handlers.handler_projection_runner_fleet import (
    HandlerProjectionRunnerFleet,
)
from omnimarket.nodes.node_projection_runner_fleet.models.model_runner_fleet_observation_wire import (
    ModelRunnerFleetObservationWire,
)
from omnimarket.nodes.node_projection_runner_fleet.models.model_runner_fleet_projection_request import (
    ModelRunnerFleetProjectionRequest,
)

pytestmark = pytest.mark.unit

#: Verbatim off the topic; see the module docstring for the one abridgement.
LIVE_EVENT: dict[str, Any] = {
    "schema_version": "1.0.0",
    "event_type": "runner-fleet-observation",
    "topic": "onex.evt.omnibase-infra.runner-fleet.v1",
    "host": "observer-host.invalid",
    "runner_group": "omnibase-ci",
    "name_prefix": "omninode-",
    "observed_at": "2026-09-20T13:30:08.464219+00:00",
    "runner_count": 69,
    "online_count": 69,
    "busy_count": 0,
    "offline_count": 0,
    "class_rollup": [
        {
            "label_class": "omnibase-ci",
            "total": 60,
            "online": 60,
            "busy": 0,
            "offline": 0,
        },
    ],
    "runners": [
        {
            "runner_name": "omninode-air-runner-1",
            "runner_id": 141087,
            "label_class": "omnibase-verify",
            "labels": [
                "self-hosted",
                "Linux",
                "ARM64",
                "omnibase-verify",
                "arch-arm64",
                "host-105",
            ],
            "host": "host-105",
            "observing_host": "observer-host.invalid",
            "status": "online",
            "current_job_id": None,
            "observed_at": "2026-09-20T13:30:08.464219+00:00",
        },
        {
            "runner_name": "omninode-customer-plane-runner-1",
            "runner_id": 141082,
            "label_class": "omnibase-customer-plane",
            "labels": ["self-hosted", "Linux", "X64", "omnibase-customer-plane"],
            "host": "observer-host.invalid",
            "observing_host": "observer-host.invalid",
            "status": "online",
            "current_job_id": None,
            "observed_at": "2026-09-20T13:30:08.464219+00:00",
        },
        {
            "runner_name": "omninode-runner-27",
            "runner_id": 33573,
            "label_class": "omnibase-ci",
            "labels": ["self-hosted", "Linux", "X64", "omnibase-ci"],
            "host": "observer-host.invalid",
            "observing_host": "observer-host.invalid",
            "status": "busy",
            "current_job_id": None,
            "observed_at": "2026-09-20T13:36:07.751736+00:00",
        },
    ],
}


def test_the_live_emitters_payload_parses_against_the_consumer_model() -> None:
    """AC1. This is the assertion that was false in production.

    Every message on the topic failed here with ``observation Field required``
    and was dead-lettered, while the projection's rows kept arriving from the
    writer on the same topic — which is why nothing looked broken.
    """
    request = ModelRunnerFleetProjectionRequest(**LIVE_EVENT)

    assert request.observation.host == "observer-host.invalid"
    assert request.observation.event_type == "runner-fleet-observation"
    assert len(request.observation.runners) == 3
    # No caller supplied them, so nothing is claimed to have disappeared.
    assert request.known_runner_names == ()


def test_the_payload_carries_no_observation_key_which_is_the_whole_defect() -> None:
    """AC1's red control, stated as a property of the real payload.

    The previous model required ``observation``. Asserting only that the new
    model accepts the event would pass just as well against a payload someone
    had quietly reshaped, so the shape itself is pinned here: the emitter sends
    these fields at the top level and sends no wrapper.
    """
    assert "observation" not in LIVE_EVENT
    assert {
        "schema_version",
        "event_type",
        "host",
        "observed_at",
        "runners",
    } <= set(LIVE_EVENT)

    # The nested expectation the model used to carry, applied to this payload,
    # is exactly the production failure.
    with pytest.raises(ValidationError) as caught:
        ModelRunnerFleetObservationWire(**{"observation": LIVE_EVENT})
    assert [e["type"] for e in caught.value.errors()] == ["missing"] * 4


def test_the_bare_observation_and_the_nested_form_derive_the_same_rows() -> None:
    """Accepting the flat shape must not fork the derivation.

    The writer and the golden chains build the nested form deliberately, and a
    caller supplying ``known_runner_names`` needs somewhere to put them, so
    both forms stay legal. They have to mean the same thing.
    """
    flat = ModelRunnerFleetProjectionRequest(**LIVE_EVENT)
    nested = ModelRunnerFleetProjectionRequest(observation=LIVE_EVENT)  # type: ignore[arg-type]

    handler = HandlerProjectionRunnerFleet()
    assert handler.handle(flat).rows == handler.handle(nested).rows


def test_the_runtime_injections_never_reach_the_observation() -> None:
    """The runtime adds its own underscore keys to the dict it dispatches.

    They are transport metadata, not fields of the observation, and folding
    them in would make the derivation a function of how it was delivered.
    """
    injected = dict(LIVE_EVENT)
    injected["_topic"] = "onex.evt.omnibase-infra.runner-fleet.v1"
    injected["_envelope_id"] = "6ff88ee1-915f-47b9-9a5b-cabf5a6eae77"
    injected["_partition"] = 0

    request = ModelRunnerFleetProjectionRequest(**injected)

    dumped = request.observation.model_dump()
    assert not [key for key in dumped if key.startswith("_")]
    assert request.observation.runners[0].runner_name == "omninode-air-runner-1"


def test_a_caller_supplied_known_runner_set_still_reaches_the_derivation() -> None:
    """The flat path must not silently drop the one field it has to carry.

    ``known_runner_names`` is how a name that has DISAPPEARED gets tombstoned.
    A flat promotion that dropped it would turn every deregistration into
    silence, which is the failure this projection exists to make visible.
    """
    with_known = dict(LIVE_EVENT)
    with_known["known_runner_names"] = ("omninode-runner-99",)

    request = ModelRunnerFleetProjectionRequest(**with_known)

    assert request.known_runner_names == ("omninode-runner-99",)
    assert "known_runner_names" not in request.observation.model_dump()
