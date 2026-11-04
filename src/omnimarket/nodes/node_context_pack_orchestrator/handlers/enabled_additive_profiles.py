# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The server-side list of enabled additive profiles (OMN-22016, plan task TC.11).

``enabled_additive_profiles`` in the orchestrator contract names the additive profiles whose
Claude Code rows are answered with a package. It ships empty: every additive call is answered
``none`` until a data PR names a profile. ``per_prompt`` is refused here until TC.13 adds it to
``PROMOTABLE_ADDITIVE_PROFILES`` after its measured p95 is within its 500 ms budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.models.claude_code_binding import ADDITIVE_BUDGET_CAPS_MS

#: Additive profiles a data PR may enable now; TC.13 promotes ``per_prompt`` into this set.
PROMOTABLE_ADDITIVE_PROFILES: frozenset[str] = frozenset(
    {"session_start", "agent_spawn"}
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_enabled_additive_profiles(
    contract_path: Path = CONTRACT_PATH,
) -> tuple[str, ...]:
    """The contract's list, each name an unrepeated promotable additive profile; absent is empty."""
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(contract, Mapping):
        raise ValueError("The orchestrator contract must be a mapping")
    names = contract.get("enabled_additive_profiles", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("enabled_additive_profiles must be a list of profile names")
    if len(set(names)) != len(names):
        raise ValueError("enabled_additive_profiles repeats a profile")
    for name in names:
        if name not in ADDITIVE_BUDGET_CAPS_MS:
            raise ValueError(f"{name!r} is not an additive profile")
        if name not in PROMOTABLE_ADDITIVE_PROFILES:
            raise ValueError(f"{name!r} cannot be enabled before its promotion (TC.13)")
    return tuple(names)
