# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail closed on absent or malformed policy, binding or sources."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from omnimarket.nodes.node_capture_silence_watch_effect.models import (
    CaptureWatchPolicyError,
    ModelCaptureSource,
    ModelCaptureWatchPolicy,
    ModelReadsSource,
)


def load_capture_watch_config(
    contract_path: Path,
) -> tuple[ModelCaptureWatchPolicy, ModelReadsSource, tuple[ModelCaptureSource, ...]]:
    """Read all required blocks once; a configuration error has no fallback."""
    try:
        raw: Any = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise CaptureWatchPolicyError("capture watch contract must be a mapping")
        policy = ModelCaptureWatchPolicy.model_validate(raw["capture_watch_policy"])
        binding = ModelReadsSource.model_validate(raw["reads_source"])
        sources = TypeAdapter(tuple[ModelCaptureSource, ...]).validate_python(
            raw["capture_sources"]
        )
        if not sources or len({source.source_id for source in sources}) != len(sources):
            raise CaptureWatchPolicyError(
                "capture_sources must be nonempty with unique source_id values"
            )
        return policy, binding, sources
    except (OSError, yaml.YAMLError, KeyError, ValidationError) as exc:
        raise CaptureWatchPolicyError(
            f"invalid capture watch contract at {contract_path}: {exc}"
        ) from exc
