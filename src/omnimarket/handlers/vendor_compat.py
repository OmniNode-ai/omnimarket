"""Place omnibase_compat beside a historical tree for one test run."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path


def vendor_compat(tree: Path, env: dict[str, str]) -> tuple[int, str] | None:
    """Place omnibase_compat beside a historical tree for one test run.

    A historical commit's tree may import omnibase_compat, which the project
    retired (OMN-22385). The project environment stays free of it. Returns a
    (returncode, stderr tail) failure, or None with PYTHONPATH set in ``env``
    (the tree's src, plus the vendored copy when needed).
    """
    env["PYTHONPATH"] = str(tree / "src")
    if importlib.util.find_spec("omnibase_compat") is not None:
        return None
    vendored = tree / ".vendored"
    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(vendored),
            "omnibase-compat==0.5.7",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if installed.returncode != 0:
        return installed.returncode, installed.stderr[-800:]
    env["PYTHONPATH"] = f"{tree / 'src'}{os.pathsep}{vendored}"
    return None
