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

CheckId = Literal["C13", "C14", "C17"]


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
