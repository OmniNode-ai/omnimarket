# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register_tick [--probe]``.

The host installer's tick form of the register reading (OMN-24944): one pass reads every row of the
standing-rulings register exactly as ``python -m omnimarket.nodes.node_ruling_register_effect`` does, prints one
``MISSING ruling-not-running:<row>`` line per row that is not TRUE on stdout and records the reading in
``<state-dir>/last-run.json``, whose age is the unit's heartbeat. A row that is not TRUE is the reading, not a
failed tick: the tick exits 0 when the register was read and 3 when an overlay is missing or refuses to validate.
``--probe`` loads the policy and the route and prints ``PROBE ok``: it runs no read and writes nothing (the
installer runs a probe before it loads the unit). ``--omni-home`` is the installer's tick argument; the reading
takes its settings from the overlays and its environment, not from it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_ruling_register import EnumRulingVerdict
from omnimarket.nodes.node_ruling_register_compute.overlay import (
    load_ruling_register_policy,
)
from omnimarket.nodes.node_ruling_register_effect.overlay import load_route
from omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register import (
    read_register,
)

EXIT_OK = 0
EXIT_UNREADABLE = 3


def _write_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temporary file beside it, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ruling-register-tick", description=__doc__)
    parser.add_argument("--omni-home", default="")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.probe:
        try:
            load_ruling_register_policy(os.environ)
            load_route(os.environ)
        except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
            sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
            return EXIT_UNREADABLE
        sys.stdout.write("PROBE ok\n")
        return EXIT_OK
    try:
        judged, sink_errors = read_register(os.environ)
    except NodeOverlayError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_UNREADABLE
    for error in sink_errors:
        sys.stderr.write(f"RULING-REGISTER-JOURNAL-ERROR {error}\n")
    for line in judged.lines():
        sys.stdout.write(line + "\n")
    counts = {
        verdict.value: sum(1 for v in judged.verdicts if v.verdict is verdict)
        for verdict in EnumRulingVerdict
    }
    record = {
        "read_at": datetime.now(UTC).isoformat(),
        "passed": judged.passed,
        "rows": len(judged.verdicts),
        "counts": counts,
        "verdicts": {v.row_id: v.verdict.value for v in judged.verdicts},
    }
    sys.stdout.write(
        f"RULING-REGISTER rows={record['rows']} "
        + " ".join(f"{name}={count}" for name, count in counts.items())
        + "\n"
    )
    if args.state_dir:
        _write_atomically(
            Path(args.state_dir) / "last-run.json", json.dumps(record, sort_keys=True)
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
