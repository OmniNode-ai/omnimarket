# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the J7 runner (OMN-23420): every setting of the run, none in code.

Found at ``<root>/node_lab_check_run_join_j7/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. Nothing has a
default: an absent key refuses the load and names it. The upstream rule (piece 7's golden stop drill rows) and the emission
journal are the join refusal overlay's (``node_join_refusal_effect``), join J7, read from there so one rule feeds both the floor's
chains and this run's correlation ids. The check-run settings the run's twin uses are the ``check_run`` block below: the twin is
the in-memory check-run API, so the run never posts on a real head.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_lab_check_run_effect.overlay import (
    ModelLabCheckRunOverlay,
)

OVERLAY_ID = "node_lab_check_run_join_j7"


class ModelJoinJ7Overlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The join this run answers, as the join refusal overlay names it, and the node name piece 8's chains are emitted under.
    join_id: str = Field(min_length=1)
    emission_node: str = Field(min_length=1)
    #: The test pieces the run stops and sends on; neither is a real piece, and the run never stops or sends on a live one.
    stopped_piece: str = Field(min_length=1)
    other_piece: str = Field(min_length=1)
    #: The in-memory gate store's per-piece reservation limit and reservation lifetime.
    piece_limit: int = Field(ge=2)
    reservation_ttl_seconds: int = Field(ge=1)
    #: How many unanswered piece 7 golden chains one run answers, newest first.
    max_chains_per_run: int = Field(ge=1)
    #: Piece 8's own error drill (a late post of a superseded attempt) runs when the journal holds none in this many hours.
    drill_window_hours: int = Field(ge=1)
    #: The test repository, its base branch and the two heads and attempt identities (dispatch:order:overlay) of the twin.
    repository: str = Field(min_length=1)
    base_branch: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    old_head_sha: str = Field(min_length=1)
    current_attempt_id: str = Field(min_length=1)
    old_attempt_id: str = Field(min_length=1)
    #: The stop the run opens: its id and class, how long before the run it opened, and how long after that its lease was granted.
    stop_id: str = Field(min_length=1)
    stop_class: str = Field(min_length=1)
    stop_age_minutes: int = Field(ge=1)
    lease_after_minutes: int = Field(ge=1)
    #: The twin's check-run settings (the in-memory source, the check's name and the superseded conclusion).
    check_run: ModelLabCheckRunOverlay
    #: Whether this overlay may run a named injected break (the join's failing control).
    allow_injected_break: bool
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct(self) -> ModelJoinJ7Overlay:
        if self.stopped_piece == self.other_piece:
            raise ValueError("the stopped piece and the other piece are two pieces")
        if self.head_sha == self.old_head_sha:
            raise ValueError("the head and the old head are two heads")
        if self.current_attempt_id == self.old_attempt_id:
            raise ValueError("the current attempt and the old attempt are two attempts")
        if self.repository not in self.check_run.settings.lab_repositories:
            raise ValueError("the test repository is a lab repository of the twin")
        return self


def load_join_overlay(environ: Mapping[str, str] | None = None) -> ModelJoinJ7Overlay:
    return load_structured_overlay(OVERLAY_ID, ModelJoinJ7Overlay, environ)


__all__ = ["OVERLAY_ID", "ModelJoinJ7Overlay", "load_join_overlay"]
