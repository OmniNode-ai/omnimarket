# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which hosts declare the host-drift unit, read from the deployment overlay (OMN-23275)."""

from pydantic import BaseModel, ConfigDict, Field


class ModelHostDriftDeclaredHost(BaseModel):
    """One host the deployment gives the host-drift unit: its id and the names it answers to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    hostnames: tuple[str, ...] = Field(min_length=1)


class ModelHostDriftDeclaration(BaseModel):
    """The declaration the running-vs-merged finding for an absent fold reads.

    ``hosts`` lists only the hosts that run ``unit``; an empty list says nobody declares it.
    ``manifest`` is the name of the document that declares them, as the finding cites it, ``installer``
    the command the operator runs to install the unit and ``ticket`` the ticket a missing declaration
    is raised under. ``plist_dir`` is where launchd keeps the unit's plist, relative to the home directory.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str = Field(min_length=1)
    manifest: str = Field(min_length=1)
    installer: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    plist_dir: str = Field(min_length=1)
    hosts: tuple[ModelHostDriftDeclaredHost, ...] = ()
