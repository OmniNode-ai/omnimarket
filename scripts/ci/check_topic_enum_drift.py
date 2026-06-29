#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI gate: Topic Enum Drift Check for omnimarket (OMN-13331).

Validates that all topic string constants declared in the canonical omnimarket
topics.py registry files are referenced by at least one contract.yaml
``event_bus.publish_topics`` or ``event_bus.subscribe_topics`` declaration,
OR carry a ``# onex-topic-allow`` / ``# onex-topic-sot`` inline annotation.

Constants not backed by any contract AND without an allow annotation are
"orphaned" registry entries — they indicate the constants file has drifted
from the live contract declarations. Orphaned entries fail this check (exit 1).

Canonical registry files scanned:
  src/omnimarket/events/topics.py
  src/omnimarket/adapters/codex/topics.py
  src/omnimarket/logging/topics.py

Constants that are prefix strings (ending in ".") are skipped — those are
namespace prefixes, not versioned topic identifiers.

Usage::

    # Run from the omnimarket repo root
    uv run python scripts/ci/check_topic_enum_drift.py

Exit codes:
  0  All constants are backed by contracts or have allow annotations (clean).
  1  One or more orphaned constants detected (drift: topics.py → contracts).
  2  Invocation error (e.g., run from wrong directory).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Canonical registry files that declare topic string constants.
_REGISTRY_FILES: tuple[Path, ...] = (
    _REPO_ROOT / "src" / "omnimarket" / "events" / "topics.py",
    _REPO_ROOT / "src" / "omnimarket" / "adapters" / "codex" / "topics.py",
    _REPO_ROOT / "src" / "omnimarket" / "logging" / "topics.py",
)

# Root directory for node contracts.
_NODES_ROOT = _REPO_ROOT / "src" / "omnimarket" / "nodes"

# Contract keys that declare event bus topics.
_EVENT_BUS_KEYS = ("subscribe_topics", "publish_topics")

# Inline annotation markers that indicate a constant is intentionally
# not backed by an omnimarket contract (e.g., cross-repo or legacy).
_ALLOW_MARKERS = ("# onex-topic-allow", "# onex-topic-sot")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_constants(registry_file: Path) -> dict[str, tuple[str, bool]]:
    """Parse a topics.py file and return ``{name: (value, has_allow_marker)}``.

    Only constants whose values contain "onex." and do NOT end with "."
    (namespace prefixes) are collected.  The ``has_allow_marker`` flag is True
    when the source line carries an inline ``# onex-topic-allow`` or
    ``# onex-topic-sot`` annotation.
    """
    if not registry_file.exists():
        return {}

    source = registry_file.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=str(registry_file))

    result: dict[str, tuple[str, bool]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if not isinstance(node.value, ast.Constant):
                    continue
                value = node.value.value
                if not isinstance(value, str):
                    continue
                if "onex." not in value:
                    continue
                if value.endswith("."):
                    # Namespace prefix, not a versioned topic — skip.
                    continue
                # Check the source line for allow marker (lineno is 1-indexed).
                lineno = node.end_lineno or node.lineno  # end of assignment
                line_text = lines[lineno - 1] if lineno <= len(lines) else ""
                has_allow = any(m in line_text for m in _ALLOW_MARKERS)
                result[target.id] = (value, has_allow)
    return result


def _collect_contract_topics(nodes_root: Path) -> frozenset[str]:
    """Return all topic strings declared in all contract.yaml files under *nodes_root*."""
    topics: set[str] = set()
    for contract_path in nodes_root.rglob("contract.yaml"):
        try:
            raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        event_bus = raw.get("event_bus")
        if not isinstance(event_bus, dict):
            continue
        for key in _EVENT_BUS_KEYS:
            for topic in event_bus.get(key, []):
                if isinstance(topic, str):
                    topics.add(topic)
    return frozenset(topics)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the topic enum drift check.

    Returns:
        0 if clean, 1 if orphaned constants found, 2 on invocation error.
    """
    if not _NODES_ROOT.is_dir():
        print(
            f"ERROR: nodes directory not found: {_NODES_ROOT}\n"
            "Run this script from the omnimarket repo root.",
            file=sys.stderr,
        )
        return 2

    # Collect all constants across registry files.
    all_constants: dict[str, tuple[str, bool, Path]] = {}
    for registry_file in _REGISTRY_FILES:
        for name, (value, has_allow) in _extract_constants(registry_file).items():
            all_constants[name] = (value, has_allow, registry_file)

    if not all_constants:
        print("WARNING: No topic constants found in registry files.")
        return 0

    # Collect all contract-declared topics.
    contract_topics = _collect_contract_topics(_NODES_ROOT)
    print(
        f"Scanned {len(all_constants)} topic constants across "
        f"{len(_REGISTRY_FILES)} registry files."
    )
    print(f"Found {len(contract_topics)} unique topics across all contracts.")
    print()

    # Find orphaned constants: in registry but not in any contract + no allow.
    orphaned: list[tuple[str, str, Path]] = []
    allowed_not_in_contract: list[tuple[str, str]] = []
    in_contract: list[tuple[str, str]] = []

    for name, (value, has_allow, registry_file) in all_constants.items():
        if value in contract_topics:
            in_contract.append((name, value))
        elif has_allow:
            allowed_not_in_contract.append((name, value))
        else:
            orphaned.append((name, value, registry_file))

    # Report results.
    print(f"Constants backed by contracts:            {len(in_contract)}")
    print(f"Constants with allow annotation (exempt): {len(allowed_not_in_contract)}")
    print(f"Orphaned constants (drift violations):    {len(orphaned)}")
    print()

    if allowed_not_in_contract:
        print("Allowed (exempt via # onex-topic-allow / # onex-topic-sot):")
        for name, value in allowed_not_in_contract:
            print(f"  {name} = {value!r}")
        print()

    if orphaned:
        print("=" * 72)
        print("TOPIC ENUM DRIFT DETECTED — orphaned constants (not in any contract):")
        print("=" * 72)
        for name, value, registry_file in orphaned:
            rel = registry_file.relative_to(_REPO_ROOT)
            print(f"  {rel}: {name} = {value!r}")
        print()
        print(
            "Fix: either declare the topic in a contract.yaml "
            "event_bus.publish_topics or event_bus.subscribe_topics block, "
            "OR add a '# onex-topic-allow: <reason>' annotation to the constant."
        )
        return 1

    print("Topic Enum Drift Check PASSED — all constants backed by contracts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
