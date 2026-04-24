#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI runtime sweep — verify every onex.nodes entry point has a contract.yaml and handler.

Exits 1 if any broken entry points are detected. Designed for plain-Python CI execution
without the Claude Code harness. Implements the entry-point validation subset of
runtime_sweep (OMN-8611).
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from importlib import import_module
from pathlib import Path

import yaml


def _import_dotted_path(dotted_path: str) -> None:
    module_path, _, attr = dotted_path.rpartition(".")
    if not module_path or not attr:
        raise ValueError(
            f"Expected dotted path with module and attribute: {dotted_path}"
        )
    module = import_module(module_path)
    getattr(module, attr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--import-check",
        action="store_true",
        help=(
            "Also import entry-point modules and contract-declared handlers/models. "
            "Requires project runtime dependencies to be installed."
        ),
    )
    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent.parent
    pyproject_path = repo_root / "pyproject.toml"

    with pyproject_path.open("rb") as f:
        config = tomllib.load(f)

    entry_points: dict[str, str] = (
        config.get("project", {}).get("entry-points", {}).get("onex.nodes", {})
    )

    if not entry_points:
        print('ERROR: No [project.entry-points."onex.nodes"] found in pyproject.toml')
        return 1

    src_root = repo_root / "src"
    sys.path.insert(0, str(src_root))
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
        content = yaml.safe_load(contract.read_text())
        if not isinstance(content, dict):
            broken.append((node_name, module_path, "contract.yaml is not a mapping"))
            continue

        if not content.get("description"):
            broken.append(
                (node_name, module_path, "contract.yaml missing description field")
            )
            continue

        if not args.import_check:
            continue

        try:
            import_module(module_path)
        except Exception as exc:
            broken.append((node_name, module_path, f"entry point import failed: {exc}"))
            continue

        handler = content.get("handler")
        if not isinstance(handler, dict):
            continue

        handler_module = handler.get("module")
        handler_class = handler.get("class")
        if not isinstance(handler_module, str) or not isinstance(handler_class, str):
            broken.append(
                (node_name, module_path, "contract.yaml missing handler module/class")
            )
            continue

        try:
            _import_dotted_path(f"{handler_module}.{handler_class}")
        except Exception as exc:
            broken.append((node_name, module_path, f"handler import failed: {exc}"))
            continue

        input_model = handler.get("input_model")
        if isinstance(input_model, str) and input_model:
            try:
                _import_dotted_path(input_model)
            except Exception as exc:
                broken.append(
                    (node_name, module_path, f"input_model import failed: {exc}")
                )

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
