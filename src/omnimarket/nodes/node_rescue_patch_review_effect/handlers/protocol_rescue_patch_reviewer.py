# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dependency boundary for a scan's pure patch reviewer."""

from typing import Protocol

from ..models import ModelRescuePatchDecision


class ProtocolRescuePatchReviewer(Protocol):
    """Review patch text and two git facts without owning git or filesystem I/O."""

    def __call__(
        self, *, patch_text: str, applies_to_base: bool, already_on_base: bool
    ) -> ModelRescuePatchDecision: ...
