# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``agent_review_gate_accept`` overlay: deployment facts over the contract's defaults.

``config.agent_review_gate_accept`` in the contract carries the receipt fence, the
blocking severities, the cause patterns and the default transport. The publisher's app
identity and the verifier's endpoint are deployment facts: they come only from
``<root>/agent_review_gate_accept/overlay.yaml``, and a missing overlay refuses (named)
rather than guessing either one.
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

from .models.model_gate_accept import (
    EnumGateAcceptTransport,
    ModelGateAcceptPolicy,
    ModelGateAcceptTopics,
)

OVERLAY_ID = "agent_review_gate_accept"
_PACKAGE = "omnimarket.nodes.node_agent_review_gate_accept_effect"


class ModelGateAcceptOverlay(BaseModel):
    """``<root>/agent_review_gate_accept/overlay.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    publisher_login: str
    verifier_endpoint: str
    transport: EnumGateAcceptTransport | None = None


def load_contract() -> dict[str, Any]:
    """The packaged contract as a mapping."""
    raw = yaml.safe_load(files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8"))
    if not isinstance(raw, dict):
        raise NodeOverlayError(
            "node_agent_review_gate_accept_effect contract.yaml is not a mapping"
        )
    return raw


def _overlay(environ: Mapping[str, str] | None) -> ModelGateAcceptOverlay:
    return load_structured_overlay(OVERLAY_ID, ModelGateAcceptOverlay, environ)


def load_gate_accept_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelGateAcceptPolicy:
    """The contract's data with the overlay's publisher identity and verifier endpoint."""
    fields: dict[str, Any] = dict(load_contract()["config"][OVERLAY_ID])
    fields.pop("transport", None)
    overlay = _overlay(environ)
    fields["publisher_login"] = overlay.publisher_login
    fields["verifier_endpoint"] = overlay.verifier_endpoint
    return ModelGateAcceptPolicy.model_validate(fields)


def load_transport(
    environ: Mapping[str, str] | None = None,
) -> EnumGateAcceptTransport:
    """The bus binding: the overlay's transport, else the contract's."""
    if find_node_overlay(OVERLAY_ID, environ) is not None:
        overlay = _overlay(environ)
        if overlay.transport is not None:
            return overlay.transport
    return EnumGateAcceptTransport(load_contract()["config"][OVERLAY_ID]["transport"])


def load_topics() -> ModelGateAcceptTopics:
    """The command topic and the decided topic, as the contract declares them."""
    bus = load_contract()["event_bus"]
    return ModelGateAcceptTopics(
        command=bus["subscribe_topics"][0], decided=bus["publish_topics"][0]
    )


__all__ = [
    "OVERLAY_ID",
    "ModelGateAcceptOverlay",
    "load_contract",
    "load_gate_accept_policy",
    "load_topics",
    "load_transport",
]
