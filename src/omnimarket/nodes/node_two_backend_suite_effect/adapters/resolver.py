# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Find a factory or scenario by its dotted path."""

from __future__ import annotations

import importlib
from collections.abc import Callable


class ImportResolver:
    def resolve(self, dotted: str) -> Callable[..., object]:
        module_name, _, attribute = dotted.rpartition(".")
        if not module_name:
            raise LookupError(f"not a dotted path: {dotted!r}")
        target: object = getattr(importlib.import_module(module_name), attribute)
        if not callable(target):
            raise TypeError(f"{dotted} is not callable")
        resolved: Callable[..., object] = target
        return resolved
