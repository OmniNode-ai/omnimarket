#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pre-commit entry-point format check — fast parse-only validation.

Runs when pyproject.toml is in the changed file set. Verifies that every
entry in [project.entry-points."onex.nodes"] is a valid dotted module path
(no colon:ClassName — these are module-only references, not entry points).

Does NOT do filesystem verification (that's the CI runtime_sweep's job).
This check is purely syntactic — catches malformed entries before commit.

Exits 1 if any entry is malformed.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path


VALID_MODULE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")


def main(files: list[str]) -> int:
    if not any("pyproject.toml" in f for f in files):
        return 0

    repo_root = Path(__file__).parent.parent.parent
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return 0

    with pyproject_path.open("rb") as f:
        config = tomllib.load(f)

    entry_points: dict[str, str] = (
        config.get("project", {})
        .get("entry-points", {})
        .get("onex.nodes", {})
    )

    broken = []
    for name, module_path in entry_points.items():
        if not VALID_MODULE_RE.match(module_path):
            broken.append((name, module_path, "invalid dotted module path"))
        elif ":" in module_path:
            broken.append((name, module_path, "unexpected colon (onex.nodes uses module path, not module:class)"))

    if broken:
        print("entry-point-format: malformed onex.nodes entries in pyproject.toml:\n")
        for name, path, reason in broken:
            print(f"  {name} = {path!r}  →  {reason}")
        print("\nFAIL: fix the entry point declarations before committing.")
        return 1

    print(f"entry-point-format: {len(entry_points)} entries OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
