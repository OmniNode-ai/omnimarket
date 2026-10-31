# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One prepared credential-row upsert, executed as a single atomic statement."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCredentialRowWrite:
    """An in-process writer plan, never a customer-supplied query or event.

    OMN-21217: the statement raises ``version`` in SQL (``version = version +
    1`` on conflict), so two writers can never both read one version and lose
    an increment.
    """

    tenant_id: str
    parameters: Mapping[str, object]
    sqlite_write: str
    postgres_write: str
