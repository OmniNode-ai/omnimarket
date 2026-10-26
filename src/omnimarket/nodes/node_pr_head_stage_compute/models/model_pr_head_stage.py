# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and rendered shell for a remote lane's ``--pr`` heads."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrHead:
    pr: str
    repo: str
    head_ref: str
    head_sha: str


@dataclass(frozen=True)
class ModelPrHeadStageRequest:
    heads: tuple[ModelPrHead, ...]
    owner: str
    path_export: str
    registry_root: str | None = None
    repo: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class ModelPrHeadStageResult:
    fetch_script: str
    stage_script: str
