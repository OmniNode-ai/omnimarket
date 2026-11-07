# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_lane_result_sweep_effect: every setting of the sweep, none in code.

Found at ``<root>/node_lane_result_sweep_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.error_normalizer import ModelErrorNormalizerFacts
from omnimarket.models.model_lane_result_sweep import EnumLaneResultClass
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
    ModelLedgerAppendRoute,
)

NODE_NAME = "node_lane_result_sweep_effect"


class ModelLaneResultSweepPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: The plan's piece number the reading row is for.
    piece: int = Field(ge=1)
    #: How long a class row may stand without an action row or a valid park (the plan says 3600).
    action_deadline_seconds: int = Field(ge=60)


class ModelLaneResultSweepOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_read: Annotated[
        ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
        Field(discriminator="route"),
    ]
    ledger_append: ModelLedgerAppendRoute
    publish: ModelLaneResultSweepPublish
    #: The error event's source and producer, as the normalizer's adapter table names them.
    source: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    normalizer_facts: ModelErrorNormalizerFacts
    #: Class of each token of a TERMINAL's ``outcome`` cell (lower case). An unlisted token is unknown.
    outcome_classes: dict[str, EnumLaneResultClass]
    #: Class of each token of a TERMINAL's ``result`` / ``lane_result`` cell (lower case).
    result_classes: dict[str, EnumLaneResultClass]
    #: The error rule table (``error_rules`` rows with ``class_keys`` and ``action``).
    error_rule_document: dict[str, Any]

    def overlay_hash(self) -> str:
        text = json.dumps(
            {
                "deadline": self.publish.action_deadline_seconds,
                "rules": sorted(
                    str(row.get("id", ""))
                    for row in self.error_rule_document.get("error_rules", [])
                ),
                "normalizer": self.normalizer_facts.model_dump(mode="json"),
                "outcomes": {
                    k: v.value for k, v in sorted(self.outcome_classes.items())
                },
                "results": {k: v.value for k, v in sorted(self.result_classes.items())},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLaneResultSweepOverlay:
    return load_structured_overlay(NODE_NAME, ModelLaneResultSweepOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelLaneResultSweepOverlay",
    "ModelLaneResultSweepPublish",
    "load_overlay",
]
