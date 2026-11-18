# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the work ledger host mirror, the unit the host installer renders (OMN-25537).

The unit passes ``--omni-home`` and ``--state-dir`` and declares ``ONEX_LEDGER_BUS_READ_COMMAND``. The mirror is
``<omni-home>/.onex_state/ledger-mirror/host-ledger-mirror.md``. ``last-run.json`` in the state directory holds
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
MIRROR_RELATIVE = ".onex_state/ledger-mirror/host-ledger-mirror.md"
STATE_FILE = "mirror-state.json"
# Under the contract's 100 s run limit for a pass of up to a few pages; one read answers in 10 to 15 s.
READ_TIMEOUT_SECONDS = 45.0


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
    handler = HandlerWorkLedgerHostMirror(AdapterBusRead(command, READ_TIMEOUT_SECONDS))
    result = handler.handle(
        ModelHostMirrorRequest(
            mirror_path=args.registry_root / MIRROR_RELATIVE,
            state_path=args.state_dir / STATE_FILE,
        )
    )
    code = 0 if result.status is EnumHostMirrorStatus.MIRRORED else 1
    sys.stdout.write(
        f"{NODE_NAME} {result.status.value} rows_appended={result.rows_appended} "
        f"lag_seconds={result.lag_seconds} {result.detail}\n"
    )
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
