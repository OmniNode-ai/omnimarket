# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``lane_commit_signing`` overlay: which hosts sign a lab lane's commits and with which key (OMN-22993).

The remote-lane runner reads ``<overlay root>/lane_commit_signing/overlay.yaml`` and, for the host a
lane is placed on, exports ``commit.gpgsign``, ``gpg.format=ssh`` and ``user.signingkey`` (and
``user.email`` when the host row names one) to the lane's shell and its commit-patch step. A cell is
written into shell by the runner, so this model holds the same limits the runner applies: a key is a
plain path with no ``..`` segment and an email is a plain address. It imports pydantic alone, so the
overlay home's check, which installs pydantic and pyyaml and nothing else, can validate the document
through it. No host, key or address lives here.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

# The name of a lab host as the lab hosts overlay names it.
HostName = Annotated[
    str, StringConstraints(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]
# The private half of an ssh key, relative to the host's home or absolute (the runner's SIGNING_KEY_RE).
_KEY = r"^[A-Za-z0-9._/@+-]{1,200}$"
# The committer address of the GitHub account that carries the key's public half (the runner's SIGNING_EMAIL_RE).
_EMAIL = r"^[A-Za-z0-9._+-]{1,100}@[A-Za-z0-9.-]{1,100}$"


def _no_parent_segment(value: str) -> str:
    if ".." in value.split("/"):
        raise ValueError("names a parent directory segment")
    return value


KeyPath = Annotated[
    str, StringConstraints(pattern=_KEY), AfterValidator(_no_parent_segment)
]
Email = Annotated[str, StringConstraints(pattern=_EMAIL)]


class ModelLaneCommitSigningHost(BaseModel):
    """The signing settings of one host."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    key: KeyPath
    email: Email | None = None


class ModelLaneCommitSigningOverlay(BaseModel):
    """``lane_commit_signing``: the signing settings of each host that signs, by host name.

    A host with no row commits unsigned.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    hosts: dict[HostName, ModelLaneCommitSigningHost] = Field(min_length=1)

    def signing_for(self, host: str) -> ModelLaneCommitSigningHost | None:
        """The row of ``host``, or nothing for a host the overlay does not name."""
        return self.hosts.get(host)


__all__ = [
    "ModelLaneCommitSigningHost",
    "ModelLaneCommitSigningOverlay",
]
