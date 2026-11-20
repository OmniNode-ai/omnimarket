# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_item_owner_dispatch_effect: every bound, cell name, command and lane setting (OMN-25981).

Found at ``<root>/node_item_owner_dispatch_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
The package ships ``overlay.example.yaml`` (the in-memory backend, synthetic values). Nothing here has a default: an
absent key refuses the load and names it. The lane runner and brief settings are the host drift owner dispatch's own
models, so an owner lane is placed and its brief checked exactly as that node does it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.overlay import (
    ModelBriefSettings,
    ModelDispatchSettings,
)

NODE_NAME = "node_item_owner_dispatch_effect"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelItemOwnerPolicy(_Frozen):
    """The bounds and cell vocabulary of the fold, the owner step and the reading."""

    #: An item with no running lane for this long gets one owner lane, and the reading reports it.
    owner_after_minutes: float = Field(gt=0)
    #: A ``state=owned`` row whose lane has written no CLAIM yet counts as running for this long (the lane starting).
    settle_minutes: float = Field(gt=0)
    #: At most this many owner lanes per pass.
    lane_cap: int = Field(ge=0)
    #: The node's own identity on the rows it appends (``lane=`` and ``actor=``).
    node_lane: str = Field(min_length=1)
    node_actor: str = Field(min_length=1)
    #: Owner lanes are named ``<lane_prefix>-<item hex>-g<generation>``, so a repeat of one binding has one name.
    lane_prefix: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    #: The ``cause=`` value an owner lane's CLAIM carries is ``<cause_prefix>:<item id>``.
    cause_prefix: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    #: TERMINAL outcomes that make a lane an item (source a).
    item_outcomes: tuple[str, ...] = Field(min_length=1)
    #: Source b: a RULING carrying this cell is an item until a CORRECTION ``corrects=<stamp>`` carries ``ruling_done_cell``.
    ruling_pending_cell: str = Field(min_length=1)
    ruling_done_cell: str = Field(min_length=1)
    #: Source d: the capture's lane, its open states and the states that close an ask.
    capture_lane: str = Field(min_length=1)
    capture_open_states: tuple[str, ...] = Field(min_length=1)
    capture_closed_states: tuple[str, ...] = Field(min_length=1)
    #: Tickets that never pair a TERMINAL with a successor CLAIM (umbrella tickets every lane cites).
    umbrella_tickets: tuple[str, ...]
    summary_limit: int = Field(ge=1)
    #: The reading lists at most this many item ids on its STATUS row.
    reading_id_limit: int = Field(ge=1)


class ModelLedgerSettings(_Frozen):
    """How the rows are read from the rolling ledger over the bus, and how a row is appended."""

    #: The ledger query command (argv); the node adds ``--query rows --kind <K> --since <S> --limit <N>``.
    query_command: tuple[str, ...] = Field(min_length=1)
    kinds: tuple[str, ...] = Field(min_length=1)
    lookback_hours: float = Field(gt=0)
    row_limit: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    #: The append command (argv); the node adds the row as the last argument.
    append_command: tuple[str, ...] = Field(min_length=1)


class ModelPlanOwnerSettings(_Frozen):
    """The plan owner check's command; its ``MISSING <rule>`` lines are source c. Empty argv: no plan source."""

    command: tuple[str, ...]
    timeout_seconds: float = Field(gt=0)


class ModelItemOwnerOverlay(_Frozen):
    backend: Literal["inmemory", "runtime"]
    policy: ModelItemOwnerPolicy
    ledger: ModelLedgerSettings
    plan_owner: ModelPlanOwnerSettings
    dispatch: ModelDispatchSettings
    brief: ModelBriefSettings


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelItemOwnerOverlay:
    return load_structured_overlay(NODE_NAME, ModelItemOwnerOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelItemOwnerOverlay",
    "ModelItemOwnerPolicy",
    "ModelLedgerSettings",
    "ModelPlanOwnerSettings",
    "load_overlay",
]
