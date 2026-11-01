# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay bundle file and verify request shared by the manifest builder and the bundle writer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelBundleFile(_Frozen):
    path: str
    content: bytes


class ModelVerifyBundleRequest(_Frozen):
    manifest_yaml: str
    bundle_files: tuple[ModelBundleFile, ...]
