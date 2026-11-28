# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the settings store validates the Claude harness tier settings through (OMN-26219).

One row per Claude harness tier (the tier's backend id): the credentials the tier runs on, in the order it tries
them, and the effort it runs at. A credential is the subscription login or the API key held in the secret store
under the name the delegation_adapters agent entry declares in ``secret_env``; no key and no key name lives here.
No tier has a default: a tier with no row is refused by the harness, by the name of the row.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ClaudeCredential = Literal["subscription", "api_key"]


class ModelClaudeHarnessTier(BaseModel):
    """What one Claude tier runs on: the credential order and, optionally, its ``--effort``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    credential_order: list[ClaudeCredential] = Field(min_length=1)
    effort: str | None = Field(default=None, min_length=1)


class ModelClaudeHarnessTiersOverlay(BaseModel):
    """The ``claude_harness_tiers`` overlay: tier settings by backend id."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    tiers: dict[str, ModelClaudeHarnessTier] = Field(default_factory=dict)
