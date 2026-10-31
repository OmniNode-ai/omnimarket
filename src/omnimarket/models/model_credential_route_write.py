# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prepared credential SQL and inputs, executed within one driver transaction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCredentialRouteWrite:
    """An in-process writer plan, never a customer-supplied query or event."""

    row: Mapping[str, object]
    expected_version: int | None
    parameters: Mapping[str, object]
    sqlite_write: str
    postgres_write: str
    sqlite_read: str
    postgres_read: str
    sqlite_models_write: str | None = None
    postgres_models_write: str | None = None
