# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates the etcd_lab_members overlay document through (OMN-24562).

The lab etcd voter set is one document: the members of the cluster, with the rules the lab etcd provisioning script
expects of its members.tsv (five voters, unique names, at most one learner). The model
merges ahead of the document and of every reader; ``render_members_tsv`` writes the text members.tsv holds so the
script's file and the overlay can be compared line for line. No value lives here, and imports are pydantic alone.
"""

from __future__ import annotations

import ipaddress
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The column line of members.tsv (the file's one comment line that names its columns).
MEMBERS_TSV_HEADER = "#name\tssh_target\tos\ttailnet_ip\tdns_name\tdata_dir\tunit"

_VOTERS = 5
_UNITS_BY_OS: dict[str, frozenset[str]] = {
    "darwin": frozenset({"launchd"}),
    "linux": frozenset({"systemd", "systemd-user"}),
}


class ModelEtcdLabMember(BaseModel):
    """One member of the lab etcd cluster."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    #: Where the provisioning reaches the host: the lab host table's target, or ``local`` (the host it runs on).
    ssh_target: Literal["lab_run_hosts", "local"]
    os: Literal["darwin", "linux"]
    #: Peer and client traffic use this tailnet address.
    tailnet_ip: str
    dns_name: str = Field(min_length=1)
    data_dir: str = Field(min_length=1)
    #: launchd (a LaunchAgent of the ssh user), systemd (a system unit run as the ssh user), systemd-user (a user unit with linger).
    unit: Literal["launchd", "systemd", "systemd-user"]
    client_port: int = Field(ge=1, le=65535)
    peer_port: int = Field(ge=1, le=65535)
    role: Literal["voter", "learner"]
    #: A host that sleeps or leaves the network takes its member down; the set bounds how many such voters it holds.
    laptop: bool
    #: The ``name`` of this host's row in the lab host table (the lab_hosts overlay document).
    host_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def _member_is_well_formed(self) -> Self:
        try:
            address = ipaddress.ip_address(self.tailnet_ip)
        except ValueError as error:
            raise ValueError(
                f"{self.name}: tailnet_ip {self.tailnet_ip!r} is not an address"
            ) from error
        # The tailnet's own block is the shared address space: neither global, private, loopback, link-local nor multicast.
        if (
            address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
        ):
            raise ValueError(
                f"{self.name}: tailnet_ip {self.tailnet_ip} is not a tailnet address"
            )
        if self.unit not in _UNITS_BY_OS[self.os]:
            raise ValueError(f"{self.name}: unit {self.unit} cannot run on {self.os}")
        if self.client_port == self.peer_port:
            raise ValueError(f"{self.name}: client and peer ports must differ")
        return self

    def tsv_row(self) -> str:
        """This member as one members.tsv row."""
        return "\t".join(
            (
                self.name,
                self.ssh_target,
                self.os,
                self.tailnet_ip,
                self.dns_name,
                self.data_dir,
                self.unit,
            )
        )


class ModelEtcdLabMembersOverlay(BaseModel):
    """The lab etcd member set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    members: tuple[ModelEtcdLabMember, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _set_holds_the_scripts_rules(self) -> Self:
        for field in ("name", "tailnet_ip", "host_ref"):
            values = [getattr(m, field) for m in self.members]
            repeated = sorted({v for v in values if values.count(v) > 1})
            if repeated:
                raise ValueError(f"members: {field} repeats: {repeated}")
        if len(self.voters()) != _VOTERS:
            raise ValueError(
                f"members: the lab cluster has exactly five voters, this document has {len(self.voters())}"
            )
        if len(self.learners()) > 1:
            raise ValueError("members: at most one learner at a time")
        if sum(1 for m in self.voters() if m.laptop) > 1:
            raise ValueError(
                "members: at most one laptop voter (two sleeping hosts must not cost quorum)"
            )
        return self

    def voters(self) -> tuple[ModelEtcdLabMember, ...]:
        """The voters, in document order."""
        return tuple(m for m in self.members if m.role == "voter")

    def learners(self) -> tuple[ModelEtcdLabMember, ...]:
        """The learners, in document order."""
        return tuple(m for m in self.members if m.role == "learner")

    def render_members_tsv(self) -> str:
        """The rows members.tsv holds: its column line, then one row per voter. A learner is never a row of the file."""
        return (
            "\n".join([MEMBERS_TSV_HEADER, *(m.tsv_row() for m in self.voters())])
            + "\n"
        )
