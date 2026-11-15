# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request for installing the public host keys the lab hosts declare into known_hosts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.overlay_keys import ModelLabHostsTable


class ModelLabHostDeclaredHostKeysInstallRequest(BaseModel):
    """Install the declared host keys of every declaring row into the named hosts' known_hosts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[str, ...] = ()
    table: ModelLabHostsTable | None = None
    table_path: str | None = None
    dry_run: bool = False
    local_host: str | None = None
    timeout_s: int = Field(default=30, ge=1)
