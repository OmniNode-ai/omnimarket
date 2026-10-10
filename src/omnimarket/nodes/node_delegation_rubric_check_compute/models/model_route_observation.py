# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A receipt field read by the route checker."""

from pydantic import BaseModel, ConfigDict, JsonValue


class ModelRouteObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    field: str
    value: JsonValue
