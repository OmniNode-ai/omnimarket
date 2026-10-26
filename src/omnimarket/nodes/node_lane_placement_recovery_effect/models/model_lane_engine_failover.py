# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner's ports for bounded quota failover (OMN-17427)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelLaneEngineFailoverRequest:
    args: Any
    execute: Callable[..., int]
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class ModelLaneEngineFailoverResult:
    exit_code: int
