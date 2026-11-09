# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_work_ledger_no_file_readings_effect``: read the seven.

Prints one ``READING`` line per reading and one ``NO-FILE`` summary line. Exit 0: all seven read WORKING.
Exit 1: at least one is NOT_WORKING or UNKNOWN (the readings are still printed and, with ``--write``,
written). Exit 69: the run itself failed (no appender, an append that did not land).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.adapters import (
    CommandNoFileFacts,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.handlers.handler_no_file_readings import (
    HandlerNoFileReadings,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.models import (
    ModelNoFileRequest,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.overlay import (
    load_overlay,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.protocols import (
    ProtocolNoFileFacts,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    facts: ProtocolNoFileFacts | None = None,
    appender: ProtocolReadingAppender | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="node_work_ledger_no_file_readings_effect")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    overlay = load_overlay(env)
    journal = env.get(overlay.journal_env, "").strip()
    sink: ProtocolEmissionSink = (
        JsonlEmissionSink(Path(journal)) if journal else InMemoryEmissionSink()
    )
    result = HandlerNoFileReadings(
        overlay,
        facts if facts is not None else CommandNoFileFacts(overlay.route, env),
        sink,
        appender,
        backend="real" if journal else "in_memory",
    ).handle(ModelNoFileRequest(correlation_id=uuid.uuid4().hex, write=args.write))
    for reading in result.readings:
        sys.stdout.write(
            f"READING {reading.number} {reading.verdict.value} {reading.name} "
            f"reason={','.join(reading.reasons)} ledger={reading.ledger}\n"
        )
    sys.stdout.write(
        f"NO-FILE status={result.status} all_working={'yes' if result.all_working else 'no'} "
        f"failure={result.failure_code or 'none'} {result.failure_detail}\n"
    )
    if result.status == "failed":
        return 69
    return 0 if result.all_working else 1
