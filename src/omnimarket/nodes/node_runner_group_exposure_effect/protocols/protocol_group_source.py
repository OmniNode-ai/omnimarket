# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols of the probe's two seams: the group source and the HTTP getter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_probe import (
    ModelGroupRead,
)


class ProtocolGroupSource(Protocol):
    def read(self, *, now: datetime) -> ModelGroupRead: ...


class ProtocolHttpGet(Protocol):
    def get(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]: ...


class ProtocolTokenMint(Protocol):
    def mint(self, *, org: str, app_id_secret: str, private_key_secret: str) -> str: ...


__all__ = ["ProtocolGroupSource", "ProtocolHttpGet", "ProtocolTokenMint"]
