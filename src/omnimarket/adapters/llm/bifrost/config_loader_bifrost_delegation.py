# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Loader for bifrost_delegation.yaml delegation routing config.

Reads and validates the delegation routing config from disk.
The config maps Claude Code task classes to bifrost backend policies.

Related:
    - OMN-10637: Bifrost routing rules for delegation task classes
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
)

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = (
    Path(__file__).parent.parent.parent.parent / "configs" / "bifrost_delegation.yaml"
)


def load_bifrost_delegation_config(
    config_path: Path | None = None,
) -> ModelBifrostDelegationConfig:
    """Load and validate the bifrost delegation routing config from disk.

    Args:
        config_path: Path to the YAML config file. Defaults to the
            canonical ``src/omnimarket/configs/bifrost_delegation.yaml``.

    Returns:
        A validated ``ModelBifrostDelegationConfig`` instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the YAML cannot be parsed or fails schema validation.
    """
    resolved = config_path or _DEFAULT_CONFIG_PATH

    if not resolved.exists():
        msg = f"Bifrost delegation config not found at {resolved}"
        raise FileNotFoundError(msg)

    raw = resolved.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)

    if not isinstance(data, dict):
        msg = f"Expected YAML mapping at root, got {type(data).__name__}"
        raise ValueError(msg)

    try:
        config = ModelBifrostDelegationConfig.model_validate(data)
    except ValidationError as exc:
        msg = f"Bifrost delegation config schema validation failed: {exc}"
        raise ValueError(msg) from exc

    declared_backend_ids = {b.backend_id for b in config.backends}

    unknown_defaults = set(config.default_backends) - declared_backend_ids
    if unknown_defaults:
        msg = f"default_backends references undeclared backend(s): {sorted(unknown_defaults)}"
        raise ValueError(msg)

    for rule in config.routing_rules:
        unknown_rule_backends = set(rule.backend_ids) - declared_backend_ids
        if unknown_rule_backends:
            msg = (
                f"Rule {rule.rule_id!s} ({rule.task_class!r}) references "
                f"undeclared backend(s): {sorted(unknown_rule_backends)}"
            )
            raise ValueError(msg)

    rule_ids = [rule.rule_id for rule in config.routing_rules]
    if len(rule_ids) != len(set(rule_ids)):
        counts: dict[object, int] = {}
        for rid in rule_ids:
            counts[rid] = counts.get(rid, 0) + 1
        duplicates = [rid for rid, count in counts.items() if count > 1]
        msg = f"Duplicate rule_id(s) detected: {duplicates}"
        raise ValueError(msg)

    logger.info(
        "Loaded bifrost delegation config v%s: %d backends, %d rules",
        config.config_version,
        len(config.backends),
        len(config.routing_rules),
    )
    return config


__all__: list[str] = ["load_bifrost_delegation_config"]
