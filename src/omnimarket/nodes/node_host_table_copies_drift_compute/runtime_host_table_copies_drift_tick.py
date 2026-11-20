# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_table_copies_drift_compute.runtime_host_table_copies_drift_tick [--probe]``.

The host installer's daily tick form of the copies drift reading. Without ``--probe``, it runs the node's ``daily``
verb and leaves ``last-run.json`` in ``--state-dir``, the heartbeat the installer's status reads (without it the status
reads NOT RUNNING after a run that worked). ``--probe`` loads the overlay and prints ``PROBE ok`` without reading a copy,
appending a row or writing the heartbeat.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_table_copies_drift_compute.overlay import load_overlay
from omnimarket.nodes.node_host_table_copies_drift_compute.runtime_host_table_copies_drift import (
    main as drift_main,
)

EVIDENCE_FILE = "last-run.json"


def _write_heartbeat(state_dir: str, code: int) -> None:
    """Leave last-run.json in the state directory: the exit code and when the pass ended, written whole or not at all."""
    if not state_dir:
        return
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    evidence = {
        "status": "ran" if code == 0 else "refused-or-failed",
        "exit_code": code,
        "written_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    partial = directory / f".{EVIDENCE_FILE}.partial"
    partial.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, directory / EVIDENCE_FILE)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="host-table-copies-drift-tick", description=__doc__
    )
    parser.add_argument("--omni-home", default="")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.probe:
        try:
            load_overlay()
        except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
            sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
            return 3
        sys.stdout.write("PROBE ok\n")
        return 0
    code = drift_main(["daily"])
    _write_heartbeat(args.state_dir, code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
