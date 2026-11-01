# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21536: the typed request every projection runner receives from the shim.

The shared runtime's projection arm hands a runner one ``dict`` per delivery.
``BaseProjectionRunner.handle`` splits it once into a
``ModelProjectionRequest`` and passes it to ``project_request``, so no
runner pops the delivery keys by hand. These tests pin the split, the defaults
that the hand-written pops had, and the seam itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

pytestmark = pytest.mark.unit


class _Runner(BaseProjectionRunner):
    """A runner that records the request its shim hands it."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[ModelProjectionRequest] = []

    @property
    def topics(self) -> list[str]:
        return ["onex.evt.test.v1"]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        return True

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        self.seen.append(request)
        return {"rows_upserted": 1}


class _StandaloneRunner(BaseProjectionRunner):
    """A runner that owns its consume loop and implements no in-process entry."""

    @property
    def topics(self) -> list[str]:
        return ["onex.evt.test.v1"]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        return True


def test_split_removes_exactly_the_four_delivery_keys() -> None:
    sentinel = object()
    request = ModelProjectionRequest.from_injected(
        {
            "_topic": "onex.evt.a.v1",
            "_partition": 2,
            "_offset": 91,
            "_fallback_id": "fb-1",
            "_db": sentinel,
            "_event_type": "a",
            "lane": "lane-a",
        }
    )
    assert request.topic == "onex.evt.a.v1"
    assert (request.partition, request.offset) == (2, 91)
    assert request.fallback_id == "fb-1"
    assert request.has_coordinates
    # The kernel seam's other keys stay, as the runners' own pops left them.
    assert request.payload == {
        "_db": sentinel,
        "_event_type": "a",
        "lane": "lane-a",
    }
    assert request.payload["_db"] is sentinel


def test_split_does_not_mutate_the_injected_dict() -> None:
    injected: dict[str, object] = {"_topic": "t", "_partition": 1, "_offset": 2, "x": 1}
    ModelProjectionRequest.from_injected(injected)
    assert injected == {"_topic": "t", "_partition": 1, "_offset": 2, "x": 1}


def test_absent_keys_take_the_defaults_the_hand_written_pops_had() -> None:
    request = ModelProjectionRequest.from_injected({"lane": "lane-a"})
    assert request.topic is None
    assert request.topic_or("fallback.v1") == "fallback.v1"
    assert (request.partition, request.offset) == (0, 0)
    assert request.fallback_id == ""
    assert not request.has_coordinates


def test_a_named_empty_topic_is_kept_not_defaulted() -> None:
    request = ModelProjectionRequest.from_injected({"_topic": ""})
    assert request.topic == ""
    assert request.topic_or("fallback.v1") == ""


@pytest.mark.parametrize(
    "injected",
    [{"_partition": 1}, {"_offset": 1}],
    ids=["partition-only", "offset-only"],
)
def test_coordinates_need_both_keys(injected: dict[str, object]) -> None:
    assert not ModelProjectionRequest.from_injected(injected).has_coordinates


def test_wire_strings_for_the_coordinates_are_coerced() -> None:
    request = ModelProjectionRequest.from_injected(
        {"_partition": "3", "_offset": "4242"}
    )
    assert (request.partition, request.offset) == (3, 4242)


def test_a_coordinate_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelProjectionRequest.from_injected({"_partition": "left"})


def test_the_request_is_frozen_and_closed() -> None:
    request = ModelProjectionRequest.from_injected({})
    with pytest.raises(ValidationError):
        request.partition = 9
    with pytest.raises(ValidationError):
        ModelProjectionRequest.model_validate({"payload": {}, "extra": 1})


def test_handle_passes_the_runner_the_typed_request_and_returns_its_result() -> None:
    runner = _Runner()
    result = runner.handle(
        {"_topic": "onex.evt.test.v1", "_partition": 5, "_offset": 6, "k": "v"}
    )
    assert result == {"rows_upserted": 1}
    assert len(runner.seen) == 1
    assert runner.seen[0].partition == 5
    assert runner.seen[0].payload == {"k": "v"}


def test_a_runner_with_no_in_process_entry_refuses_a_delivery() -> None:
    with pytest.raises(NotImplementedError, match="_StandaloneRunner"):
        _StandaloneRunner().handle({"k": "v"})
