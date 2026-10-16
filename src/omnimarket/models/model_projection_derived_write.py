# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable inputs of an in-process transactional projection derivation."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProjectionDerivedWrite:
    """Storage metadata and a domain callback, never a serialized event."""

    table: str
    conflict_columns: tuple[str, ...]
    input_columns: tuple[str, ...]
    derived_columns: tuple[str, ...]
    returning: tuple[str, ...]
    derive: Callable[[Mapping[str, object]], Mapping[str, object]]

    @property
    def readback_columns(self) -> tuple[str, ...]:
        """Read inputs and requested output on the same transaction."""
        return tuple(
            dict.fromkeys(
                (*self.returning, *self.conflict_columns, *self.input_columns)
            )
        )
