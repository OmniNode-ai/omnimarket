# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census unit's entry: one daily census run that prints and keeps the daily record.

    python -m omnimarket.nodes.node_work_ledger_file_census_effect.runtime_file_census_tick --omni-home <root> --state-dir <dir>

The host installer renders the unit (``ledger-file-census-systemd`` in ``node_host_installer_registry_units``) with
``ONEX_LEDGER_CENSUS_ROOT`` set to the registry root. One pass runs the census with the overlay and the ratchet file
packaged beside this node (paths that do not depend on the unit's working directory) and the ``--record`` flag, so the
daily record is printed and written to ``last-run.json`` in the state directory whether the census passed or failed. Exit codes are the census's own: 0 at or under every ratchet row, 1 a count grew, 69 a read failed.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from omnimarket.nodes.node_work_ledger_file_census_effect.runtime_file_census import (
    main as run_census,
)

PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"
EVIDENCE_FILE = "last-run.json"
RECORD_PREFIX = "CENSUS-RECORD "
ROOT_ENV = "ONEX_LEDGER_CENSUS_ROOT"
_RATCHET_FILE = Path(__file__).resolve().parent / "ratchet.yaml"
_CENSUS_ARGV = (
    "--packaged-overlay",
    "--record",
    "--ratchet-file",
    str(_RATCHET_FILE),
)


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def _record_from(output: str) -> object:
    for line in output.splitlines():
        if line.startswith(RECORD_PREFIX):
            return json.loads(line[len(RECORD_PREFIX) :])
    return None


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    args_in = list(sys.argv[1:] if argv is None else argv)
    if args_in == [PROBE_FLAG]:
        out.write(json.dumps({"status": PROBE_STATUS}) + "\n")
        return 0
    parser = argparse.ArgumentParser(prog="node_work_ledger_file_census_effect.tick")
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(args_in)
    env = dict(os.environ if environ is None else environ)
    if not env.get(ROOT_ENV, "").strip():
        env[ROOT_ENV] = str(args.registry_root)
    ran_at = datetime.now(UTC).isoformat()
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = run_census(list(_CENSUS_ARGV), environ=env)
    out.write(captured.getvalue())
    _write_atomically(
        args.state_dir / EVIDENCE_FILE,
        json.dumps(
            {
                "ran_at": ran_at,
                "exit_code": code,
                "record": _record_from(captured.getvalue()),
            }
        ),
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
