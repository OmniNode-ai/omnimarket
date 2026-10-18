# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Theme of an incident or fix: the first rule whose keyword is in the row's own words wins."""

from __future__ import annotations

THEME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Delegation and model access",
        (
            "delegat",
            "glm",
            "codex",
            "claude api",
            "model",
            "token",
            "quota",
            "credential",
            "provider",
        ),
    ),
    (
        "Lab capacity and placement",
        (
            "lab-idle",
            "lab-enumerate",
            "quiet-host",
            "placement",
            "capacity",
            "runner",
            "memory",
            "disk",
            "slot",
            "lab-fill",
            "lab_fill",
        ),
    ),
    (
        "Landing, merging and CI",
        (
            "landing",
            "merge",
            "pull request",
            "ci ",
            "check",
            "queue",
            "arm",
            "release",
            "review",
            "judge",
            "pr-",
        ),
    ),
    (
        "Hooks, guards and refusals",
        ("guard", "refusal", "hook", "shim", "gate", "tool-refusal"),
    ),
    (
        "Ledger, rulings and process",
        (
            "ledger",
            "ruling",
            "process",
            "zero-flip",
            "tick",
            "watcher",
            "alarm",
            "friction",
            "adoption",
        ),
    ),
)
OTHER_THEME = "Everything else"
THEME_ORDER: tuple[str, ...] = (*(name for name, _ in THEME_RULES), OTHER_THEME)


def theme_of(*words: str) -> str:
    """Theme of the first of `words` that names one; put the most specific word first."""
    for word in words:
        haystack = word.lower()
        for name, keywords in THEME_RULES:
            if any(keyword in haystack for keyword in keywords):
                return name
    return OTHER_THEME


__all__ = ["OTHER_THEME", "THEME_ORDER", "THEME_RULES", "theme_of"]
