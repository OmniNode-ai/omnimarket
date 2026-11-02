# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed facts the worker-engine choice reads in place of the placement module (OMN-21958).

The caller probes the pool and states, per engine, the pick a host can take now or why no host can. The
node's config carries the Codex engine id and the two Codex routes; both come from the node's contract and
its overlay, never from a literal in the handler or an environment switch.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelEngineAvailability(BaseModel):
    """One engine's availability at placement, recounted by the caller under the placement lock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # the engine id as the caller's placement names it
    engine: str = Field(min_length=1)
    # the placement a host can take for this engine now, or None when no host can
    pick: Any = None
    # host -> why the host cannot take the engine; read when ``pick`` is None
    skips: Mapping[str, str] = Field(default_factory=dict)


class ModelWorkerEngineConfig(BaseModel):
    """``config`` of the node's contract, with the overlay's ``codex_routes`` laid over it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # the id of the Codex engine in the caller's placement
    codex_engine: str = Field(min_length=1)
    # False turns off every Codex route
    codex_mechanical: bool
    # False turns off only the fallback from a Claude pool with no headroom
    codex_fallback: bool
