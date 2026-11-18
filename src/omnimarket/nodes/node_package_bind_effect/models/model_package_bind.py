# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the package bind effect (OMN-24321).

Standard library and pydantic only: the landing controller loads this node in an environment that holds
nothing else.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelPackageBindRequest(BaseModel):
    """Bind ``omnimarket.nodes.<node>`` found under ``src`` without running its package ``__init__``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    src: str
    node: str


class ModelPackageBindResult(BaseModel):
    """The bind's outcome; ``reason`` names why a request was refused and is empty when it bound."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: Literal["bound", "refused"]
    package: str = ""
    path: str = ""
    reason: str = ""
