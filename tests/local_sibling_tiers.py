# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build a routing-tiers file in which a local rung HAS a same-tier sibling.

OMN-16833. The committed ``routing_tiers.yaml`` no longer gives any task class
two DISTINCT local backends, because the only second local endpoint in the fleet
(``local-ds-v4-flash`` at .200:8101) is stopped — every lane overlay marks it
``serving: false`` (OMN-16999), every lane renders it ``endpoint_url: null``, and
``_load_bifrost_endpoints`` drops it. Note that ``local-coder`` and
``local-heavy-reasoning`` are two backend_ids on the SAME physical endpoint, so
they were never a real retry sibling for each other either.

That leaves OMN-14402's same-tier fallback (``sibling_backend_available_in_tier``)
unexercisable against the committed config. It was previously exercised against
a rung the whole fleet skips, so those tests were green on a routing path that
did not exist — the exact defect OMN-16833 is about.

The mechanism is still worth proving, so the tests that prove it run against the
config this helper builds: the committed tiers with the parked rung RESTORED,
i.e. the world in which .200:8101 is back up. Pair it with the synthetic bifrost
contract in ``tests/unit/delegation/conftest.py``, which already gives
``local-ds-v4-flash`` a concrete endpoint.

``tests/test_routing_tiers_contract.py`` holds the other half: the moment
``local-ds-v4-flash`` becomes lane-bound for real, the tier entry must be
restored in the committed file and these helpers become unnecessary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml

from omnimarket.routing.routing_tiers_path import (
    ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
)

#: The rung OMN-16833 parked, in the shape ``routing_tiers.yaml`` declared it.
RESTORED_LOCAL_SIBLING: Final[dict[str, Any]] = {
    "id": "ds-v4-flash",
    "backend_id": "local-ds-v4-flash",
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
    """Write the committed tiers with the parked local sibling restored.

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
    if RESTORED_LOCAL_SIBLING["backend_id"] in already_declared:
        raise AssertionError(
            "the committed routing_tiers.yaml already declares "
            f"{RESTORED_LOCAL_SIBLING['backend_id']!r} in the local tier — the "
            "endpoint is back, so delete this helper and let the tests that use "
            "it run against the committed config again (OMN-16833)"
        )
    local_tier["models"].append(dict(RESTORED_LOCAL_SIBLING))

    written = destination_dir / "routing_tiers.yaml"
    written.write_text(yaml.safe_dump(committed, sort_keys=False), encoding="utf-8")
    return written
