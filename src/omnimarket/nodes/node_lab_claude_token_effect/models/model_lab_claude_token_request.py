# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the lab Claude token effect is asked to do.

``set``: the operator made a token (``claude setup-token``) and pipes it in; store it
under the host's account reference, deploy it to the host and probe it.
``add_spare``: store a token as a spare account in the pool, deploying nothing.
``rotate_limited``: placement benched the host (``.limited-<host>``); move it onto a
spare account with headroom, or say once that no spare exists.

The token travels only as a ``SecretStr`` and never appears in ``repr``, a result or
a refusal.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

# A host or account name becomes one dotted segment of a store reference and a
# marker file name, so it is a plain lowercase slug.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ModelLabTokenHost(BaseModel):
    """A lab host as the lab host table names it: ``target`` is what ssh dials;
    ``local`` is the launching host, run under ``bash -c`` instead of ssh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    target: str | None = None
    local: bool = False

    @model_validator(mode="after")
    def _check(self) -> ModelLabTokenHost:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"host name {self.name!r} is not a plain lowercase slug")
        if not self.local and not self.target:
            raise ValueError(f"host {self.name} is not local and names no ssh target")
        return self


class ModelLabClaudeTokenRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["set", "add_spare", "rotate_limited"]
    # The host to deploy to or rotate; None only for ``add_spare``.
    host: ModelLabTokenHost | None = None
    # The account the piped token belongs to (``set``, ``add_spare``).
    account: str | None = None
    token: SecretStr | None = Field(default=None, repr=False)
    # The landing placement directory holding ``.limited-<host>`` markers.
    placement_dir: str
    dry_run: bool = False

    @model_validator(mode="after")
    def _check(self) -> ModelLabClaudeTokenRequest:
        if self.operation != "add_spare" and self.host is None:
            raise ValueError(f"{self.operation} names no host")
        if self.operation in ("set", "add_spare"):
            if not self.account or not _NAME_RE.match(self.account):
                raise ValueError(
                    f"{self.operation} needs an account name that is a plain lowercase slug"
                )
            if not self.dry_run and (
                self.token is None or not self.token.get_secret_value().strip()
            ):
                raise ValueError(
                    f"{self.operation} received no token on stdin; the token is read from stdin only, "
                    "never from an argument"
                )
        return self
