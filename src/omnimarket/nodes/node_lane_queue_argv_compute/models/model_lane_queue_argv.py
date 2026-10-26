# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for a queued lane's relaunch command."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelLaneQueueArgvRequest:
    inputs: Mapping[
        str, Any
    ]  # the run's parsed arguments (``vars`` of the runner's namespace)
    brief: str  # the brief the run pinned


@dataclass(frozen=True)
class ModelLaneQueueArgvResult:
    argv: tuple[str, ...]  # the runner's command after the script, without --host
