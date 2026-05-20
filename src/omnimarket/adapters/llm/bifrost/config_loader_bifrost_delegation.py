# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Loader for bifrost_delegation.yaml delegation routing config.

Reads and validates the delegation routing config from disk. Endpoint-bearing
local state is stored in an overlay file and deep-merged over the repo default
at load time.

Related:
    - OMN-10637: Bifrost routing rules for delegation task classes
    - OMN-10717: Default contract + endpoint overlay merge semantics
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import ValidationError

from omnimarket.adapters.llm.bifrost.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
)

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = (
    Path(__file__).parent.parent.parent.parent / "configs" / "bifrost_delegation.yaml"
)
_DEFAULT_OVERLAY_PATH = (
    Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"
)

_IDENTITY_KEYS = ("backend_id", "rule_id")


def load_bifrost_delegation_config(
    config_path: Path | None = None,
    overlay_path: Path | None = None,
) -> ModelBifrostDelegationConfig:
    """Load and validate the bifrost delegation routing config from disk.

    Args:
        config_path: Path to the YAML config file. Defaults to the
            canonical ``src/omnimarket/configs/bifrost_delegation.yaml``.
        overlay_path: Optional endpoint overlay YAML path. Defaults to
            ``~/.omninode/delegation/bifrost_overrides.yaml`` when present.

    Returns:
        A validated ``ModelBifrostDelegationConfig`` instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the YAML cannot be parsed or fails schema validation.
    """
    resolved = config_path or _DEFAULT_CONFIG_PATH
    overlay = overlay_path or _DEFAULT_OVERLAY_PATH

    if not resolved.exists():
        msg = f"Bifrost delegation config not found at {resolved}"
        raise FileNotFoundError(msg)

    data = _read_yaml_mapping(resolved)

    if overlay.exists():
        overlay_data = _read_yaml_mapping(overlay)
        data = deep_merge_bifrost_delegation_config(data, overlay_data)

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


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)

    if not isinstance(data, dict):
        msg = f"Expected YAML mapping at root for {path}, got {type(data).__name__}"
        raise ValueError(msg)

    return data


def deep_merge_bifrost_delegation_config(
    default_config: dict[str, Any],
    overlay_config: dict[str, Any],
) -> dict[str, Any]:
    """Return ``default_config`` deep-merged with ``overlay_config``.

    This function is pure compute: callers provide already-read YAML mappings,
    and no file system or environment access happens here. Lists of mappings
    keyed by ``backend_id`` or ``rule_id`` merge by identity, preserving default
    ordering and appending overlay-only entries.
    """
    return cast(dict[str, Any], _deep_merge(default_config, overlay_config))


def _deep_merge(default_value: Any, overlay_value: Any) -> Any:
    if isinstance(default_value, dict) and isinstance(overlay_value, dict):
        merged = copy.deepcopy(default_value)
        for key, value in overlay_value.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged

    if isinstance(default_value, list) and isinstance(overlay_value, list):
        return _merge_lists(default_value, overlay_value)

    return copy.deepcopy(overlay_value)


def _merge_lists(default_items: list[Any], overlay_items: list[Any]) -> list[Any]:
    identity_key = _list_identity_key(default_items, overlay_items)
    if identity_key is None:
        return copy.deepcopy(overlay_items)

    merged = copy.deepcopy(default_items)
    index_by_id = {
        item[identity_key]: index
        for index, item in enumerate(merged)
        if isinstance(item, dict) and identity_key in item
    }

    for overlay_item in overlay_items:
        if not isinstance(overlay_item, dict) or identity_key not in overlay_item:
            merged.append(copy.deepcopy(overlay_item))
            continue
        item_id = overlay_item[identity_key]
        if item_id in index_by_id:
            existing_index = index_by_id[item_id]
            merged[existing_index] = _deep_merge(merged[existing_index], overlay_item)
        else:
            index_by_id[item_id] = len(merged)
            merged.append(copy.deepcopy(overlay_item))

    return merged


def _list_identity_key(
    default_items: list[Any], overlay_items: list[Any]
) -> str | None:
    mapping_items = [
        item for item in [*default_items, *overlay_items] if isinstance(item, dict)
    ]
    if not mapping_items:
        return None

    for key in _IDENTITY_KEYS:
        if all(key in item for item in mapping_items):
            return key
    return None


__all__: list[str] = [
    "deep_merge_bifrost_delegation_config",
    "load_bifrost_delegation_config",
]
