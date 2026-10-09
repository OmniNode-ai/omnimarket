# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build a synthetic same-tier sibling for fallback mechanism tests.

The packaged local tier has one physical chat endpoint. Tests that exercise a
transport retry need a distinct fixture backend on an RFC 6761 host; this
helper adds that synthetic rung without changing the product contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml

from omnimarket.routing.routing_tiers_path import (
    ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
)

#: Synthetic sibling with the same capabilities as the reasoning rung.
FIXTURE_LOCAL_SIBLING: Final[dict[str, Any]] = {
    "id": "fixture-model-a",
    "backend_id": "local-fixture-sibling",
    "max_context_tokens": 65536,
    "use_for": [
        "research",
        "reasoning",
        "complex_reasoning",
        "code_generation",
        "escalation",
    ],
    "fast_path_threshold_tokens": 8192,
}


def write_routing_tiers_with_local_sibling(destination_dir: Path) -> Path:
    """Write the committed tiers with a synthetic local sibling.

    Args:
        destination_dir: directory to write ``routing_tiers.yaml`` into,
            normally a pytest ``tmp_path``.

    Returns:
        The path of the written file, for binding to
        ``DELEGATION_ROUTING_TIERS_PATH``.
    """
    committed = yaml.safe_load(
        ROUTING_TIERS_PACKAGED_DEFAULT_PATH.read_text(encoding="utf-8")
    )

    local_tier = next(tier for tier in committed["tiers"] if tier["name"] == "local")
    already_declared = {model["backend_id"] for model in local_tier["models"]}
    if FIXTURE_LOCAL_SIBLING["backend_id"] in already_declared:
        raise AssertionError(
            "the committed routing_tiers.yaml already declares "
            f"{FIXTURE_LOCAL_SIBLING['backend_id']!r} in the local tier — the "
            "fixture id collides with a product declaration"
        )
    local_tier["models"].append(dict(FIXTURE_LOCAL_SIBLING))

    written = destination_dir / "routing_tiers.yaml"
    written.write_text(yaml.safe_dump(committed, sort_keys=False), encoding="utf-8")
    return written
