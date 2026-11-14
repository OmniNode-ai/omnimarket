# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ledger_hold_verdict_effect``: is this pull request held?

Reads the ``holds`` answer through ``node_work_ledger_reader_effect`` (the bus-fed projection)
ONCE, decides each ``--pr`` with ``HandlerHoldVerdict`` and prints the verdicts on stdout and one
``HOLD-VERDICT`` line per pull request on stderr (``source=file-fallback`` when the file's lines
answered). The caller carries the file's ``holds`` lines with ``--file-holds`` only as the
fallback the overlay allows.

With a single ``--pr`` the output is the bare verdict word (``HELD``, ``CLEAR`` or
``UNDECIDED``) and the exit code is 0 for CLEAR, 3 for HELD and 2 for UNDECIDED, which is a
stop, never CLEAR. With two or more ``--pr`` values the output is one line per pull request in
the order given, ``<pr> <VERDICT> <hold ids joined by comma, or - when none>``, and the exit
code is 2 if any pull request is UNDECIDED, else 0: a HELD pull request does not change the
exit code in the batch form, the caller reads the lines.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderQuery,
    ModelReaderRequest,
)
from omnimarket.nodes.node_ledger_hold_verdict_effect.adapters import (
    InMemoryHoldVerdictSink,
    JournalHoldVerdictSink,
)
from omnimarket.nodes.node_ledger_hold_verdict_effect.handlers import HandlerHoldVerdict
from omnimarket.nodes.node_ledger_hold_verdict_effect.models import (
    EnumHoldVerdict,
    ModelHoldVerdictRequest,
)
from omnimarket.nodes.node_ledger_hold_verdict_effect.overlay import load_overlay
from omnimarket.nodes.node_ledger_hold_verdict_effect.protocols import (
    ProtocolHoldVerdictEventSink,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    build_point_read,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import (
    load_overlay as load_reader_overlay,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)

READER_NAME = "pause-check"
EXIT = {
    EnumHoldVerdict.CLEAR: 0,
    EnumHoldVerdict.HELD: 3,
    EnumHoldVerdict.UNDECIDED: 2,
}


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    point_read: ProtocolLedgerPointRead | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="node_ledger_hold_verdict_effect")
    parser.add_argument("--pr", action="append", required=True, help="repo#number")
    parser.add_argument("--lane")
    parser.add_argument(
        "--file-holds",
        help="file of the `holds` verb lines read from the ledger file (the fallback only)",
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    reader_overlay = load_reader_overlay(env)
    overlay = load_overlay(env)
    reader = HandlerWorkLedgerReader(
        reader_overlay,
        point_read
        if point_read is not None
        else build_point_read(reader_overlay.route, env),
        InMemoryEventSink(),
    )
    answer = reader.handle(
        ModelReaderRequest(reader=READER_NAME, query=EnumReaderQuery.HOLDS)
    )
    file_lines = (
        tuple(Path(args.file_holds).read_text(encoding="utf-8").splitlines())
        if args.file_holds
        else None
    )
    journal = env.get(overlay.journal_env, "").strip()
    sink: ProtocolHoldVerdictEventSink = (
        JournalHoldVerdictSink(Path(journal)) if journal else InMemoryHoldVerdictSink()
    )
    handler = HandlerHoldVerdict(overlay, sink)
    batch = len(args.pr) > 1
    exit_code = 0
    for pr in args.pr:
        verdict = handler.handle(
            ModelHoldVerdictRequest(
                pr=pr,
                lane=args.lane,
                node_answer=answer,
                file_fallback_lines=file_lines,
            )
        )
        sys.stderr.write(verdict.log_line() + "\n")
        if batch:
            hold_ids = ",".join(verdict.hold_ids) if verdict.hold_ids else "-"
            sys.stdout.write(f"{pr} {verdict.verdict.value} {hold_ids}\n")
            if verdict.verdict is EnumHoldVerdict.UNDECIDED:
                exit_code = EXIT[EnumHoldVerdict.UNDECIDED]
        else:
            sys.stdout.write(verdict.verdict.value + "\n")
            exit_code = EXIT[verdict.verdict]
    return exit_code
