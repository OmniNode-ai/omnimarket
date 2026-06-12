# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression test: no top-level runtime_profiles shadowing descriptor.runtime_profiles.

profile_ownership.py reads top-level ``runtime_profiles`` first; the fallback
to ``descriptor.runtime_profiles`` only fires when the top-level key is absent.
A contract that declares *both* (e.g. top-level ``main`` + descriptor ``effects``)
will be wired into ``main`` with no dispatcher, silently routing every command to
the DLQ.  This test is the instance guard; OMN-12957 is the systemic gate.

Root cause and diagnosis: docs/evidence/2026-06-12-weekend-pass/p2-pass/DEL-dispatcher/DIAGNOSIS.md
Ticket: OMN-13104
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

NODES_ROOT = Path(__file__).resolve().parents[2] / "src/omnimarket/nodes"


def _load_contract(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict)
    return raw


_ALWAYS_ON_RUNTIME_PROFILES = frozenset({"main", "effects", "workers"})


def _has_conflicting_runtime_profiles(contract: dict[str, Any]) -> bool:
    """Return True when top-level ``runtime_profiles`` shadows descriptor's value in a
    way that would mis-wire the node.

    A contract is conflicting when ALL of:
    - it has a non-None top-level ``runtime_profiles`` list, AND
    - the ``descriptor`` block also declares a ``runtime_profiles`` list, AND
    - the top-level value is an always-on profile (main/effects/workers) but the
      descriptor declares a DIFFERENT always-on profile.

    Rationale for the always-on restriction: some nodes intentionally use a
    specialized top-level profile (e.g. ``manual_pr_review``, ``memory``,
    ``intelligence``, ``canary``) that overrides the descriptor's ``effects``
    declaration.  Those dual declarations are intentional — the specialized
    runtime owns the subscription and the descriptor documents the "base" profile
    class.  Flagging them here would generate false positives.

    The actual mis-wiring bug only occurs when the top-level declares an
    always-on runtime (``main`` being the canonical example) that has no
    dispatcher for the message type, while the descriptor correctly declares
    ``effects`` — but ``profile_ownership.py`` never reaches the descriptor
    because the top-level value takes precedence.
    """
    top_level = contract.get("runtime_profiles")
    descriptor_raw = contract.get("descriptor")
    if not isinstance(top_level, list):
        return False
    if not isinstance(descriptor_raw, dict):
        return False
    descriptor_profiles = descriptor_raw.get("runtime_profiles")
    if not isinstance(descriptor_profiles, list):
        return False
    top_level_set = {str(p).strip().lower() for p in top_level}
    descriptor_set = {str(p).strip().lower() for p in descriptor_profiles}
    # Only flag when the top-level declares always-on profile(s) AND the
    # descriptor declares different always-on profile(s).
    top_always_on = top_level_set & _ALWAYS_ON_RUNTIME_PROFILES
    descriptor_always_on = descriptor_set & _ALWAYS_ON_RUNTIME_PROFILES
    if not top_always_on or not descriptor_always_on:
        # One side is specialized-only — intentional override, not a mis-wiring.
        return False
    return top_always_on != descriptor_always_on


def test_no_contract_has_conflicting_top_level_runtime_profiles() -> None:
    """Every contract must NOT have both a top-level runtime_profiles AND a
    differing descriptor.runtime_profiles.  Such contracts are silently mis-wired:
    the top-level value wins in profile_ownership.py and the descriptor value
    is never consulted, causing the wrong runtime to own the subscription.

    Regression guard for OMN-13104 (delegate-skill DLQ root cause).
    """
    conflicting: list[str] = []

    for contract_path in sorted(NODES_ROOT.glob("node_*/contract.yaml")):
        contract = _load_contract(contract_path)
        if _has_conflicting_runtime_profiles(contract):
            conflicting.append(contract_path.parent.name)

    assert conflicting == [], (
        "Contracts must not declare top-level runtime_profiles that conflict with "
        f"descriptor.runtime_profiles (profile_ownership.py picks top-level only, "
        f"silently mis-wiring these nodes): {conflicting}"
    )
