# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a registered overlay-store kind declares: the bootstrap inputs and the secrets only it needs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelStoreBackendSpec(BaseModel):
    """One kind of overlay store, as its backend registers it under the ``onex.backends`` group.

    ``inputs`` are the names of the bootstrap inputs the kind requires (never their values); ``secrets`` are the
    names of the secrets the wiring fetches from Infisical for this kind and no other.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    inputs: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()


__all__ = ["ModelStoreBackendSpec"]
