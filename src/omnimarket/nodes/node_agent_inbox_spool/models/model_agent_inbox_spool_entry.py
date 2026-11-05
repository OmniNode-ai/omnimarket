# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One spooled MSG, RELEASE or HOLD row, as written for a lane's hook shim to inject."""

from typing import Literal

from ._base import ModelFrozen

SpoolRowType = Literal["MSG", "RELEASE", "HOLD"]


class ModelAgentInboxSpoolEntry(ModelFrozen):
    row_id: str
    row_type: SpoolRowType = "MSG"
    msg_id: str
    sender: str
    recipients: tuple[str, ...]
    ticket: str
    row_timestamp: str
    text: str
    raw_row: str
    consumer_group: str
