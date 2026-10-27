# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and rendered shell fragments for Codex landing preparation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCodexLandingRequest:
    worktree: str


@dataclass(frozen=True)
class ModelCodexLandingResult:
    clean_command: str
    json_reader: str
