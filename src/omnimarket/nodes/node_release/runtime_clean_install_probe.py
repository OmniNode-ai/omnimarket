# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the built distribution into a clean environment and import it (OMN-26025).

``verify_pypi_pin_resolvability.py`` proves a wheel's declared pins RESOLVE. It
never imports the package, so a wheel whose code imports a module its metadata
does not declare passes it, is tagged, is published, and breaks every clean
install. This probe closes that gap before the tag step: it installs the wheel
into a throwaway venv in a scratch directory that holds no source tree, then, in
that venv, imports the package and loads every ``onex.nodes`` entry point the
distribution registers. Anything that does not import fails the probe and, as
the probe step has no ``continue-on-error``, the job stops before the tag push.

Stdlib only, run by path from the release job::

    python3 src/omnimarket/nodes/node_release/runtime_clean_install_probe.py dist/

Exit codes: ``0`` installed and every entry point loaded; ``1`` the install or
an import failed, or ``dist/`` did not hold exactly one wheel; ``2`` bad
invocation.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ENTRY_POINT_GROUP = "onex.nodes"

#: Runs inside the clean venv. ``argv[1]`` is the distribution name.
_IMPORT_SNIPPET = """
import importlib, importlib.metadata as md, sys
dist = md.distribution(sys.argv[1])
failures = []
for top in sorted({(f.parts[0]) for f in dist.files or () if f.suffix == ".py" and len(f.parts) > 1 and f.parts[0].isidentifier()}):
    try:
        importlib.import_module(top)
    except Exception as exc:
        failures.append(f"import {top}: {type(exc).__name__}: {exc}")
entry_points = [ep for ep in dist.entry_points if ep.group == sys.argv[2]]
for ep in entry_points:
    try:
        ep.load()
    except Exception as exc:
        failures.append(f"entry point {ep.name} = {ep.value}: {type(exc).__name__}: {exc}")
for line in failures:
    print(line, file=sys.stderr)
print(f"clean-install probe: {len(entry_points)} entry point(s), {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
"""


def _dist_name(wheel: Path) -> str:
    return wheel.name.split("-", 1)[0].replace("_", "-")


def probe(dist_dir: Path, *, uv: str = "uv", timeout: int = 900) -> int:
    wheels = sorted(dist_dir.glob("*.whl"))
    if len(wheels) != 1:
        sys.stderr.write(
            f"ERROR: expected exactly one wheel in {dist_dir}, found {len(wheels)}\n"
        )
        return 1
    wheel = wheels[0].resolve()
    scratch = Path(tempfile.mkdtemp(prefix="clean-install-probe-"))
    try:
        venv = scratch / "venv"
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "VIRTUAL_ENV")
        }
        steps = (
            [uv, "venv", str(venv)],
            [
                uv,
                "pip",
                "install",
                "--python",
                str(venv / "bin" / "python"),
                str(wheel),
            ],
            [
                str(venv / "bin" / "python"),
                "-I",
                "-c",
                _IMPORT_SNIPPET,
                _dist_name(wheel),
                ENTRY_POINT_GROUP,
            ],
        )
        for argv in steps:
            done = subprocess.run(
                argv, cwd=scratch, env=env, timeout=timeout, check=False
            )
            if done.returncode != 0:
                sys.stderr.write(
                    f"ERROR: clean-install probe step failed ({done.returncode}): {argv[0]} {argv[1]}\n"
                )
                return 1
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dist", type=Path, help="directory holding the built wheel")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args(argv)
    if not args.dist.is_dir():
        sys.stderr.write(f"ERROR: {args.dist} is not a directory\n")
        return 2
    return probe(args.dist, uv=args.uv)


if __name__ == "__main__":
    raise SystemExit(main())
