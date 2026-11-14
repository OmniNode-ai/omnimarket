# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three topics of the release sibling-premise read effect, read from its contract and nowhere else."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


class ModelSiblingPremiseReadTopics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: str = Field(min_length=1)
    verdict: str = Field(min_length=1)
    failure: str = Field(min_length=1)


def load_topics(contract_path: Path = CONTRACT_PATH) -> ModelSiblingPremiseReadTopics:
    dispatch = yaml.safe_load(contract_path.read_text(encoding="utf-8"))[
        "runtime_dispatch"
    ]
    return ModelSiblingPremiseReadTopics(
        receipt=dispatch["receipt_topic"],
        verdict=dispatch["terminal_events"]["success"],
        failure=dispatch["terminal_events"]["failure"],
    )
