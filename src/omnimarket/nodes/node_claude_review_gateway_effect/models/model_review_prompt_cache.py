# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which review prompt blocks the provider caches, and for how long (OMN-22343).

The values live in the node's ``contract.yaml`` under ``config.prompt_cache``
(or an overlay that replaces that mapping), never in handler code. The
provider's rules this model enforces (prompt-caching documentation):

* at most four ``cache_control`` breakpoints per request;
* TTL is ``5m`` (write 1.25x base input) or ``1h`` (write 2x); a read is cheap
  on either;
* cache entries with a longer TTL must appear before shorter ones, and the
  render order is tools, system, messages, so the system blocks (1h) precede
  the diff block (5m).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EnumCacheTtl = Literal["5m", "1h"]

#: The provider's hard limit on ``cache_control`` breakpoints in one request.
PROVIDER_MAX_BREAKPOINTS = 4


class ModelReviewPromptCache(BaseModel):
    """Cache policy of one review request.

    Attributes:
        system_ttl: TTL of the cached system blocks (rubric and catalog, then the
            repository's standing rules). ``None`` leaves the system uncached.
        diff_ttl: TTL of the cached diff block, which precedes the voter's own
            uncached persona text. ``None`` leaves the diff uncached.
        max_breakpoints: Most breakpoints this gateway places, at most four.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system_ttl: EnumCacheTtl | None = Field(default="1h")
    diff_ttl: EnumCacheTtl | None = Field(default="5m")
    max_breakpoints: int = Field(default=PROVIDER_MAX_BREAKPOINTS, ge=1, le=4)

    @model_validator(mode="after")
    def _longer_ttl_first(self) -> ModelReviewPromptCache:
        if self.system_ttl == "5m" and self.diff_ttl == "1h":
            raise ValueError(
                "a 1h diff entry cannot follow a 5m system entry: the provider "
                "requires longer TTLs to appear first"
            )
        return self


__all__ = ["PROVIDER_MAX_BREAKPOINTS", "EnumCacheTtl", "ModelReviewPromptCache"]
