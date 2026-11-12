# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One model as the onex model explorer shows it (OMN-23413).

Built by omnimarket.handlers.model_catalog from the provider's own list, the
canonical pricing contract, models.dev and real pinned tests, and shown by
every screen (/config, /model, /delegation) and `onex models ... --json`.
An unknown value stays None or "unknown": never zero, never "no".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: supported, unsupported, or not stated by any source read.
CapabilityState = Literal["yes", "no", "unknown"]

#: Where a model runs: on a server the user runs (local) or a hosted API (cloud).
Deployment = Literal["local", "cloud"]

#: listed: the provider returned it, not yet tested; ready / failed: the last
#: pinned delegation passed or failed on it; untested: stored, never tested;
#: unavailable: stored but the provider no longer lists it, or not set up.
ModelStatus = Literal["listed", "ready", "failed", "untested", "unavailable"]


class ModelCatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    #: How the provider is shown: the server's own kind for a model run by URL.
    provider_label: str = ""
    model_id: str
    display_name: str
    deployment: Deployment
    context_tokens: int | None = None
    max_output_tokens: int | None = None
    #: USD per 1M tokens; None means unknown (never treated as free).
    input_usd_per_million: Decimal | None = None
    output_usd_per_million: Decimal | None = None
    cache_read_usd_per_million: Decimal | None = None
    cache_write_usd_per_million: Decimal | None = None
    #: The price depends on context size or another tier (models.dev cost.tiers).
    tiered_price: bool = False
    capabilities: dict[str, CapabilityState] = Field(default_factory=dict)
    status: ModelStatus = "listed"
    #: Why the status is what it is (a failed test's reason, a missing list).
    status_reason: str | None = None
    #: Which source each shown field came from, e.g. {"price": "OpenRouter"}.
    sources: dict[str, str] = Field(default_factory=dict)
    #: When supplemental metadata was fetched (ISO UTC), and whether it is stale.
    metadata_fetched_at: str | None = None
    metadata_stale: bool = False
