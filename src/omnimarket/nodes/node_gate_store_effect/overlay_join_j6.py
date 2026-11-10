# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the J6 runner (OMN-22936): every setting of the run, none in code.

Found at ``<root>/node_gate_store_join_j6/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. Nothing has a
default: an absent key refuses the load and names it. The upstream rule (piece 6's golden event rows) and the emission
journal are the join refusal overlay's (``node_join_refusal_effect``), join J6, read from there so one rule feeds both the
floor's chains and this run's correlation ids.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

OVERLAY_ID = "node_gate_store_join_j6"


class ModelJoinJ6Overlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The node name piece 7's chains are emitted under.
    emission_node: str = Field(min_length=1)
    #: The test pieces the run stops and sends on; neither is a real piece, and the drill never touches a live one.
    stopped_piece: str = Field(min_length=1)
    other_piece: str = Field(min_length=1)
    #: The in-memory gate store's per-piece reservation limit and reservation lifetime.
    piece_limit: int = Field(ge=2)
    reservation_ttl_seconds: int = Field(ge=1)
    #: How many unanswered piece 6 golden chains one run answers, newest first.
    max_chains_per_run: int = Field(ge=1)
    #: The fence drill (piece 7's own error chain): run when the journal holds none in this many hours.
    drill_window_hours: int = Field(ge=1)
    #: The drill's commit deadline and how long the injected silence holds the commit; the hold exceeds the deadline.
    drill_commit_deadline_seconds: float = Field(gt=0)
    drill_silence_seconds: float = Field(gt=0)
    #: Whether this overlay may run a named injected break (the join's failing control).
    allow_injected_break: bool
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_pieces_and_silence(self) -> ModelJoinJ6Overlay:
        if self.stopped_piece == self.other_piece:
            raise ValueError("the stopped piece and the other piece are two pieces")
        if self.drill_silence_seconds <= self.drill_commit_deadline_seconds:
            raise ValueError("the injected silence outlasts the commit deadline")
        return self


def load_join_overlay(environ: Mapping[str, str] | None = None) -> ModelJoinJ6Overlay:
    return load_structured_overlay(OVERLAY_ID, ModelJoinJ6Overlay, environ)


__all__ = ["OVERLAY_ID", "ModelJoinJ6Overlay", "load_join_overlay"]
