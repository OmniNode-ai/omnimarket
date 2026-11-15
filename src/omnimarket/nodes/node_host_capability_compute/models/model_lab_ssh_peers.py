# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows for the ``lab-ssh`` peer probe: which hosts each lab host must reach, and by which destinations.

The ``lab-ssh`` capability of the parity probe judges a lab host by an ssh route to the other lab hosts. Two kinds of
row of the lab host table must not be demanded of it: machines the lab does not own (``lab_ssh_peer`` false, whose
login and keys are their owner's) and rows marked unavailable. A peer is named by the destinations that resolve from
the probing host — the row's ``target``, then each ``fallback_targets`` — so the probe tries one route per peer, not
one per host. This node holds only names from the table; it opens no connection.
"""

from pydantic import BaseModel, ConfigDict


class ModelLabSshPeerRow(BaseModel):
    """One lab host table row as the peer probe reads it.

    ``ssh_user`` prefixes ``target``; ``fallback_ssh_user`` prefixes each ``fallback_targets`` destination and,
    when the row names none, ``ssh_user`` does. ``lab_ssh_peer`` false marks a machine the lab does not own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    target: str
    fallback_targets: tuple[str, ...] = ()
    ssh_user: str | None = None
    fallback_ssh_user: str | None = None
    available: bool = True
    lab_ssh_peer: bool = True


class ModelLabSshPeersRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    rows: tuple[ModelLabSshPeerRow, ...]


class ModelLabSshPeerExclusion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    reason: str


class ModelLabSshPeersResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # One probe token per peer, in row order; empty with a refusal when the probing host names no row.
    peers: tuple[str, ...]
    excluded: tuple[ModelLabSshPeerExclusion, ...]
    refusal: str | None = None
