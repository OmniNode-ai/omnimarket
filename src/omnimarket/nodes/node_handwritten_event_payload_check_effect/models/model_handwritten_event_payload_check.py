# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed repository scan request and verdict."""

from pydantic import BaseModel, ConfigDict


class ModelHandwrittenEventPayloadCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repo_root: str


class ModelHandwrittenEventPayloadCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    admitted: bool
    exit_code: int
    scanned_files: int
    violations: int
    output_lines: tuple[str, ...]
