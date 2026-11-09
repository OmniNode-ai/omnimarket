# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Say why the host-drift fleet fold does not exist, from what the manifest declares (OMN-23275).

A missing fold file is not a failed run. The host-drift manifest gives the host-drift unit to named
hosts only, the unit writes the fold on the host it runs on, and nothing copies that file to another
host. So the finding names the declaring hosts and whether this one is among them, and its FIX is an
action that exists on the host it names. Ported from the internal tooling repository (OMN-17427); the unit,
manifest, installer and ticket words ride on the request, read by the caller from its overlay.
"""

from __future__ import annotations

from ..models import ModelFleetFoldAbsence


def describe(absence: ModelFleetFoldAbsence) -> tuple[str, str]:
    """The ``why`` and ``fix`` of the running-vs-merged finding for a fold file that is absent."""
    unit, manifest, ticket = absence.unit, absence.manifest, absence.ticket
    head = f"host-drift fleet fold {absence.path} does not exist on {absence.this_host}"
    if not absence.owners:
        return (
            f"{head}: no host in {manifest} declares {unit}, so the fold has no producer",
            f"declare a host for {unit} in {manifest} under {ticket}; until then this loop "
            "cannot be judged",
        )
    owners = ", ".join(absence.owners)
    if absence.declared_here and absence.plist_installed:
        return (
            f"{head}: {unit} is declared for this host and its plist is installed, but it has "
            "written no fold",
            f"read its run on this host: tail ~/.omninode/host-drift/launchd.out.log and "
            f"launchctl list {unit}",
        )
    if absence.declared_here:
        return (
            f"{head}: {unit} is declared for this host ({owners}) and its plist is not installed",
            f"install it with the host drift installer (the operator runs it): "
            f"{absence.installer}",
        )
    return (
        f"{head}: {unit} is declared only for {owners} by {manifest}; {absence.this_host} is not "
        "in the manifest, nothing here writes the fold and no copy of it reaches this host",
        f"read the fold on a declaring host ({owners}): launchctl list {unit} and tail "
        f"~/.omninode/host-drift/launchd.out.log there; to judge this loop from "
        f"{absence.this_host} the operator declares it in {manifest} ({ticket}) and runs "
        f"{absence.installer}",
    )
