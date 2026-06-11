#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""CI guard for the FSM terminal-state invariant typed-watchdog vocabulary (OMN-12959).

The platform invariant: every workflow FSM reaches a terminal state or trips a
typed watchdog. Watchdog emissions MUST be typed (`workflow-timeout`,
`workflow-unroutable`, `workflow-stalled`) and resolve their publication topic
through the single authority in ``omnimarket.events.watchdog``
(``EnumWatchdogEventType`` / ``watchdog_topic_for``) — never via hardcoded topic
strings scattered across orchestrator handlers.

The canonical watchdog topic literals live in the omnimarket topic registry
(``src/omnimarket/events/topics.py`` — the single source of truth allowlisted
by the dependency-health and topic-literal gates). ``omnimarket.events.watchdog``
imports them and maps each ``EnumWatchdogEventType`` class 1:1 to its topic.

This guard fails (exit 1) when a canonical watchdog topic string is hardcoded
anywhere outside the canonical registry module, forcing the typed authority.

Exit codes:
    0: no violations
    1: hardcoded watchdog topic string found outside the registry

Usage:
    python scripts/ci/check_watchdog_topic_authority.py
    python scripts/ci/check_watchdog_topic_authority.py --verbose
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Canonical registry module — the ONLY place watchdog topic literals may appear.
# The literals live in the omnimarket topic registry; events/watchdog.py imports
# them and exposes the typed authority (EnumWatchdogEventType / watchdog_topic_for).
_REGISTRY_REL = "src/omnimarket/events/topics.py"

# Matches any canonical watchdog topic literal: onex.evt.<svc>.workflow-<class>.vN
_WATCHDOG_TOPIC_RE = re.compile(
    r"onex\.evt\.[a-z0-9_]+\.workflow-(timeout|unroutable|stalled)\.v\d+"
)

# Inline escape for the registry's own mirrors / intentional test fixtures.
_SKIP_TOKEN = "ONEX_WATCHDOG_TOPIC_OK"

_ALLOWLISTED_SEGMENTS = ("tests/", "fixtures/", "conftest.py", "__pycache__/")


def _repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    while candidate != candidate.parent:
        if (candidate / ".git").exists():
            return candidate
        candidate = candidate.parent
    return Path.cwd()


def scan(repo_root: Path) -> list[str]:
    """Return violation strings for hardcoded watchdog topics outside the registry."""
    violations: list[str] = []
    src_root = repo_root / "src"
    if not src_root.exists():
        return violations

    registry_path = (repo_root / _REGISTRY_REL).resolve()

    for py_file in src_root.rglob("*.py"):
        if py_file.resolve() == registry_path:
            continue
        rel = str(py_file.relative_to(repo_root)).replace("\\", "/")
        if any(seg in rel for seg in _ALLOWLISTED_SEGMENTS):
            continue

        try:
            lines = py_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        for lineno, line in enumerate(lines, start=1):
            if _SKIP_TOKEN in line:
                continue
            if _WATCHDOG_TOPIC_RE.search(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    repo_root = _repo_root()
    violations = scan(repo_root)

    if violations:
        print(
            f"[watchdog-topic-authority] {len(violations)} hardcoded watchdog "
            f"topic literal(s) outside {_REGISTRY_REL}:"
        )
        for v in sorted(violations):
            print(f"  {v}")
        print(
            "\nFix: import EnumWatchdogEventType / watchdog_topic_for from "
            "omnimarket.events.watchdog and resolve the topic from the typed class. "
            "Watchdog topics live ONLY in the canonical registry (OMN-12959)."
        )
        return 1

    if args.verbose:
        print("[watchdog-topic-authority] OK — no hardcoded watchdog topics.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
