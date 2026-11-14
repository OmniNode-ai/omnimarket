# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Split the J2 hosts into the ones probed and the ones parked in the host table.

A host the table lists with ``pooled`` False is parked: it is not probed and its reason is
reported as a skip line. A host the table does not list is probed (placement will refuse it
typed, as before). An unreadable table never hides a host: every host is probed and a warning
is returned.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_host_reading import ModelCanaryHost
from omnimarket.models.model_join_j2 import ModelJoinJ2HostResult


class ProtocolJ2HostInventory(Protocol):
    """What the split reads of the host table: its rows."""

    def hosts(self) -> Sequence[ModelCanaryHost]: ...


def split_probed_and_parked(
    hosts: Sequence[str], inventory: ProtocolJ2HostInventory
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], str]:
    """The hosts to probe, the parked ``(host, reason)`` pairs, and a warning (empty normally).

    ``probed`` keeps the order given; a host the table does not list is probed. ``parked`` keeps
    the order given and carries each host's table reason. When the table cannot be read
    (``ValueError``, ``OSError`` or ``NodeOverlayError``) every host is probed and the warning
    names the error.
    """
    try:
        table = {row.name: row for row in inventory.hosts()}
    except (ValueError, OSError, NodeOverlayError) as exc:
        return (
            tuple(hosts),
            (),
            f"host table unread, every host probed: {type(exc).__name__}",
        )
    probed: list[str] = []
    parked: list[tuple[str, str]] = []
    for host in hosts:
        row = table.get(host)
        if row is not None and not row.pooled:
            parked.append((host, row.reason))
        else:
            probed.append(host)
    return tuple(probed), tuple(parked), ""


def parked_line(host: str, reason: str) -> str:
    """One skip line for a host parked in the host table."""
    return f"J2-SKIP host={host} reason=parked-in-host-table detail={reason!r}"


DETAIL_CHARS = 200


def failure_report(results: Sequence[ModelJoinJ2HostResult]) -> str | None:
    """The one-line stderr report of a run in which a host did not read golden; ``None`` when every host did.

    The publisher keeps the last stderr lines as the step's diagnostic, so the line names each failed host, its outcome and its
    typed failure code on one line, a multi-line detail folded (the J5 step's ``failure_report`` shape, OMN-23717).
    """
    failed = [r for r in results if not r.ok]
    if not failed:
        return None
    return "J2-FAILED " + "; ".join(
        f"{r.host} {r.outcome.value} {r.failure_code or 'none'}: {' '.join(r.detail.split())[:DETAIL_CHARS]}"
        for r in failed
    )
