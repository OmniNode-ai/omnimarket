# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the additive in-place install for the reconcile pass: print the receipt as JSON.

Exit 0 when the venv now carries the locked versions (or already did), 3 when the
pass rolled back or could not stage, so the caller falls back to its staged path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..models.model_venv_atomic_install import (
    EnumVenvInstallStatus,
    ModelVenvAtomicInstallRequest,
)
from .handler_venv_atomic_install import HandlerVenvAtomicInstall

REFUSED = "VENV-ATOMIC-INSTALL-REFUSED"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv-root", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument(
        "--uv", required=True, help="the uv executable the installer runs"
    )
    args = parser.parse_args(argv)
    python = args.venv_root / "bin" / "python"
    sites = sorted((args.venv_root / "lib").glob("python3*/site-packages"))
    if len(sites) != 1:
        sys.stderr.write(
            f"{REFUSED}: expected one site-packages under {args.venv_root}\n"
        )
        return 3
    result = HandlerVenvAtomicInstall().handle(
        ModelVenvAtomicInstallRequest(
            venv_root=args.venv_root,
            site_packages=sites[0],
            staging_dir=args.staging_dir,
            journal_path=args.journal,
            lock_file=args.lock_file,
            python=python,
            installer_command=(args.uv,),
        )
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    ok = result.status in (EnumVenvInstallStatus.NOOP, EnumVenvInstallStatus.INSTALLED)
    if not ok:
        sys.stderr.write(f"{REFUSED}: {result.status.value}: {result.reason}\n")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
