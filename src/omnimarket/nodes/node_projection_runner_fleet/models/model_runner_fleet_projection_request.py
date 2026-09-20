# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B input for the runner-fleet derivation (OMN-18768)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_projection_runner_fleet.models.model_runner_fleet_observation_wire import (
    ModelRunnerFleetObservationWire,
)


class ModelRunnerFleetProjectionRequest(BaseModel):
    """One observation, plus the fact the derivation cannot know by itself.

    ``known_runner_names`` is the set of runners already materialized for this
    host, supplied BY THE CALLER rather than looked up here. That is what keeps
    ``handle()`` a pure function of its input -- no clock, no database, no
    ambient state, so the same request always derives the same rows -- while
    still letting the derivation name the runners that have DISAPPEARED and owe
    a tombstone. The writer that owns the database resolves it and hands it in.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    observation: ModelRunnerFleetObservationWire

    @model_validator(mode="before")
    @classmethod
    def _accept_the_bare_observation(cls, data: Any) -> Any:
        """The event IS the observation; the wrapper is this model's own.

        OMN-18880. The runtime hands a projection handler the bare event dict
        plus its own underscore-prefixed injections. This model declared the
        observation NESTED under a key the emitter has never sent, so the
        runtime could not construct it and every message on the topic failed
        the pure handler with `observation Field required`.

        Measured on the .201 dev lane, 2026-09-20T13:4xZ, over 40 consecutive
        live messages captured off `onex.evt.omnibase-infra.runner-fleet.v1`:
        40 of 40 flat, 0 carrying an `observation` key, 40 of 40 refused here
        on exactly that field. The positive control is that the SAME 40 parse
        cleanly as `ModelRunnerFleetObservationWire`, which is what proves the
        emitter is right and this wrapper is what was wrong. Neither reading
        the ticket offered -- a second emitter, or a conditionally absent
        field -- survives that measurement.

        The nested form is still accepted, because the writer and the golden
        chains construct it deliberately and a caller supplying
        ``known_runner_names`` has to have somewhere to put them.
        """
        if not isinstance(data, dict) or "observation" in data:
            return data
        return {
            "observation": {k: v for k, v in data.items() if not k.startswith("_")},
            "known_runner_names": data.get("known_runner_names", ()),
        }

    known_runner_names: tuple[str, ...] = Field(
        default=(),
        description=(
            "Runner names already materialized for this host. A name in here "
            "and absent from the observation has been deregistered and is "
            "tombstoned; a name absent from BOTH is simply not this host's."
        ),
    )
