# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed repository scan request and refusal result."""

from pydantic import BaseModel, ConfigDict


class ModelRetiredModelReferenceCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repo_root: str
    paths: tuple[str, ...] = ()


class ModelRetiredModelReferenceCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    admitted: bool
    exit_code: int
    output_lines: tuple[str, ...]
