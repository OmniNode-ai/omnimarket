# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Filesystem probe protocol of the overlay root resolver (OMN-24032)."""

from __future__ import annotations

import typing


class ProtocolOverlayRootsProbe(typing.Protocol):
    def is_dir(self, path: str) -> bool: ...

    def read_text(self, path: str) -> str | None: ...

    def resolve(self, path: str) -> str: ...
