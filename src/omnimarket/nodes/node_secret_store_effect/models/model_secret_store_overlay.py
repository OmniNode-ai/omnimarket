# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the secret store is and who this machine is to it (OMN-20944).

Deployment facts come from the machine's private overlay, never from this
package: the ``secret_store`` block of ``<onex_home>/config.yaml``. The block
holds addressing and the NAMES of the identity's credentials, never a value.
Each ``*_ref`` names an entry the bootstrap secret store resolves at run time:
by default ``<onex_home>/credentials.json`` (refused unless mode 0600), or the
runtime's own ``ProtocolSecretStore`` when one is injected.

The shipped default is no block at all, which the handler answers with
``not_configured``: a machine that has not been given a store reaches none.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: The block name inside ``<onex_home>/config.yaml``.
SECRET_STORE_BLOCK = "secret_store"


class ModelSecretStoreOverlay(BaseModel):
    """Store addressing and the identity's credential references."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    #: Which adapter speaks to the store. Matched inside the node's adapters
    #: package, the only place a store product is named.
    provider: str = Field(min_length=1)
    address: str = Field(min_length=1, pattern=r"^https?://")
    project_id: uuid.UUID
    environment: str = Field(min_length=1)
    #: Every folder a request names is taken relative to this one.
    root_folder: str = Field(default="/", pattern=r"^/")
    #: The machine identity's client id, by reference.
    client_id_ref: str = Field(min_length=1)
    #: The machine identity's client secret, by reference.
    client_secret_ref: str = Field(min_length=1)
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)


def load_secret_store_overlay(
    config: Mapping[str, object], *, source: str = "config.yaml"
) -> ModelSecretStoreOverlay | None:
    """The block, or ``None`` when the config has none.

    Raises:
        ValueError: the block is present but not a mapping or is malformed;
            the message names the fields, never a value.
    """
    block = config.get(SECRET_STORE_BLOCK)
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise ValueError(f"{source} block '{SECRET_STORE_BLOCK}' is not a mapping")
    try:
        return ModelSecretStoreOverlay.model_validate(dict(block))
    except ValidationError as exc:
        fields = sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]})
        raise ValueError(
            f"{source} block '{SECRET_STORE_BLOCK}' is missing or malformed: {fields}"
        ) from None


__all__ = [
    "SECRET_STORE_BLOCK",
    "ModelSecretStoreOverlay",
    "load_secret_store_overlay",
]
