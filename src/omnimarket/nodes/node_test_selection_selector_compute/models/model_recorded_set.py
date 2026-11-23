# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded set a stage comparison runs over: a replay input and the recorded full-suite runs, each by digest."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_comparison import (
    ModelRecordedFullRun,
)

_HEX64 = r"^[0-9a-f]{64}$"


class ModelRecordedSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    replay_input: ModelReplayRequest
    #: SHA-256 of the replay input file's bytes.
    replay_input_sha256: str = Field(pattern=_HEX64)
    runs: tuple[ModelRecordedFullRun, ...]
    #: SHA-256 of the recorded runs file's bytes.
    observed_input_sha256: str = Field(pattern=_HEX64)
