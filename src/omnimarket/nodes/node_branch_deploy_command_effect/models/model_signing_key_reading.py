# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The signing key reading of node_branch_deploy_command_effect (OMN-24611).

``status`` ``read`` carries the key; ``refused`` carries why the store did not
return it. The key is a ``SecretStr`` excluded from serialization and from
repr, so no print, log, dump or JSON of this model can carry it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ModelSigningKeyReading(BaseModel):
    """The reading of the deploy agent's signing key: ``read`` carries it, ``refused`` carries why it was not returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["read", "refused"]
    detail: str = ""
    #: The key itself. Never serialized: excluded from ``model_dump``, from
    #: JSON and from repr, so no print, log or dump carries it.
    key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
