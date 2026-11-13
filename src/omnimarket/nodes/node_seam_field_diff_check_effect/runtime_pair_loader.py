# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line entry that prints every producer-consumer pair of a source tree as JSON (OMN-24121).

python runtime_pair_loader.py --root PACKAGE=PATH [--root ...] [--envelope-model NAME ...]

STANDALONE ON PURPOSE: the check runs this file against a base revision's source tree,
where this node does not exist yet, so it imports only the standard library and loads
the sibling ``handlers/pair_loader.py`` by file path, never anything from ``omnimarket``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

_LOADER_PATH = Path(__file__).resolve().parent / "handlers" / "pair_loader.py"


def _load_pair_loader() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seam_pair_loader", _LOADER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_LOADER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", default=[], metavar="PACKAGE=PATH")
    parser.add_argument("--envelope-model", action="append", default=[])
    args = parser.parse_args(argv)
    roots = {}
    for item in args.root:
        package, _, path = item.partition("=")
        roots[package] = Path(path)
    loader = _load_pair_loader()
    sys.stdout.write(json.dumps(loader.collect_pairs(roots, args.envelope_model)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
