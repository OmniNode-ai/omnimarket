# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The append node's own deployment overlay as a second source of writers (OMN-24330).

A service login such as the dev runtime's own has no person login or device, so it cannot be a
row of the developer principal overlay. Its writers are declared in the node's own deployment
overlay, found the way every node overlay is found.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    find_node_overlay,
)
from omnimarket.work_ledger_bus.bus import (
    WORK_LEDGER_APPEND_NODE,
    node_contract,
)


class ModelServiceWritersPolicy(BaseModel):
    """The service-writers section of the append node's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str = Field(min_length=1)
    roots_env: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)


def load_service_writers_policy() -> ModelServiceWritersPolicy:
    """The service-writers policy the append node's contract declares."""
    return ModelServiceWritersPolicy.model_validate(
        node_contract(WORK_LEDGER_APPEND_NODE)["service_writers"]
    )


class ModelServiceWriterRow(BaseModel):
    """One service writer declared in the node's deployment overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str
    why: str = Field(min_length=1)


class ModelServiceWritersOverlay(BaseModel):
    """The parsed service-writers overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    writers: tuple[ModelServiceWriterRow, ...] = Field(min_length=1)


def service_writers_path(
    policy: ModelServiceWritersPolicy, environment: Mapping[str, str] | None = None
) -> Path | None:
    """The node's deployment overlay under the declared roots env, or None when absent."""
    env = os.environ if environment is None else environment
    if policy.roots_env != OVERLAY_ROOTS_ENV:
        raise ValueError(
            f"service writers overlay roots env must be {OVERLAY_ROOTS_ENV!r}, "
            f"the constant find_node_overlay reads"
        )
    return find_node_overlay(
        policy.overlay_id, {policy.roots_env: env.get(policy.roots_env, "")}
    )


def service_writers(
    text: str, policy: ModelServiceWritersPolicy, principal_pattern: str
) -> frozenset[str]:
    """The service writers a deployment overlay declares, checked against the pattern."""
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(
            f"service writers overlay is not valid yaml: {error}"
        ) from error
    if not isinstance(data, dict):
        raise ValueError("service writers overlay is not a mapping")
    try:
        overlay = ModelServiceWritersOverlay.model_validate(data)
    except ValidationError as error:
        raise ValueError(f"service writers overlay is malformed: {error}") from error
    if overlay.schema_version != policy.schema_version:
        raise ValueError(
            f"service writers overlay schema_version {overlay.schema_version!r} "
            f"does not match {policy.schema_version!r}"
        )
    seen: set[str] = set()
    writers: set[str] = set()
    for row in overlay.writers:
        if row.principal in seen:
            raise ValueError(
                f"service writers overlay declares {row.principal!r} more than once"
            )
        seen.add(row.principal)
        if re.fullmatch(principal_pattern, row.principal) is None:
            raise ValueError(
                f"service writers overlay principal {row.principal!r} does not match {principal_pattern!r}"
            )
        writers.add(row.principal)
    return frozenset(writers)
