# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam that mints a fresh GitHub App installation token for one run.

The overlay names the secret-store refs; the minter resolves them and mints. A mint that
fails raises ``ExposureTokenUnavailableError`` whose message names the failing ref or step and
the exception type only, never a value or a response body.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    ModelGithubAppMint,
)


class ExposureTokenUnavailableError(Exception):
    """The App token could not be minted for this run; the read ends UNKNOWN."""


class ProtocolTokenMinter(Protocol):
    """Mints one installation token from the refs the overlay names."""

    def mint(self, *, org: str, mint: ModelGithubAppMint) -> str: ...


__all__ = ["ExposureTokenUnavailableError", "ProtocolTokenMinter"]
