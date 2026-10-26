# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs and results at the remote lane's GitHub process boundary."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelLaneGithubCall:
    policy: Mapping[str, Any]
    tool: str
    args: tuple[str, ...]
    env: Mapping[str, str]


@dataclass(frozen=True)
class ModelLaneGithubResult:
    code: int
    stdout: str = ""
    stderr: str = ""
