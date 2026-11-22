# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The on-demand run command of the host hygiene orchestrator (OMN-24610).

``onex run-node`` publishes it on the node's command topic. The command's
correlation id is the caller's: the id the terminal run event carries back so
the caller matches the answer to its request.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelHostHygieneRunRequested(BaseModel):
    """One on-demand host hygiene run, asked by its correlation id.

    ``correlation_id`` is the caller's correlation id (a JSON string is
    accepted): ``onex run-node`` injects it from the command envelope, and the
    run's terminal event carries it back to the caller.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID


__all__: list[str] = ["ModelHostHygieneRunRequested"]
