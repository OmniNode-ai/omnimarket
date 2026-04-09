# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class DispatchTrace(BaseModel):
    trace_id: str
    trace_data: dict | None = None
    traced_at: str | None = None
