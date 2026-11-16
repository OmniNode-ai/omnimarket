# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the capture silence watch, the unit the host installer renders.

The unit passes ``--omni-home`` and ``--state-dir``. The tick runs the node's
existing CLI (``tick``) and records the exit code in ``last-run.json`` in the
state directory, so the unit's installer can tell a ticking unit from a silent
one. When the node exits 2 because it published a silence alarm, the unit exits
0 and says so in the evidence; any other non-zero code stays a failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from omnimarket.nodes.node_capture_silence_watch_effect.__main__ import (
    main as node_main,
)

# The node returns 2 when a silence alarm was published; that is the tick
# doing its job, so the unit must not read as failed.
ALARM_DELIVERED_CODE: Final[int] = 2


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="omnimarket.nodes.node_capture_silence_watch_effect.tick",
        description=__doc__,
    )
    parser.add_argument("--omni-home", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    ran_at = datetime.now(UTC).isoformat()
    try:
        code = node_main(["tick"])
    except Exception as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        _write_atomically(
            args.state_dir / "last-run.json",
            json.dumps({"ran_at": ran_at, "exit_code": 3, "indeterminate": str(exc)}),
        )
        return 3
    if code == ALARM_DELIVERED_CODE:
        _write_atomically(
            args.state_dir / "last-run.json",
            json.dumps(
                {
                    "ran_at": ran_at,
                    "exit_code": 0,
                    "node_exit_code": ALARM_DELIVERED_CODE,
                    "alarm_delivered": True,
                }
            ),
        )
        return 0
    _write_atomically(
        args.state_dir / "last-run.json",
        json.dumps({"ran_at": ran_at, "exit_code": code}),
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
