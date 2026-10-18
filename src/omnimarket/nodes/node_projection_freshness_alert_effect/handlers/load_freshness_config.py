# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail closed on absent or malformed policy, source or checks."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    FreshnessPolicyError,
    ModelFreshnessCheck,
    ModelFreshnessPolicy,
    ModelReadsSource,
)


def load_freshness_config(
    contract_path: Path,
) -> tuple[ModelFreshnessPolicy, ModelReadsSource, tuple[ModelFreshnessCheck, ...]]:
    """Read all required blocks once; a configuration error has no fallback."""
    try:
        raw: Any = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise FreshnessPolicyError("freshness contract must be a mapping")
        policy = ModelFreshnessPolicy.model_validate(raw["freshness_policy"])
        source = ModelReadsSource.model_validate(raw["reads_source"])
        checks = TypeAdapter(tuple[ModelFreshnessCheck, ...]).validate_python(
            raw["freshness_checks"]
        )
        if not checks or len({check.check_id for check in checks}) != len(checks):
            raise FreshnessPolicyError(
                "freshness_checks must be nonempty with unique check_id values"
            )
        return policy, source, checks
    except (OSError, yaml.YAMLError, KeyError, ValidationError) as exc:
        raise FreshnessPolicyError(
            f"invalid freshness contract at {contract_path}: {exc}"
        ) from exc
