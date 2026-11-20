# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repin what a post-release dev bump leaves behind (OMN-26025).

The reopen-dev job of the release workflow changes ``pyproject.toml`` and, by
``uv lock``, ``uv.lock``. Two more classes of file have to move in the same
commit, or the bump pull request is red by construction:

* every work contract whose ``acceptance.spec.environment`` pins ``uv.lock``
  holds the sha256 of the previous lockfile (the loader refuses the corpus
  with ``input_pin_mismatch: uv.lock``);
* the version floor in ``tests/nodes/test_work_contract_pins_current.py`` and
  in the deploy probe of ``contracts/OMN-18010.yaml``. The floor is what makes
  those two bound checks fail at the merge base; with it left at the previous
  version they pass at base too, the base control runs zero bound checks and
  refuses ("zero bound checks, nothing is bound, so nothing can fail").

Stdlib only and run by path, so the release job needs no project install (the
package ``__init__`` files import the project's dependencies)::

    python3 src/omnimarket/handlers/work_contracts/repin.py --root . --target 0.4.322

Contract files are edited textually, one line each, so no other byte moves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

CORPUS_CONTRACTS = Path("src/omnimarket/configs/work_contracts/contracts")

#: Files whose ``>= (X, Y, Z)`` lockfile version floor follows the bump.
FLOOR_FILES = (
    Path("tests/nodes/test_work_contract_pins_current.py"),
    Path("contracts/OMN-18010.yaml"),
)

_ENV_PIN = re.compile(
    r"(?m)^(?P<head>[ \t]*environment:[ \t]*\n[ \t]*path:[ \t]*uv\.lock[ \t]*\n"
    r"[ \t]*sha256:[ \t]*)(?P<digest>[0-9a-f]{64})(?P<tail>[ \t]*)$"
)
_FLOOR = re.compile(r">= \(\d+, \d+, \d+\)")
_FINAL = re.compile(r"^\d+\.\d+\.\d+$")


class RepinError(Exception):
    """The tree does not have the shape the repin relies on."""


def lock_version(root: Path) -> str:
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    for package in lock.get("package", []):
        if package.get("name") == "omnimarket":
            return str(package["version"])
    raise RepinError("uv.lock has no omnimarket package entry")


def repin_environment_pins(root: Path) -> list[Path]:
    """Rewrite every stale ``uv.lock`` environment pin; return the changed files."""
    digest = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    changed: list[Path] = []
    for path in sorted((root / CORPUS_CONTRACTS).glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        new = _ENV_PIN.sub(lambda m: f"{m['head']}{digest}{m['tail']}", text)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path.relative_to(root))
    return changed


def raise_version_floor(root: Path, target: str) -> list[Path]:
    """Set the lockfile version floor in each floor file to ``target``."""
    if _FINAL.match(target) is None:
        raise RepinError(f"target must be a final X.Y.Z version (got {target!r})")
    floor = ">= (" + ", ".join(target.split(".")) + ")"
    changed: list[Path] = []
    for rel in FLOOR_FILES:
        text = (root / rel).read_text(encoding="utf-8")
        found = _FLOOR.findall(text)
        if len(found) != 1:
            raise RepinError(
                f"{rel}: expected exactly one version floor, found {len(found)}"
            )
        if found[0] != floor:
            (root / rel).write_text(_FLOOR.sub(floor, text), encoding="utf-8")
            changed.append(rel)
    return changed


def repin(root: Path, target: str) -> list[Path]:
    """Repin pins and floors to the relocked tree; return every changed path."""
    locked = lock_version(root)
    if locked != target:
        raise RepinError(
            f"uv.lock pins omnimarket {locked}, not the bump target {target}; "
            "run `uv lock` after the version bump first"
        )
    return [*repin_environment_pins(root), *raise_version_floor(root, target)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path())
    parser.add_argument("--target", required=True, help="the bumped X.Y.Z version")
    args = parser.parse_args(argv)
    try:
        changed = repin(args.root.resolve(), args.target.removeprefix("v"))
    except RepinError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    sys.stdout.write(
        json.dumps({"changed": [str(p) for p in changed]}, indent=2) + "\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
