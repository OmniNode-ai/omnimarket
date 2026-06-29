#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI gate: Topic Drift Check for omnimarket (OMN-13331).

Validates that topics declared in omnimarket contracts satisfy two properties:

1. **Namespace integrity** — every topic string matches the canonical ONEX
   format ``onex.{kind}.{producer}.{event-slug}.v{n}`` and the producer segment
   belongs to the known-producer allowlist.  Topics failing this check are
   format violations and exit with code 1.  A per-repo baseline suppresses
   pre-existing violations so only NEW drift blocks PRs.

2. **Cross-repo registry coverage** — topics from external namespaces
   (producer != ``omnimarket``) that are subscribed/published by omnimarket
   contracts should ideally have a corresponding constant in the canonical
   ``events/topics.py`` registry.  Missing registry entries are reported as
   WARNINGS only (exit 0); this mirrors the ``--warn-only`` behaviour of the
   equivalent check in omnibase_infra (OMN-5248).

Run from the omnimarket repo root::

    uv run python scripts/ci/check_topic_drift.py

Exit codes:
  0  No format violations (cross-repo registry gaps are warnings only).
  1  One or more NEW format or unknown-producer violations detected.
  2  Invocation error.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Constants — keep in sync with omnibase_infra's lint_topic_names.py
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_NODES_ROOT = _REPO_ROOT / "src" / "omnimarket" / "nodes"

# Baseline file: pre-existing violations that are suppressed during enforcement
# roll-out.  New violations (not in baseline) cause exit 1.
_BASELINE_FILE = _REPO_ROOT / "scripts" / "validation" / "topic_naming_baseline.txt"

# Registry files for cross-repo coverage check (warn-only).
_REGISTRY_FILES: tuple[Path, ...] = (
    _REPO_ROOT / "src" / "omnimarket" / "events" / "topics.py",
    _REPO_ROOT / "src" / "omnimarket" / "adapters" / "codex" / "topics.py",
    _REPO_ROOT / "src" / "omnimarket" / "logging" / "topics.py",
)

# Contract keys that declare event bus topics.
_EVENT_BUS_KEYS = ("subscribe_topics", "publish_topics")

# Valid topic kinds (evt = event, cmd = command, dlq = dead-letter, etc.).
_VALID_KINDS: frozenset[str] = frozenset({"evt", "cmd", "dlq", "intent", "snapshot"})

# Known producer segments — matches the allowlist in lint_topic_names.py.
_KNOWN_PRODUCERS: frozenset[str] = frozenset(
    {
        "omnimarket",
        "omnibase-infra",
        "omniclaude",
        "omniintelligence",
        "omnimemory",
        "omninode",
        "omnibase-compat",
        "github",
        "platform",
        "ui",
        "deploy",
        "savings",
        "model-router",
        "baselines",
        "build-loop",
        "projection",
    }
)

# Regex: full ONEX topic string.
_TOPIC_PATTERN = re.compile(
    r"^onex\."
    r"(?P<kind>[a-z]+)\."
    r"(?P<producer>[a-z0-9-]+)\."
    r"(?P<event>[a-z0-9._-]+)\."
    r"(?P<version>v[1-9]\d*)$"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_baseline(baseline_file: Path) -> frozenset[str]:
    """Load pre-existing violation topics from *baseline_file* (one per line)."""
    if not baseline_file.exists():
        return frozenset()
    lines = baseline_file.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip() for line in lines if line.strip() and not line.startswith("#")
    )


def _collect_contract_topics(nodes_root: Path) -> dict[str, list[str]]:
    """Return ``{topic: [contract_path, ...]}`` for all contract-declared topics."""
    result: dict[str, list[str]] = {}
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
        rel = str(contract_path.relative_to(nodes_root.parent.parent.parent))
        for key in _EVENT_BUS_KEYS:
            for topic in event_bus.get(key, []):
                if isinstance(topic, str):
                    result.setdefault(topic, []).append(rel)
    return result


def _collect_registry_topics(registry_files: tuple[Path, ...]) -> frozenset[str]:
    """Return all topic string constants from canonical registry files."""
    topics: set[str] = set()
    for registry_file in registry_files:
        if not registry_file.exists():
            continue
        source = registry_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(
                        node.value, ast.Constant
                    ):
                        value = node.value.value
                        if (
                            isinstance(value, str)
                            and "onex." in value
                            and not value.endswith(".")
                        ):
                            topics.add(value)
    return frozenset(topics)


def _validate_topic(topic: str) -> list[str]:
    """Return a list of format violation strings for *topic*, or empty list if valid."""
    violations: list[str] = []
    m = _TOPIC_PATTERN.match(topic)
    if not m:
        violations.append(
            "does not match onex.{kind}.{producer}.{event}.v{n} format"
        )
        return violations  # No point checking sub-parts if overall format is wrong.
    kind = m.group("kind")
    producer = m.group("producer")
    if kind not in _VALID_KINDS:
        violations.append(
            f"invalid kind {kind!r} (expected one of: {sorted(_VALID_KINDS)})"
        )
    if producer not in _KNOWN_PRODUCERS:
        violations.append(
            f"unknown producer segment {producer!r} — "
            f"add to _KNOWN_PRODUCERS in check_topic_drift.py if intentional"
        )
    return violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the topic drift check.

    Returns:
        0  No format violations (cross-repo registry gaps are warnings only).
        1  One or more NEW format or unknown-producer violations.
        2  Invocation error.
    """
    if not _NODES_ROOT.is_dir():
        print(
            f"ERROR: nodes directory not found: {_NODES_ROOT}\n"
            "Run this script from the omnimarket repo root.",
            file=sys.stderr,
        )
        return 2

    baseline = _load_baseline(_BASELINE_FILE)
    contract_topics = _collect_contract_topics(_NODES_ROOT)
    registry_topics = _collect_registry_topics(_REGISTRY_FILES)

    print(
        f"Scanned {len(contract_topics)} unique topics across "
        f"{len(list(_NODES_ROOT.rglob('contract.yaml')))} contracts."
    )
    print(f"Baseline suppresses {len(baseline)} pre-existing violations.")
    print()

    # --- Part 1: Format / producer namespace validation (exit 1 on new violations). ---
    format_violations: list[tuple[str, list[str]]] = []
    new_violations: list[tuple[str, list[str]]] = []

    for topic in sorted(contract_topics):
        issues = _validate_topic(topic)
        if issues:
            format_violations.append((topic, issues))
            if topic not in baseline:
                new_violations.append((topic, issues))

    # --- Part 2: Cross-repo registry coverage (warn-only). ---
    unregistered_cross_repo: list[str] = []
    for topic in sorted(contract_topics):
        # Only check cross-repo (non-omnimarket) topics.
        m = _TOPIC_PATTERN.match(topic)
        if not m:
            continue
        if m.group("producer") == "omnimarket":
            continue
        if topic not in registry_topics:
            unregistered_cross_repo.append(topic)

    # --- Report. ---
    if format_violations:
        suppressed = len(format_violations) - len(new_violations)
        print(
            f"Format/producer violations: {len(format_violations)} total, {suppressed} suppressed by baseline."
        )
        if new_violations:
            print()
            print("=" * 72)
            print("NEW TOPIC FORMAT VIOLATIONS (not in baseline):")
            print("=" * 72)
            for topic, issues in new_violations:
                print(f"  {topic!r}")
                for issue in issues:
                    print(f"    ✗ {issue}")
            print()
            print(
                "Fix: correct the topic string in the contract.yaml, OR add it to\n"
                "scripts/validation/topic_naming_baseline.txt if it is a pre-existing\n"
                "violation being tracked separately."
            )
    else:
        print("All contract-declared topics pass format validation.")

    print()

    if unregistered_cross_repo:
        print(
            f"Cross-repo registry coverage (warn-only): "
            f"{len(unregistered_cross_repo)} external topics not in events/topics.py."
        )
        print(
            "  NOTE: These are WARNINGS only. Cross-repo topics resolved via\n"
            "  contract_topics.py at runtime do not require registry constants.\n"
            "  Consider adding registry constants for high-traffic cross-repo topics."
        )
        # Print first 10 as a sample.
        for topic in unregistered_cross_repo[:10]:
            print(f"  (warn) {topic!r} not in events/topics.py registry")
        if len(unregistered_cross_repo) > 10:
            print(f"  ... and {len(unregistered_cross_repo) - 10} more.")
        print()
    else:
        print("All cross-repo topics have registry constants in events/topics.py.")

    if new_violations:
        print("Topic Drift Check FAILED — new format violations detected.")
        return 1

    print("Topic Drift Check PASSED — no new violations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
