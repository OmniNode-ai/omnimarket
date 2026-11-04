# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request for one house model key (OMN-22417).

A house model key (``LLM_GLM_API_KEY``) lives in the secret store's house
folder, never in a host file. The request names only the key; the folder is the
handler's, so a request cannot reach a customer folder.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: A house inference-provider key: ``LLM_<PROVIDER>_API_KEY``.
HOUSE_MODEL_KEY_NAME_PATTERN = r"^LLM_[A-Z0-9]+(_[A-Z0-9]+)*_API_KEY$"


class ModelHouseModelKeyRequest(BaseModel):
    """Resolve one house model key by its variable name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=HOUSE_MODEL_KEY_NAME_PATTERN)
    correlation_id: UUID | None = None


__all__ = ["HOUSE_MODEL_KEY_NAME_PATTERN", "ModelHouseModelKeyRequest"]
