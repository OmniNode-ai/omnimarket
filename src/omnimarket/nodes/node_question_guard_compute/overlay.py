# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``question_guard`` overlay: rule data and transport over the contract's defaults.

``config.question_guard`` in the contract carries the rule data and the default
transport (in_memory). A deployment replaces the rules or the transport in
``<root>/question_guard/overlay.yaml``; with no overlay the contract stands.
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

from .models.model_question_guard import (
    EnumQuestionGuardTransport,
    ModelQuestionGuardRule,
    ModelQuestionGuardRules,
    ModelQuestionGuardTopics,
)

OVERLAY_ID = "question_guard"
_PACKAGE = "omnimarket.nodes.node_question_guard_compute"


class ModelQuestionGuardOverlay(BaseModel):
    """``<root>/question_guard/overlay.yaml``; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: EnumQuestionGuardTransport | None = None
    rules: tuple[ModelQuestionGuardRule, ...] | None = None


def load_contract() -> dict[str, Any]:
    """The packaged contract as a mapping."""
    raw = yaml.safe_load(files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8"))
    if not isinstance(raw, dict):
        raise NodeOverlayError(
            "node_question_guard_compute contract.yaml is not a mapping"
        )
    return raw


def _overlay(environ: Mapping[str, str] | None) -> ModelQuestionGuardOverlay | None:
    if find_node_overlay(OVERLAY_ID, environ) is None:
        return None
    return load_structured_overlay(OVERLAY_ID, ModelQuestionGuardOverlay, environ)


def load_question_guard_rules(
    environ: Mapping[str, str] | None = None,
) -> ModelQuestionGuardRules:
    """The contract's rule data with the overlay's rules laid over it."""
    fields: dict[str, Any] = dict(load_contract()["config"][OVERLAY_ID])
    fields.pop("transport", None)
    overlay = _overlay(environ)
    if overlay is not None and overlay.rules is not None:
        fields["rules"] = overlay.rules
    return ModelQuestionGuardRules.model_validate(fields)


def load_transport(
    environ: Mapping[str, str] | None = None,
) -> EnumQuestionGuardTransport:
    """The bus binding: the overlay's transport, else the contract's."""
    overlay = _overlay(environ)
    if overlay is not None and overlay.transport is not None:
        return overlay.transport
    return EnumQuestionGuardTransport(
        load_contract()["config"][OVERLAY_ID]["transport"]
    )


def load_topics() -> ModelQuestionGuardTopics:
    """The command topic and the decided topic, as the contract declares them."""
    bus = load_contract()["event_bus"]
    return ModelQuestionGuardTopics(
        command=bus["subscribe_topics"][0], decided=bus["publish_topics"][0]
    )


__all__ = [
    "OVERLAY_ID",
    "ModelQuestionGuardOverlay",
    "load_contract",
    "load_question_guard_rules",
    "load_topics",
    "load_transport",
]
