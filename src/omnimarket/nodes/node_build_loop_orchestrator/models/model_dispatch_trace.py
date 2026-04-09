# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class DispatchTrace(BaseModel):
    trace_id: str
    dispatched_at: str | None = None
    target_node: str
