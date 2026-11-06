# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One (consumer_group, topic) seam of the status page flow panel at its latest window."""

from pydantic import BaseModel, ConfigDict


class ModelFlowConsumer(BaseModel):
    """One (consumer_group, topic) seam at its most recent observed window."""

    model_config = ConfigDict(frozen=True)

    consumer_group: str
    topic: str
    flow_state: str
    messages_in: int
    messages_out: int
    messages_dlq: int
    handler_errors: int
    upstream_evidence: str
    window_end: str
