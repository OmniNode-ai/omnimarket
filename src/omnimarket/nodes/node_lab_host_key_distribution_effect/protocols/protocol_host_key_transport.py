# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transport boundary for public key reads and restricted file appends."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.lab_host_key import EnumKeyFile, EnumLineState
from omnimarket.models.overlay_keys import ModelLabHostRow

from ..models.model_lab_host_key_distribution import (
    ModelHostKeyMaterial,
    ModelLabHostKeyDistributionRequest,
)


class HostTransportError(Exception):
    """A transport refusal carrying its exit status and diagnostic text."""


class ProtocolHostKeyTransport(Protocol):
    """Read only public material and ensure lines in the two allowed files."""

    def read_material(
        self, host: ModelLabHostRow, request: ModelLabHostKeyDistributionRequest
    ) -> ModelHostKeyMaterial: ...

    def ensure_line(
        self,
        host: ModelLabHostRow,
        file: EnumKeyFile,
        needle: str,
        line: str,
        *,
        apply: bool,
    ) -> EnumLineState: ...
