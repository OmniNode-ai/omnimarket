# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B input for the lab lane-health fold (OMN-18769)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.nodes.node_projection_lab_lane_health.contract_topics import (
    TOPIC_LAB_PASS_RECEIPT,
    TOPIC_LANE_CENSUS,
    TOPIC_RUNTIME_HEALTH,
)

#: The discriminating key of each input shape, and the topic it arrives on.
#: Census is keyed on ``lanes_checked`` because a census is the only event that
#: names a SET of lanes; the other two name one lane and differ on which
#: timestamp they carry. The three are disjoint on live traffic and the
#: validator below refuses anything that matches none or more than one.
_SHAPES: tuple[tuple[str, str], ...] = (
    ("lanes_checked", TOPIC_LANE_CENSUS),
    ("finished_at", TOPIC_LAB_PASS_RECEIPT),
    ("timestamp", TOPIC_RUNTIME_HEALTH),
)


class ModelLabLaneHealthRequest(BaseModel):
    """One source fact, exactly as the runtime adapter hands it over.

    THIS IS THE EVENT, NOT A WRAPPER AROUND IT. The shared
    ``runtime_local_adapter`` builds a handler's def-B input with
    ``input_model_cls(**payload_dict)`` over the unwrapped DOMAIN payload, so a
    model declaring ``{topic, payload}`` can never be constructed from a bus
    message: the event has no ``topic`` key of its own to supply, and the
    adapter has no wrapper to put one in.

    The previous revision of this model was that wrapper. It validated in every
    unit test, because those tests built it by hand, and failed on every real
    message with ``1 validation error ... topic Field required``. The projection
    consumed, committed its offsets and wrote nothing for as long as it ran.
    That is why the test beside this model drives the adapter path rather than
    the fold: a fold test cannot see this class of defect.

    WHY THE SHAPE IS DERIVED RATHER THAN DECLARED. The three input topics carry
    three unrelated payloads — a census over many lanes, a runtime health
    snapshot, a lab-pass receipt. Flattening their union into one
    optional-everything record would let a malformed census validate as an
    empty health event, which the previous docstring correctly refused to do.
    The topic is therefore recovered from the payload's own discriminating key
    rather than asserted by the caller, and an event matching no shape, or more
    than one, is REFUSED here instead of being best-effort parsed downstream.

    The per-shape parsers in ``lane_health_fold`` still enforce their own
    required fields, so this validator is a router, not a substitute for them.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> ModelLabLaneHealthRequest:
        matched = self._matched_shapes()
        if len(matched) == 1:
            return self
        keys = sorted(self.as_payload())
        if not matched:
            raise ValueError(
                "lab lane-health input matches no known source shape: expected "
                "exactly one of 'lanes_checked' (census), 'finished_at' "
                f"(lab-pass receipt) or 'timestamp' (runtime health); got {keys}"
            )
        raise ValueError(
            "lab lane-health input is ambiguous: it carries the discriminating "
            f"keys of more than one source shape ({sorted(matched)}); got {keys}"
        )

    def _matched_shapes(self) -> tuple[str, ...]:
        payload = self.as_payload()
        return tuple(key for key, _ in _SHAPES if payload.get(key) is not None)

    def as_payload(self) -> dict[str, Any]:
        """The event verbatim, as the fold's parsers read it.

        ``model_extra`` rather than ``model_dump`` on purpose: this model
        declares no fields, so nothing is coerced on the way in and nothing is
        re-typed on the way out. A ``lanes_checked`` list stays a list, which
        ``census_facts`` checks with ``isinstance(..., list)``.
        """
        return dict(self.model_extra or {})

    @property
    def source_topic(self) -> str:
        """The topic this event must have arrived on, from its own shape."""
        matched = self._matched_shapes()
        lookup = dict(_SHAPES)
        return lookup[matched[0]]

    @property
    def source_kind(self) -> Literal["census", "receipt", "health"]:
        """Which of the three source shapes this is, for logging and tests."""
        kinds: dict[str, Literal["census", "receipt", "health"]] = {
            "lanes_checked": "census",
            "finished_at": "receipt",
            "timestamp": "health",
        }
        return kinds[self._matched_shapes()[0]]
