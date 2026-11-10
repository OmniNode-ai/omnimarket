# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the lane context ledger dedupe, the unit the host installer renders (OMN-23297).

The unit passes ``--omni-home`` and ``--state-dir``. A pass that refused a roll exits 1; an overlay that
supplies no setting exits 3 and links nothing. Either way ``last-run.json`` in the state directory says
what the tick did, so the unit's installer can tell a ticking unit from a silent one.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

from .__main__ import NODE_NAME, report, run


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE_NAME}.tick", description=__doc__)
    parser.add_argument("--omni-home", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    ran_at = datetime.now(UTC).isoformat()
    try:
        result = run(execute=True)
    except (NodeOverlayError, ValueError, ValidationError) as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        _write_atomically(
            args.state_dir / "last-run.json",
            json.dumps({"ran_at": ran_at, "exit_code": 3, "indeterminate": str(exc)}),
        )
        return 3
    code = report(result)
    _write_atomically(
        args.state_dir / "last-run.json",
        json.dumps(
            {
                "ran_at": ran_at,
                "exit_code": code,
                "result": result.model_dump(mode="json"),
            }
        ),
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
