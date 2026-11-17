# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register_tick [--probe]``.

The host installer's tick form of the register reading (OMN-24944): one pass reads every row of the
standing-rulings register exactly as ``python -m omnimarket.nodes.node_ruling_register_effect`` does, prints one
``MISSING ruling-not-running:<row>`` line per row that is not TRUE on stdout and records the reading in
``<state-dir>/last-run.json``, whose age is the unit's heartbeat. A row that is not TRUE is the reading, not a
failed tick: the tick exits 0 when the register was read and 3 (``INDETERMINATE: <type>: <text>`` on stderr, on the probe and the
run alike) when an overlay is missing or refuses to validate or the record cannot be written. Only the loaders and the write
are held to that: an error raised inside the read itself is a bug and ends the run with its traceback, never as INDETERMINATE.
A journal write that failed costs its row's event only and is listed in ``journal_errors`` of the record, which also names the
``journal`` file the events went to (empty when the route's journal variable is unset and the events stay in memory).
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
# The exceptions of the two places that may end the tick with 3 and a line on stderr instead of a traceback: the
# loaders (an overlay that is missing or refuses to validate, a value a loader did not wrap, a file it cannot read)
# and the write of the record. Nothing else is caught: a ValueError or OSError from the read is a bug.
LOADER_ERRORS = (NodeOverlayError, ValidationError, ValueError, OSError)
WRITE_ERRORS = (OSError, ValueError)


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
    try:
        load_ruling_register_policy(os.environ)
        route = load_route(os.environ)
    except LOADER_ERRORS as exc:
        return _indeterminate(exc)
    if args.probe:
        sys.stdout.write("PROBE ok\n")
        return EXIT_OK
    return _run(args.state_dir, os.environ.get(route.journal_env, "").strip())


def _indeterminate(exc: BaseException) -> int:
    sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
    return EXIT_UNREADABLE


def _run(state_dir: str, journal: str) -> int:
    """One pass: read the register, print the MISSING lines and the summary, record the reading under ``state_dir``."""
    judged, sink_errors = read_register(os.environ)
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
        # A journal write that failed loses its row's event, not its reading: the failures are part of the record.
        "journal_errors": list(sink_errors),
        # The file the events went to; empty when the route's journal variable is unset (the events stay in memory).
        "journal": journal,
    }
    sys.stdout.write(
        f"RULING-REGISTER rows={record['rows']} "
        + " ".join(f"{name}={count}" for name, count in counts.items())
        + "\n"
    )
    if state_dir:
        try:
            _write_atomically(
                Path(state_dir) / "last-run.json", json.dumps(record, sort_keys=True)
            )
        except WRITE_ERRORS as exc:
            return _indeterminate(exc)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
