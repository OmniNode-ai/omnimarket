# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the work ledger host mirror, the unit the host installer renders (OMN-25537).

The unit passes ``--omni-home`` and ``--state-dir`` and declares ``ONEX_LEDGER_BUS_READ_COMMAND``. The mirror is
``<omni-home>/.onex_state/ledger-mirror/host-ledger-mirror.md`` (authority rows, 1 day) and
``host-ledger-rulings.md`` (rulings and consents, 30 days). ``last-run.json`` in the state directory holds
the tick's receipt, with the mirror's lag behind the record, so the unit's installer can tell a ticking unit from a
silent one. Exit 0: the mirror holds every row the record held at the read. Exit 1: the read failed or the page
bound ended the tick; the next tick resumes. Exit 3: the unit supplies no read command.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_work_ledger_host_mirror_effect.handlers.adapter_bus_read import (
    AdapterBusRead,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.handlers.handler_work_ledger_host_mirror import (
    HandlerWorkLedgerHostMirror,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.models.model_work_ledger_host_mirror import (
    EnumHostMirrorStatus,
    ModelHostMirrorRequest,
)

NODE_NAME = "node_work_ledger_host_mirror_effect"
READ_COMMAND_ENV = "ONEX_LEDGER_BUS_READ_COMMAND"
MIRROR_DIRECTORY = ".onex_state/ledger-mirror"
# The rows a lane reads to find an authority or an owner, not the status and friction rows that are most of the
# ledger (about 10,500 rows a day against about 4,000).
AUTHORITY_KINDS = (
    "RULING",
    "OPERATOR-CONSENT",
    "MSG",
    "ACK",
    "HOLD",
    "RELEASE",
    "CLAIM",
    "TERMINAL",
    "CORRECTION",
)
RULING_KINDS = ("RULING", "OPERATOR-CONSENT")
DAY_SECONDS = 86400


@dataclass(frozen=True)
class Stream:
    """One mirror file: the row kinds it holds and how far back the first read goes."""

    name: str
    kinds: tuple[str, ...]
    window_seconds: int


# The recent authority rows in one file; a ruling is cited for weeks, so the rulings and consents go back further.
STREAMS = (
    Stream("host-ledger-mirror", AUTHORITY_KINDS, DAY_SECONDS),
    Stream("host-ledger-rulings", RULING_KINDS, 30 * DAY_SECONDS),
)
# One read answers in 5 to 12 s; two streams of up to ten pages each end inside the contract's 100 s run limit,
# and a read that hangs ends at this bound with the pages before it kept.
READ_TIMEOUT_SECONDS = 25.0


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE_NAME}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    ran_at = datetime.now(UTC).isoformat()
    command = shlex.split(os.environ.get(READ_COMMAND_ENV, ""))
    if not command:
        message = (
            f"{READ_COMMAND_ENV} is not set: the unit declares the ledger read command"
        )
        sys.stderr.write(f"INDETERMINATE: {message}\n")
        _write_atomically(
            args.state_dir / "last-run.json",
            json.dumps({"ran_at": ran_at, "exit_code": 3, "indeterminate": message}),
        )
        return 3
    results = {}
    for stream in STREAMS:
        handler = HandlerWorkLedgerHostMirror(
            AdapterBusRead(command, READ_TIMEOUT_SECONDS, stream.kinds)
        )
        results[stream.name] = handler.handle(
            ModelHostMirrorRequest(
                mirror_path=args.registry_root / MIRROR_DIRECTORY / f"{stream.name}.md",
                state_path=args.state_dir / f"{stream.name}-state.json",
                window_seconds=stream.window_seconds,
            )
        )
    code = (
        0
        if all(r.status is EnumHostMirrorStatus.MIRRORED for r in results.values())
        else 1
    )
    for name, result in results.items():
        sys.stdout.write(
            f"{NODE_NAME} {name} {result.status.value} rows_appended={result.rows_appended} "
            f"lag_seconds={result.lag_seconds} {result.detail}\n"
        )
    _write_atomically(
        args.state_dir / "last-run.json",
        json.dumps(
            {
                "ran_at": ran_at,
                "exit_code": code,
                "results": {n: r.model_dump(mode="json") for n, r in results.items()},
            }
        ),
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
