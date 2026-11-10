# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``launch_validity`` overlay: mode, transport and limits over the contract's defaults.

The contract's ``config.launch_validity`` carries every default. A deployment overrides
any of them in ``<root>/launch_validity/overlay.yaml``; with no overlay the contract
stands (report_only, in_memory). The flip to ``refuse`` is the overlay value ``mode:
refuse`` and refuses to load without ``refuse_citation``, the citation of the replay
count that moved it.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)

from .models.model_launch_validity import (
    EnumLaunchValidityMode,
    EnumLaunchValidityTransport,
    ModelLaunchValidityPolicy,
    ModelLaunchValidityTopics,
)

OVERLAY_ID = "launch_validity"
_PACKAGE = "omnimarket.nodes.node_launch_validity_compute"


class ModelLaunchValidityOverlay(BaseModel):
    """``<root>/launch_validity/overlay.yaml``; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EnumLaunchValidityMode | None = None
    transport: EnumLaunchValidityTransport | None = None
    max_read_age_seconds: int | None = None
    refuse_citation: str | None = None


def load_contract() -> dict[str, Any]:
    """The packaged contract as a mapping."""
    raw = yaml.safe_load(files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8"))
    if not isinstance(raw, dict):
        raise NodeOverlayError(
            "node_launch_validity_compute contract.yaml is not a mapping"
        )
    return raw


def load_launch_validity_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelLaunchValidityPolicy:
    """The contract's defaults with the overlay laid over them; refuses naming the broken key."""
    fields: dict[str, Any] = dict(load_contract()["config"][OVERLAY_ID])
    fields.pop("replay_window_days", None)
    if find_node_overlay(OVERLAY_ID, environ) is not None:
        overlay = load_structured_overlay(
            OVERLAY_ID, ModelLaunchValidityOverlay, environ
        )
        if (
            overlay.mode is EnumLaunchValidityMode.REFUSE
            and not (overlay.refuse_citation or "").strip()
        ):
            raise NodeOverlayError(
                f"{OVERLAY_ID}.refuse_citation is missing: mode refuse needs the "
                f"citation of the replay count that moved it"
            )
        for key in ("mode", "transport", "max_read_age_seconds"):
            value = getattr(overlay, key)
            if value is not None:
                fields[key] = value
    return ModelLaunchValidityPolicy.model_validate(fields)


def replay_window_days() -> int:
    """The window of recorded launches the replay covers, from the contract."""
    return int(load_contract()["config"][OVERLAY_ID]["replay_window_days"])


def load_topics() -> ModelLaunchValidityTopics:
    """The command topic and the two published topics, as the contract declares them."""
    bus = load_contract()["event_bus"]
    decided, would_refuse = bus["publish_topics"]
    return ModelLaunchValidityTopics(
        command=bus["subscribe_topics"][0], decided=decided, would_refuse=would_refuse
    )
