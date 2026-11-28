# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates overlays/node_delegation_check_probe_effect/overlay.yaml through (OMN-26043).

The overlays repository pins a node's overlay to a merged revision that already holds its model, and the omnimarket
overlay check refuses a new node whose overlay is not on overlays main. The model therefore lives in this shared
package and merges ahead of the node that reads it. No value lives here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus

CheckId = Literal["C11", "C12", "C13", "C14", "C15", "C16", "C17", "C28", "C29", "C30"]


class ModelRegressionTicketSettings(BaseModel):
    """Where a check that turned red files its one top-priority ticket (OMN-26726); no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The ticket the regression ticket is filed under (the create gate requires a parent).
    parent: str = Field(pattern=r"^OMN-\d+$")
    # The lane id the ticket node's write command carries.
    lane: str = Field(min_length=1)
    repo: str | None = None
    # The file under the tick's state directory holding each check's last reading and open ticket.
    state_file: str = Field(min_length=1)


class ModelFallThroughSettings(BaseModel):
    """The declared setting that turns the chain fall-through leg on for the scheduled C13 and C14 runs (OMN-26043).

    With the block absent the leg is off and a run reads as it did before. ``secret_ref`` is the secret store REFERENCE the
    OpenRouter tier reads its key through on dev (never a value); the key reaches the clean customer machine only through
    the install's own ``onex secret set``, and never appears in a record. No key has a default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: tuple[Literal["C13", "C14"], ...] = Field(min_length=1)
    secret_ref: str = Field(pattern=r"^llm\.[a-z0-9_-]+\.[a-z0-9_]+$")
    # The environment variable NAME the host hands the key to the clean machine's driver under; never a value.
    key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")


class ModelDelegationCheckProbeOverlay(BaseModel):
    """Every setting of the delegation check probe node; no key has a default.

    The responder row is the settings store's (overlay ``delegation_check_responders``), not this document's.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The checks one tick runs, in order.
    checks: tuple[CheckId, ...] = Field(min_length=1)
    history_file: str = Field(min_length=1)
    history_keep: int = Field(gt=0)
    result_topic: str = Field(min_length=1)
    # How long one responder has to answer ``/v1/models``.
    responder_timeout_s: float = Field(gt=0)
    # How long the gateway has to answer the workflow submit (POST /v1/workflows), apart from a responder's cap. Absent,
    # the submit shares ``responder_timeout_s``.
    submit_timeout_s: float | None = Field(default=None, gt=0)
    # The bus the tick delivers its run record on: the lane id only, read by the declared broker publisher. A lab host's
    # unit carries no KAFKA_BOOTSTRAP_SERVERS, so without it the tick delivers nothing and exits 3.
    event_bus: ModelNodeEventBus | None = None
    # Absent: a red check writes its STATUS evidence only, as before.
    regression_ticket: ModelRegressionTicketSettings | None = None
    # Absent: C13 and C14 run without the fall-through leg.
    fall_through: ModelFallThroughSettings | None = None
