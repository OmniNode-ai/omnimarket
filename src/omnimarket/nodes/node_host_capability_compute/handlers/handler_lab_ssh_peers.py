# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Select the lab hosts one lab host's ``lab-ssh`` probe must try a route to, and name each by its destinations.

The capability half of the parity probe (OMN-21373) measures ssh to the other lab hosts, and the probe
demands a route to every other row of the host table: two rows name machines the lab does not own, whose login and
keys are their owner's, so no lab host could ever hold the route. This node reads the table only — it skips rows that
say so (``lab_ssh_peer`` false), the probing host itself and unavailable rows, and joins each remaining peer's
destinations into one probe token. It opens no connection.
"""

from __future__ import annotations

from ..models.model_lab_ssh_peers import (
    ModelLabSshPeerExclusion,
    ModelLabSshPeerRow,
    ModelLabSshPeersRequest,
    ModelLabSshPeersResult,
)

NOT_LAB_OWNED = "not a lab-owned peer (lab_ssh_peer is false)"
PROBING_HOST_ITSELF = "the probing host itself"
UNAVAILABLE = "unavailable"


def peer_destinations(row: ModelLabSshPeerRow) -> tuple[str, ...]:
    """The destinations that name ``row``'s host from the probing host, in try order, none repeated."""
    fallback_user = (
        row.fallback_ssh_user if row.fallback_ssh_user is not None else row.ssh_user
    )

    def named(destination: str, user: str | None) -> str:
        return f"{user}@{destination}" if user else destination

    return tuple(
        dict.fromkeys(
            (
                named(row.target, row.ssh_user),
                *(named(d, fallback_user) for d in row.fallback_targets),
            )
        )
    )


class HandlerLabSshPeers:
    def handle(self, request: ModelLabSshPeersRequest) -> ModelLabSshPeersResult:
        if not any(row.name == request.host for row in request.rows):
            return ModelLabSshPeersResult(
                peers=(),
                excluded=(),
                refusal=f"unknown probing host: no row of the host table is named {request.host}",
            )
        peers: list[str] = []
        excluded: list[ModelLabSshPeerExclusion] = []

        def exclude(host: str, reason: str) -> None:
            excluded.append(ModelLabSshPeerExclusion(host=host, reason=reason))

        for row in request.rows:
            if not row.lab_ssh_peer:
                exclude(row.name, NOT_LAB_OWNED)
            elif row.name == request.host:
                exclude(row.name, PROBING_HOST_ITSELF)
            elif not row.available:
                exclude(row.name, UNAVAILABLE)
            else:
                peers.append("+".join(peer_destinations(row)))
        return ModelLabSshPeersResult(peers=tuple(peers), excluded=tuple(excluded))
