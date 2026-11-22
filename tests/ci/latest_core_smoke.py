# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Latest-core smoke: import every node entry point (and its submodules) and name what fails.

The CI job installs the newest omnibase-core release without the lockfile into a
throwaway venv and runs this file by path. It is stdlib-only and is run as a
script (``python latest_core_smoke.py``) so that importing it never imports
``omnimarket`` or core before the smoke starts: a broken core must show up as a
named failure per module, not as a crash of the harness.

Exit 0 when every module imports, 1 when any import fails, 2 when no module was
found to import (a green run that imported nothing is not a pass).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import pkgutil
import re
import sys
from dataclasses import dataclass

ENTRY_POINT_GROUP = "onex.nodes"
_SKIPPED_PARTS = frozenset({"tests", "test", "__main__"})
_CANNOT_IMPORT = re.compile(
    r"cannot import name '(?P<name>[^']+)' from '(?P<mod>[^']+)'"
)


@dataclass(frozen=True)
class SmokeFailure:
    node: str
    module: str
    symbol: str
    error: str


def _symbol_of(error: BaseException) -> str:
    text = str(error)
    match = _CANNOT_IMPORT.search(text)
    if match:
        return f"{match['mod']}.{match['name']}"
    if isinstance(error, ModuleNotFoundError) and error.name:
        return error.name
    if isinstance(error, AttributeError) and getattr(error, "name", None):
        return str(error.name)
    return "<unknown>"


def _try_import(node: str, module: str, failures: list[SmokeFailure]) -> bool:
    try:
        importlib.import_module(module)
    except Exception as error:
        failures.append(
            SmokeFailure(
                node=node,
                module=module,
                symbol=_symbol_of(error),
                error=f"{type(error).__name__}: {error}",
            )
        )
        return False
    return True


def smoke(
    packages: dict[str, str], *, walk: bool = True
) -> tuple[list[SmokeFailure], int]:
    """Import each package, then its non-test submodules; return (failures, modules tried)."""
    failures: list[SmokeFailure] = []
    tried = 0
    for node, package in sorted(packages.items()):
        tried += 1
        if not _try_import(node, package, failures) or not walk:
            continue
        path = getattr(sys.modules[package], "__path__", None)
        if path is None:
            continue
        for info in pkgutil.walk_packages(path, prefix=f"{package}."):
            if _SKIPPED_PARTS & set(info.name.split(".")):
                continue
            tried += 1
            _try_import(node, info.name, failures)
    return failures, tried


def entry_point_packages(group: str = ENTRY_POINT_GROUP) -> dict[str, str]:
    return {
        ep.name: ep.value.split(":")[0]
        for ep in importlib.metadata.entry_points(group=group)
    }


def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="import this package instead of the entry-point group (repeatable)",
    )
    parser.add_argument("--group", default=ENTRY_POINT_GROUP)
    parser.add_argument(
        "--no-walk", action="store_true", help="import packages only, not submodules"
    )
    args = parser.parse_args(argv)
    packages = {m: m for m in args.module} or entry_point_packages(args.group)
    failures, tried = smoke(packages, walk=not args.no_walk)
    for f in failures:
        _emit(
            f"LATEST-CORE-SMOKE FAIL node={f.node} module={f.module} symbol={f.symbol} error={f.error}"
        )
    _emit(
        f"LATEST-CORE-SMOKE packages={len(packages)} modules_tried={tried} failed={len(failures)}"
    )
    if not packages:
        _emit("LATEST-CORE-SMOKE FAIL no packages found to import")
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
