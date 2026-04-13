#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI runtime sweep — verify every onex.nodes entry point has a contract.yaml and handler.

Exits 1 if any broken entry points are detected. Designed for plain-Python CI execution
without the Claude Code harness. Implements the entry-point validation subset of
runtime_sweep (OMN-8611).
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).parent.parent.parent
    pyproject_path = repo_root / "pyproject.toml"

    with pyproject_path.open("rb") as f:
        config = tomllib.load(f)

    entry_points: dict[str, str] = (
        config.get("project", {})
        .get("entry-points", {})
        .get("onex.nodes", {})
    )

    if not entry_points:
        print("ERROR: No [project.entry-points.\"onex.nodes\"] found in pyproject.toml")
        return 1

    src_root = repo_root / "src"
    broken: list[tuple[str, str, str]] = []

    for node_name, module_path in entry_points.items():
        # Convert dotted module path to filesystem path
        node_dir = src_root / Path(*module_path.split("."))

        if not node_dir.exists():
            broken.append((node_name, module_path, "module directory missing"))
            continue

        if not (node_dir / "__init__.py").exists():
            broken.append((node_name, module_path, "__init__.py missing"))
            continue

        contract = node_dir / "contract.yaml"
        if not contract.exists():
            broken.append((node_name, module_path, "contract.yaml missing"))
            continue

        # Verify contract has a non-empty description
        content = contract.read_text()
        if "description:" not in content:
            broken.append((node_name, module_path, "contract.yaml missing description field"))

    total = len(entry_points)
    if broken:
        print(f"runtime_sweep: {len(broken)}/{total} entry points BROKEN\n")
        print(f"{'NODE':<45} {'MODULE':<55} REASON")
        print("-" * 140)
        for node_name, module_path, reason in sorted(broken):
            print(f"{node_name:<45} {module_path:<55} {reason}")
        print(f"\nFAIL: {len(broken)} broken entry points detected.")
        return 1

    print(f"runtime_sweep: {total}/{total} entry points OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
