# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``factory_bindings`` overlay as the parity run reads it.

Shape only. The profile ids, which profile is the baseline and which binding backs each
protocol under each profile are values the deployment owner supplies; no profile name is a
literal in this node. The caller loads the overlay once and passes it on the request.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)

OVERLAY_ID = "factory_bindings"


class ModelBindingProfile(BaseModel):
    """One profile id; exactly one profile of the overlay is marked the baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    baseline: bool = False


class ModelFactoryBindingsOverlay(BaseModel):
    """``<root>/factory_bindings/overlay.yaml``: profiles, each profile's bindings, proof bindings.

    ``bindings`` maps a profile id to a map of protocol code to binding id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profiles: tuple[ModelBindingProfile, ...]
    bindings: dict[str, dict[str, str]]
    proof_bindings: tuple[str, ...]

    def parity_profiles(
        self,
    ) -> tuple[ModelBindingProfile, tuple[ModelBindingProfile, ...]]:
        """The baseline profile and the others; refuses naming the overlay key."""
        ids = [profile.id for profile in self.profiles]
        if len(set(ids)) != len(ids):
            raise NodeOverlayError(
                f"{OVERLAY_ID}.profiles repeats a profile id; every id must be unique"
            )
        baselines = [profile for profile in self.profiles if profile.baseline]
        if len(baselines) != 1:
            raise NodeOverlayError(
                f"{OVERLAY_ID}.profiles must mark exactly one baseline profile "
                f"(found {len(baselines)}); a parity run compares against the baseline"
            )
        others = tuple(profile for profile in self.profiles if not profile.baseline)
        if not others:
            raise NodeOverlayError(
                f"{OVERLAY_ID}.profiles lists no profile besides the baseline; "
                f"a parity run needs at least one other profile"
            )
        return baselines[0], others

    def binding_id(self, profile_id: str, protocol_code: str) -> str:
        """The binding id for one protocol under one profile; refuses naming the key."""
        profile_bindings = self.bindings.get(profile_id)
        if profile_bindings is None or protocol_code not in profile_bindings:
            raise NodeOverlayError(
                f"{OVERLAY_ID}.bindings.{profile_id}.{protocol_code} is missing: "
                f"every profile of a parity run binds each protocol it depends on"
            )
        return profile_bindings[protocol_code]


def load_factory_bindings(
    environ: Mapping[str, str] | None = None,
) -> ModelFactoryBindingsOverlay:
    """Load and validate the overlay; refuses naming the missing key."""
    return load_structured_overlay(OVERLAY_ID, ModelFactoryBindingsOverlay, environ)
