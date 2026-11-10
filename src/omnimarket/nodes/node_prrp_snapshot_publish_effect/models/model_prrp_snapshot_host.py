# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab host the snapshot publisher writes to: one row of the lab host table, as far as the copy needs it."""

from pydantic import BaseModel, ConfigDict


class ModelPrrpSnapshotHost(BaseModel):
    """The host's display name, ssh destination, ssh route and availability.

    ``ssh_user``, ``proxy_jump`` and ``ssh_identity`` are the table row's route to ``target``; the
    ``fallback_*`` cells are the route to each ``fallback_targets`` destination, and a fallback cell left
    out takes the row-level cell, as the launcher reads the same row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    target: str
    local: bool = False
    available: bool = True
    unavailable_reason: str = ""
    fallback_targets: tuple[str, ...] = ()
    ssh_user: str | None = None
    proxy_jump: str | None = None
    ssh_identity: str | None = None
    fallback_ssh_user: str | None = None
    fallback_proxy_jump: str | None = None
    fallback_ssh_identity: str | None = None
