# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Heuristic quality rules a prompt's own layout waives."""

from __future__ import annotations

import re

__all__ = ["prompt_waived_rules"]


def prompt_waived_rules(
    contract: dict[str, object] | None,
    heuristic: tuple[str, ...],
    prompt: str,
) -> tuple[str, ...]:
    """Heuristic rules whose own declaration says this prompt's layout waives them.

    OMN-19432. A rule declares ``waived_when_prompt_matches`` (and, OMN-20844,
    ``waived_unless_prompt_matches``: the input it needs) under
    ``quality_rules`` in the SAME contract this resolver reads the DoD from, so
    what the model is told and what the gate enforces stay one list. Returns the
    waived names in band order. A rule the contract does not declare, or a
    malformed declaration, waives nothing: an unreadable waiver must never
    silently drop a veto (the fail-closed default of ``resolve_quality_rule``).
    """
    if not isinstance(contract, dict):
        return ()
    rules = contract.get("quality_rules")
    if not isinstance(rules, dict):
        return ()
    waived: list[str] = []
    for name in heuristic:
        rule = rules.get(name)
        if not isinstance(rule, dict):
            continue
        patterns = rule.get("waived_when_prompt_matches")
        if isinstance(patterns, list) and any(
            isinstance(pattern, str) and re.search(pattern, prompt, re.IGNORECASE)
            for pattern in patterns
        ):
            waived.append(name)
            continue
        # OMN-20844: the input the rule judges. A rule that declares what its
        # input looks like applies only to a prompt carrying some; a malformed
        # declaration (no readable pattern) leaves the rule in force.
        needed = rule.get("waived_unless_prompt_matches")
        if not isinstance(needed, list):
            continue
        input_patterns = [pattern for pattern in needed if isinstance(pattern, str)]
        if input_patterns and not any(
            re.search(pattern, prompt, re.IGNORECASE | re.MULTILINE)
            for pattern in input_patterns
        ):
            waived.append(name)
    return tuple(waived)
