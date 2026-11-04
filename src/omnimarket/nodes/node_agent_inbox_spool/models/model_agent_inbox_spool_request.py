# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One bounded read of the ledger MSG topic for the lanes running on a host."""

from typing import Annotated

from pydantic import Field, StringConstraints

from ._base import ModelFrozen
from .model_agent_inbox_spool_overlay import ModelAgentInboxSpoolOverlay

LaneId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]


class ModelAgentInboxSpoolRequest(ModelFrozen):
    host: LaneId
    lanes: Annotated[tuple[LaneId, ...], Field(min_length=1)]
    overlay: ModelAgentInboxSpoolOverlay | None = None
