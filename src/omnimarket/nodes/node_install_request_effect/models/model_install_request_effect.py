# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the installer on one host did with one install request."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.install_request import (
    ModelInstallRequestDecision,
    ModelInstallRequestResult,
)


class ModelInstallRequestHandled(BaseModel):
    """The decision, and the result to publish when the host answered the request.

    ``result`` is ``None`` for ``ignore`` (the request is another host's), ``skip_done`` (already verified) and ``hold``
    (not safe yet: the host keeps the request and decides again on its next pass).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ModelInstallRequestDecision
    result: ModelInstallRequestResult | None = None
