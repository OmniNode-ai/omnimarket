# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the J4 join test (OMN-22912): every setting of the join, none in code.

Found at ``<root>/node_secret_reading_join_j4/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The store addressing, the consumers and the planted dummy come
from the piece 5 reading overlay (``overlay.py``); this one adds only what the join itself
needs. Nothing here has a default: an absent key refuses the load and names it. The overlay
names keys by consumer only; it never holds a value.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

OVERLAY_ID = "node_secret_reading_join_j4"


class ModelJoinJ4Overlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The declared consumer of the piece 5 reading overlay whose key the join resolves.
    consumer: str = Field(min_length=1)
    #: The emission identity of the consuming node and the piece it belongs to (4).
    consumer_node: str = Field(min_length=1)
    consumer_piece: int = Field(ge=1, le=18)
    #: The emission identity of the scan and the piece it belongs to (5).
    scan_node: str = Field(min_length=1)
    scan_piece: int = Field(ge=1, le=18)
    #: Glob patterns of the consuming node's code; ``~`` and variables expand on the host.
    code_globs: tuple[str, ...] = Field(min_length=1)
    #: Glob patterns of the node's own log files, read whole beside the lines of this run.
    log_surfaces: tuple[str, ...]
    max_bytes_per_surface: int = Field(ge=1024)
    #: The floor's emission journal the chains are written to (append-only JSON lines).
    emission_journal: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: A drill run is tagged so no join counts it as evidence.
    drill: bool
    injected_fault: str = Field(min_length=1)


def load_join_overlay(environ: Mapping[str, str] | None = None) -> ModelJoinJ4Overlay:
    return load_structured_overlay(OVERLAY_ID, ModelJoinJ4Overlay, environ)


__all__ = ["OVERLAY_ID", "ModelJoinJ4Overlay", "load_join_overlay"]
