# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One spooled MSG, as written for a lane's hook shim to inject."""

from ._base import ModelFrozen


class ModelAgentInboxSpoolEntry(ModelFrozen):
    row_id: str
    msg_id: str
    sender: str
    recipients: tuple[str, ...]
    ticket: str
    row_timestamp: str
    text: str
    raw_row: str
    consumer_group: str
