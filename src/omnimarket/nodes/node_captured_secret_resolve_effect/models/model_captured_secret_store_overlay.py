# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the captured-secret namespace lives, from the private overlay (OMN-20926).

The overlay is the machine's own ``~/.onex/config.yaml``, block
``captured_secret_store``: store addressing and identity NAMES and REFERENCES,
never a value. Values sit in ``~/.onex/credentials.json`` (mode 0600), the same
split ``onex auth login`` and ``onex auth lane-login`` already use. Nothing about
any deployment's store is committed to this package.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: The block name inside ``~/.onex/config.yaml``. The hook producer in
#: omniclaude reads the same block, with its own writer identity keys.
CAPTURED_SECRET_STORE_BLOCK = "captured_secret_store"


class ModelCapturedSecretStoreOverlay(BaseModel):
    """Store addressing for the captured namespace, plus the READER identity.

    The writer's identity keys (``writer_client_id``, ``writer_client_secret_ref``,
    ``reference_key_ref``) are in the same block and ignored here
    (``extra="ignore"``): a reader authenticates as itself, never as the writer.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    infisical_addr: str = Field(min_length=1)
    project_id: uuid.UUID
    environment_slug: str = Field(min_length=1)
    secret_path: str = Field(min_length=1, pattern=r"^/")
    #: The reader's machine-identity client id. Not a secret.
    reader_client_id: str | None = Field(default=None, min_length=1)
    #: The key the reader's client secret is filed under in credentials.json.
    reader_client_secret_ref: str | None = Field(default=None, min_length=1)


def load_captured_secret_store_overlay(
    config: Mapping[str, object], *, source: str = "config.yaml"
) -> ModelCapturedSecretStoreOverlay:
    """Read the block out of a parsed config, naming every bad key (never a value).

    Raises:
        ValueError: the block is absent, not a mapping, or missing a key.
    """
    block = config.get(CAPTURED_SECRET_STORE_BLOCK)
    if not isinstance(block, Mapping):
        raise ValueError(
            f"{source} has no '{CAPTURED_SECRET_STORE_BLOCK}' block; this machine "
            "is not configured to reach the captured-secret store"
        )
    try:
        return ModelCapturedSecretStoreOverlay.model_validate(dict(block))
    except ValidationError as exc:
        fields = sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]})
        raise ValueError(
            f"{source} block '{CAPTURED_SECRET_STORE_BLOCK}' is missing or "
            f"malformed: {fields}"
        ) from None


__all__ = [
    "CAPTURED_SECRET_STORE_BLOCK",
    "ModelCapturedSecretStoreOverlay",
    "load_captured_secret_store_overlay",
]
