# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the phase runtime probe tool to its typed handler."""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel

from ..handlers.handler_morning_runtime_probe import HandlerMorningRuntimeProbe
from ..models.model_morning_runtime_probe import (
    ModelMorningRuntimeProbeCommand,
    ModelMorningRuntimeProbeRequest,
)
from .base import MorningTool, Result
from .context import ModelMorningToolContext


def runtime_probe(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    result = HandlerMorningRuntimeProbe().handle(
        ModelMorningRuntimeProbeCommand(
            context=ctx, **cast(ModelMorningRuntimeProbeRequest, params).model_dump()
        )
    )
    return cast(Result, result.model_dump(mode="json"))


RUNTIME_PROBE_TOOL = MorningTool(
    "runtime_probe",
    "Execute the declared source row's HTTP Done-proof readback on the declared dev "
    "runtime host. Select a document key, source_sha256 and one-based table-row line. Returns "
    "fresh output, source hashes and execution context; never infers a ladder stage. "
    "Supports curl GET and JSON field/equality readbacks; refuses other commands.",
    ModelMorningRuntimeProbeRequest,
    "read",
    runtime_probe,
)
