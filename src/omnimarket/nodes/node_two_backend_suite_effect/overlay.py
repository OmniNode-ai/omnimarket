# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_two_backend_suite_effect: every setting, none in code.

Found at ``<root>/node_two_backend_suite_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_two_backend_suite_effect"


class ModelInMemoryReportSettings(BaseModel):
    """Default backend: an empty effect list held in memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: Literal["in_memory"]


class ModelFileReportSettings(BaseModel):
    """Real backend: the validator's JSON, at the path the named variable holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: Literal["file"]
    path_env: str = Field(min_length=1)


ModelReportSettings = Annotated[
    ModelInMemoryReportSettings | ModelFileReportSettings, Field(discriminator="route")
]


class ModelSuitePublish(BaseModel):
    """The lane and actor of the STATUS row the piece 4 reading reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)


class ModelTwoBackendSuiteOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    report: ModelReportSettings
    #: The environment variable that holds the directory scenarios may write under.
    workdir_env: str = Field(min_length=1)
    publish: ModelSuitePublish
    #: The command that appends the STATUS row (the same route the reading writer uses).
    ledger_append: ModelLedgerAppendRoute


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelTwoBackendSuiteOverlay:
    return load_structured_overlay(NODE_NAME, ModelTwoBackendSuiteOverlay, environ)
