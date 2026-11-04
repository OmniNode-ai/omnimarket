# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wilson interval for the labelled structural false-coverage sample."""

from __future__ import annotations

import math
from pathlib import Path

from omnimarket.models.operator_unanswered import (
    ModelStructuralMissRate,
    ModelStructuralSample,
)
from omnimarket.nodes.contract_topics import contract_section


class HandlerStructuralMissRate:
    def __init__(self, *, contract_path: Path | None = None) -> None:
        self._policy = contract_section(
            contract_path or Path(__file__).parents[1] / "contract.yaml",
            "measurement_policy",
        )

    def handle(self, request: ModelStructuralSample) -> ModelStructuralMissRate:
        policy = self._policy
        if not isinstance(policy, dict):
            raise ValueError("measurement_policy must be a mapping")
        n = len(request.labels)
        if n < int(policy["minimum_sample_size"]):
            raise ValueError(
                f"labelled sample has {n} messages; requires at least {policy['minimum_sample_size']}"
            )
        if len({label.message_id for label in request.labels}) != n:
            raise ValueError("labelled sample has duplicate message ids")
        misses = sum(
            not label.assistant_text_addresses_message for label in request.labels
        )
        rate = misses / n
        z = float(policy["wilson_z"])
        denom = 1 + z * z / n
        centre = (rate + z * z / (2 * n)) / denom
        half = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / denom
        return ModelStructuralMissRate(
            sample_id=request.sample_id,
            sample_size=n,
            miss_count=misses,
            miss_rate=rate,
            interval_low=max(0.0, centre - half),
            interval_high=min(1.0, centre + half),
            confidence_level=float(policy["confidence_level"]),
        )
