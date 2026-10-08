# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment census location for host hygiene."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelLabDiskHygieneDeployment:
    """An absolute census path, or one relative to the request's workspace."""

    census_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.census_path, str) or not self.census_path.strip():
            raise ValueError("census_path must be a nonempty string")
