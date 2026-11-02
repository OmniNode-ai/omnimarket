# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``standup_retrospective`` overlay: weekday, transport, roles and wording over the contract's defaults.

The contract's ``config.standup_retrospective`` carries every default. A deployment overrides
any top-level key in ``<root>/standup_retrospective/overlay.yaml``; with no overlay the
contract stands. The handler holds none of these values.
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

from .models.model_standup_retrospective import (
    EnumStandupRetrospectiveTransport,
    ModelStandupRetrospectiveLabels,
    ModelStandupRetrospectiveMessages,
    ModelStandupRetrospectivePolicy,
    ModelStandupRetrospectiveTopics,
)

OVERLAY_ID = "standup_retrospective"
_PACKAGE = "omnimarket.nodes.node_standup_retrospective_compute"


class ModelStandupRetrospectiveOverlay(BaseModel):
    """``<root>/standup_retrospective/overlay.yaml``; every key is optional and replaces the contract's value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: EnumStandupRetrospectiveTransport | None = None
    retro_weekday: int | None = None
    role_vocabulary: tuple[str, ...] | None = None
    draft_state: str | None = None
    draft_title_max_chars: int | None = None
    credit_template: str | None = None
    draft_description_template: str | None = None
    section_heading: str | None = None
    drafts_heading: str | None = None
    draft_line: str | None = None


_MESSAGE_KEYS = frozenset(ModelStandupRetrospectiveMessages.model_fields)
_LABEL_KEYS = frozenset(ModelStandupRetrospectiveLabels.model_fields)


def load_contract() -> dict[str, Any]:
    """The packaged contract as a mapping."""
    raw = yaml.safe_load(files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8"))
    if not isinstance(raw, dict):
        raise NodeOverlayError(
            "node_standup_retrospective_compute contract.yaml is not a mapping"
        )
    return raw


def load_standup_retrospective_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelStandupRetrospectivePolicy:
    """The contract's defaults with the overlay laid over them; refuses naming the broken key."""
    fields: dict[str, Any] = dict(load_contract()["config"][OVERLAY_ID])
    if find_node_overlay(OVERLAY_ID, environ) is not None:
        overlay = load_structured_overlay(
            OVERLAY_ID, ModelStandupRetrospectiveOverlay, environ
        )
        for key, value in overlay.model_dump(exclude_none=True).items():
            if key in _LABEL_KEYS:
                fields["labels"] = {**fields["labels"], key: value}
            else:
                fields[key] = value
    return ModelStandupRetrospectivePolicy.model_validate(fields)


def load_topics() -> ModelStandupRetrospectiveTopics:
    """The command topic and the built topic, as the contract declares them."""
    bus = load_contract()["event_bus"]
    (built,) = bus["publish_topics"]
    (command,) = bus["subscribe_topics"]
    return ModelStandupRetrospectiveTopics(command=command, built=built)
