# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployed overlay of node_ci_checkout_effect: one document, every host's serve unit (OMN-24287).

``ModelCiCheckoutOverlay`` is what one host's handler answers from. A host's own facts (where its bare
mirrors live, the address its runner containers reach the mirror daemon on) differ per host, so the
deployed document holds them as host rows and ``resolve`` fills them into the source templates, giving
the one host's ``ModelCiCheckoutOverlay``. No key has a default: an absent key refuses the load and
names it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    ModelCiCheckoutBusProfile,
    ModelCiCheckoutOverlay,
    ModelCiCheckoutSource,
)

MIRROR_ROOT_PLACEHOLDER = "{mirror_root}"
SERVE_ADDRESS_PLACEHOLDER = "{serve_address}"
HOME_PLACEHOLDER = "$HOME"


class ModelCiCheckoutHost(BaseModel):
    """One host that runs the serve unit, and the facts only that host holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user"]
    #: Where this host's bare mirrors live (the mirror daemon's base path).
    mirror_root: str = Field(min_length=1)
    #: The address the host's runner containers reach its git daemon on.
    serve_address: str = Field(min_length=1)


class ModelCiCheckoutUnit(BaseModel):
    """The systemd user unit the serve runs under on every declared host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    bus_profile: str = Field(min_length=1)
    python: str = Field(min_length=1)
    pythonpath: tuple[str, ...]
    restart_seconds: int = Field(ge=1)
    stop_timeout_seconds: int = Field(ge=1)
    log_file: str = Field(min_length=1)
    environment: dict[str, str] = Field(min_length=1)


class ModelCiCheckoutDeployment(BaseModel):
    """The deployed overlay document: shared settings, the unit and one row per host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    github_owner: str = Field(min_length=1)
    sources: tuple[ModelCiCheckoutSource, ...] = Field(min_length=1)
    sync_on_miss: bool
    sync_command: tuple[str, ...]
    sync_timeout_seconds: float = Field(gt=0)
    git_timeout_seconds: float = Field(gt=0)
    reply_timeout_seconds: float = Field(gt=0)
    default_profile: str = Field(min_length=1)
    profiles: dict[str, ModelCiCheckoutBusProfile] = Field(min_length=1)
    unit: ModelCiCheckoutUnit
    hosts: tuple[ModelCiCheckoutHost, ...] = Field(min_length=1)

    def host(self, name: str) -> ModelCiCheckoutHost:
        for row in self.hosts:
            if row.name == name:
                return row
        raise ValueError(
            f"{name}: not in the overlay's hosts {sorted(row.name for row in self.hosts)}, "
            "so the checkout is not declared for it"
        )

    def resolve(self, host_name: str, *, home: str) -> ModelCiCheckoutOverlay:
        """The one host's overlay: its mirror root and serve address filled into the templates."""
        row = self.host(host_name)
        mirror_root = row.mirror_root.replace(HOME_PLACEHOLDER, home)

        def fill(text: str) -> str:
            return (
                text.replace(MIRROR_ROOT_PLACEHOLDER, mirror_root)
                .replace(SERVE_ADDRESS_PLACEHOLDER, row.serve_address)
                .replace(HOME_PLACEHOLDER, home)
            )

        return ModelCiCheckoutOverlay(
            host_name=row.name,
            github_owner=self.github_owner,
            sources=tuple(
                ModelCiCheckoutSource(
                    name=source.name,
                    path_template=fill(source.path_template),
                    serve_url_template=fill(source.serve_url_template),
                )
                for source in self.sources
            ),
            sync_on_miss=self.sync_on_miss,
            sync_command=tuple(fill(element) for element in self.sync_command),
            sync_timeout_seconds=self.sync_timeout_seconds,
            git_timeout_seconds=self.git_timeout_seconds,
            reply_timeout_seconds=self.reply_timeout_seconds,
            default_profile=self.default_profile,
            profiles=self.profiles,
        )

    def profile(self, name: str | None) -> ModelCiCheckoutBusProfile:
        key = name or self.default_profile
        if key not in self.profiles:
            raise ValueError(
                f"no bus profile {key!r}; the overlay has {sorted(self.profiles)}"
            )
        return self.profiles[key]


__all__ = [
    "ModelCiCheckoutDeployment",
    "ModelCiCheckoutHost",
    "ModelCiCheckoutUnit",
]
