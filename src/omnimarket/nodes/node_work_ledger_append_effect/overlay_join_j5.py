# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the J5 runner (OMN-23086): every setting of the run, none in code.

Found at ``<root>/node_work_ledger_join_j5/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. Nothing has
a default: an absent key refuses the load and names it. The overlay names the store key by folder and key only and never
holds a value. The upstream rule (piece 5's golden event rows) and the emission journal are the join refusal overlay's
(``node_join_refusal_effect``), read from there so one rule feeds both the floor's chains and this run's correlation ids.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
)
from omnimarket.nodes.node_work_ledger_append_effect.adapters.join_j5 import (
    ModelStoreIdentityBinding,
)

OVERLAY_ID = "node_work_ledger_join_j5"


class ModelJ5Ledger(BaseModel):
    """The durable ledger the append lands on: a file to read back, a command to append through, the host's name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_id: str = Field(min_length=1)
    path_env: str = Field(min_length=1)
    command_env: str = Field(min_length=1)
    host_env: str = Field(min_length=1)
    append_timeout_seconds: int = Field(ge=1)


class ModelJ5Row(BaseModel):
    """The row one run appends."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    requesting_lane: str = Field(min_length=1)
    text: str = Field(min_length=1, pattern=r"^[^|\n]+$")


class ModelJ5Projection(BaseModel):
    """How piece 6's projection is read: a read command over rows, asked again while the row has not arrived."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read: ModelCommandLedgerReadRoute
    attempts: int = Field(ge=1)
    interval_seconds: float = Field(gt=0)
    slack_seconds: int = Field(ge=1)


class ModelJoinJ5Overlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: ModelStoreIdentityBinding
    ledger: ModelJ5Ledger
    row: ModelJ5Row
    projection: ModelJ5Projection
    #: How many unanswered piece 5 golden chains one run answers, newest first.
    max_appends_per_run: int = Field(ge=1)
    #: The run's own time limit (OMN-23591). It sits inside the publish step's time limit (the ``join_j5`` step of
    #: ``node_factory_floor_publish_effect``), so a run still open at it closes its chain typed before the step kills it.
    run_deadline_seconds: int = Field(ge=1)
    #: Whether this overlay may run a named injected break (the join's failing control).
    allow_injected_break: bool
    injected_fault: str = Field(min_length=1)

    @property
    def projection_wait_seconds(self) -> float:
        """The time the poll spends waiting between reads: the reads themselves come on top of it."""
        return (self.projection.attempts - 1) * self.projection.interval_seconds

    @model_validator(mode="after")
    def _poll_fits_the_deadline(self) -> ModelJoinJ5Overlay:
        # A poll that waits longer than the run's own time limit is cut by the limit, so its attempts would only be
        # a promise the run cannot keep (OMN-23717): the overlay is refused and the two numbers are named.
        if self.projection_wait_seconds >= self.run_deadline_seconds:
            raise ValueError(
                f"the projection poll waits {self.projection_wait_seconds:g} s between its "
                f"{self.projection.attempts} reads, not inside run_deadline_seconds={self.run_deadline_seconds}"
            )
        return self


def load_join_overlay(environ: Mapping[str, str] | None = None) -> ModelJoinJ5Overlay:
    return load_structured_overlay(OVERLAY_ID, ModelJoinJ5Overlay, environ)


__all__ = ["OVERLAY_ID", "ModelJoinJ5Overlay", "load_join_overlay"]
