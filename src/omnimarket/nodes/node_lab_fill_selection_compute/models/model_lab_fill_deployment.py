# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment repository roles for lab-fill selection."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelLabFillDeployment:
    """Repository roles supplied by the deployment, without built-in identities.

    Companion repositories carry change-control evidence that names a ticket
    without carrying its work; a companion's head moving is not a change to the
    ticket. Not-work repositories, together with companions, never say where a
    ticket's work lives. Document repositories say where a ticket's documents
    live, so a PR there ranks after code repositories.

    The deployment reads its overlay file,
    <overlay root>/node_lab_fill_selection_compute/overlay.yaml, at its own
    effect boundary and passes the parsed mapping to from_overlay. This node
    holds no repository identity.
    """

    companion_repositories: tuple[str, ...]
    not_work_repositories: tuple[str, ...]
    document_repositories: tuple[str, ...]

    @classmethod
    def from_overlay(cls, raw: object) -> ModelLabFillDeployment:
        """Validate an already-parsed overlay mapping and freeze its role lists."""
        if not isinstance(raw, dict):
            raise ValueError("overlay must be a dict mapping")
        keys = (
            "companion_repositories",
            "not_work_repositories",
            "document_repositories",
        )
        missing = set(keys) - raw.keys()
        if missing:
            raise ValueError("overlay missing keys: " + ", ".join(sorted(missing)))
        unknown = raw.keys() - set(keys)
        if unknown:
            raise ValueError(
                "overlay unknown keys: " + ", ".join(sorted(map(str, unknown)))
            )
        for key in keys:
            value = raw[key]
            if not isinstance(value, list):
                raise ValueError(f"{key} must be a list of strings")
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    raise ValueError(f"{key}[{index}] must be a string")
        return cls(
            companion_repositories=tuple(raw["companion_repositories"]),
            not_work_repositories=tuple(raw["not_work_repositories"]),
            document_repositories=tuple(raw["document_repositories"]),
        )

    def __post_init__(self) -> None:
        for key in (
            "companion_repositories",
            "not_work_repositories",
            "document_repositories",
        ):
            value = getattr(self, key)
            if not isinstance(value, tuple) or any(
                not isinstance(item, str) for item in value
            ):
                raise ValueError(f"{key} must be a tuple of strings")
