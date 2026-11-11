# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the node's settings from ``policy.yaml`` into the frozen policy model."""

from __future__ import annotations

from pathlib import Path

import yaml

from ..models.model_test_duration_record_check import ModelTestDurationRecordPolicy


def load_policy() -> ModelTestDurationRecordPolicy:
    """Load and validate ``policy.yaml`` from the node directory."""
    policy_path = Path(__file__).resolve().parents[1] / "policy.yaml"
    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    return ModelTestDurationRecordPolicy.model_validate(raw)
