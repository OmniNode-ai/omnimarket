# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model-free lab service: every tick uses the declared command/terminal bus."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from omnibase_core.runtime.runtime_local import RuntimeLocal

from .models import ModelFixInstallRequest, ModelFixInstallResult


def tick(*, apply: bool = True) -> int:
    request = ModelFixInstallRequest(apply=apply)
    with TemporaryDirectory(prefix="automatic-fix-install-") as scratch:
        root = Path(scratch)
        source = root / "request.json"
        source.write_text(request.model_dump_json())
        runtime = RuntimeLocal(
            Path(__file__).with_name("contract.yaml"),
            input_path=source,
            state_root=root / "runtime",
            run_id=UUID(request.request_id),
            backend_overrides={"event_bus": "inmemory"},
            timeout=3600,
            host_handlers=True,
        )
        runtime.run()
        result = runtime.handler_result
    if not isinstance(result, ModelFixInstallResult):
        sys.stderr.write(
            "automatic-fix-install: command dispatch produced no terminal result\n"
        )
        return 2
    sys.stdout.write(result.model_dump_json() + "\n")
    sys.stdout.flush()
    return 0 if result.status in {"completed", "locked"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--overlay", type=Path, help="server-owned deployment overlay")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if not 1 <= args.interval_seconds <= 300:
        parser.error("--interval-seconds must be between 1 and 300")
    previous = os.environ.get("ONEX_FIX_INSTALL_OVERLAY")
    if args.overlay is not None:
        os.environ["ONEX_FIX_INSTALL_OVERLAY"] = str(args.overlay.resolve())
    try:
        while True:
            code = tick(apply=not args.dry_run)
            if not args.serve:
                return code
            time.sleep(args.interval_seconds)
    finally:
        if args.overlay is not None:
            if previous is None:
                os.environ.pop("ONEX_FIX_INSTALL_OVERLAY", None)
            else:
                os.environ["ONEX_FIX_INSTALL_OVERLAY"] = previous


if __name__ == "__main__":
    raise SystemExit(main())
