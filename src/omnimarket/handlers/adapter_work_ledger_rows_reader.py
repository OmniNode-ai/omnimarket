# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production ProtocolLedgerRowsReader: the ledger reader module, run as a subprocess (OMN-20547).

The interpreter is the first word of ``ONEX_LEDGER_BUS_APPEND_COMMAND`` (the ledger interpreter every
moved reader runs under). The reader module answers from the bus-fed projection and prints one row per
line; exit 0 is a fresh answer, exit 1 a stale one (printed, and the reader's own line is kept on stderr),
every other exit (3 not-moved, 69 unreadable, and an exit 1 whose stderr carries no ``status=stale`` reader line, such as an
interpreter that cannot import the module) is a ``LedgerReadError``. No ledger file is opened.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from omnimarket.protocols.protocol_ledger_rows_reader import LedgerReadError

__all__ = ["READER_MODULE", "SubprocessLedgerRowsReader"]

READER_MODULE = "omnimarket.nodes.node_work_ledger_reader_effect"
INTERPRETER_ENV = "ONEX_LEDGER_BUS_APPEND_COMMAND"
_STALE = "status=stale"


class SubprocessLedgerRowsReader:
    """Runs ``<interpreter> -m <reader module> --reader NAME --query rows ...`` and returns the rows."""

    def __init__(
        self,
        environ: Mapping[str, str],
        *,
        timeout_seconds: float = 120.0,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._environ = environ
        self._timeout = timeout_seconds
        self._run = run

    def _interpreter(self) -> str:
        raw = self._environ.get(INTERPRETER_ENV, "").strip()
        try:
            words = shlex.split(raw)
        except ValueError as exc:
            raise LedgerReadError(
                f"{INTERPRETER_ENV} does not split into a command ({exc})"
            ) from exc
        if not words:
            raise LedgerReadError(
                f"{INTERPRETER_ENV} is not set; its first word is the interpreter the ledger reader runs under"
            )
        return words[0]

    def rows(
        self,
        *,
        reader: str,
        window_hours: int,
        kinds: Sequence[str] = (),
        lanes: Sequence[str] = (),
    ) -> tuple[str, ...]:
        argv = [
            self._interpreter(),
            "-m",
            READER_MODULE,
            "--reader",
            reader,
            "--query",
            "rows",
            "--window-hours",
            str(window_hours),
        ]
        for kind in kinds:
            argv += ["--kind", kind]
        for lane in lanes:
            argv += ["--row-lane", lane]
        try:
            done = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                env=dict(self._environ),
            )
        except subprocess.TimeoutExpired as exc:
            raise LedgerReadError(
                f"reader {reader} timed out after {self._timeout:g}s"
            ) from exc
        except OSError as exc:
            raise LedgerReadError(
                f"reader {reader} could not run ({type(exc).__name__})"
            ) from exc
        stderr = str(done.stderr or "")
        stale = done.returncode == 1 and _STALE in stderr
        if done.returncode != 0 and not stale:
            tail = " ".join(stderr.split())[-200:]
            raise LedgerReadError(f"reader {reader} exited {done.returncode}: {tail}")
        if stale:
            sys.stderr.write(stderr)
        return tuple(line for line in str(done.stdout).splitlines() if line.strip())
