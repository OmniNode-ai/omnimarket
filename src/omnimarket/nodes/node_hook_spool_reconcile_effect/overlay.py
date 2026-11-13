# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_hook_spool_reconcile_effect: every setting of the spool reconcile, none in code.

Found at ``<root>/node_hook_spool_reconcile_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_hook_spool_reconcile_effect"


class ModelSpoolPaths(BaseModel):
    """The spool's directories and files, relative to a directory an environment variable names.

    ``dead_letter_relpaths`` names every dead-letter dir the drainer leaves records in (quarantine and denied-classes);
    each is read for replay. ``loss_log_relpath`` is the jsonl loss log, ``drainer_status_relpath`` the drainer's status
    (its refused event classes), and ``alarm_state_relpath`` the alarm state this node writes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    journal_relpath: str = Field(min_length=1)
    dead_letter_relpaths: tuple[str, ...] = Field(min_length=1)
    loss_log_relpath: str = Field(min_length=1)
    drainer_status_relpath: str = Field(min_length=1)
    alarm_state_relpath: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelSpoolPaths:
        if len(self.dead_letter_relpaths) != len(set(self.dead_letter_relpaths)):
            raise ValueError("a dead-letter dir is named twice")
        if any(not p.strip() for p in self.dead_letter_relpaths):
            raise ValueError("a dead-letter dir path is empty")
        return self


class ModelReplayBound(BaseModel):
    """How many records one run replays, and how old they must be before they are replayed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_limit: int = Field(ge=1)
    min_age_seconds: int = Field(ge=0)


class ModelReconcileEmission(BaseModel):
    """The emission journal the receipt fold reads, relative to a directory an environment variable names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    journal_relpath: str = Field(min_length=1)
    piece: int = Field(ge=1, le=18)


class ModelHookSpoolReconcileOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spool: ModelSpoolPaths
    replay: ModelReplayBound
    emission: ModelReconcileEmission


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHookSpoolReconcileOverlay:
    return load_structured_overlay(NODE_NAME, ModelHookSpoolReconcileOverlay, environ)
