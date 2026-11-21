# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drain a host's bus outbox from the command line (the ``bus-drain`` verb of the retired ledger tool).

``--ledger`` names the ledger, ``--limit`` bounds the run, ``--retry-refused`` retries the files of ``refused/`` instead
(the file not tried for longest first) and reports ``remaining=`` as the refused files left. ``--publisher-argv`` is the
publisher's argv prefix (shell-quoted), with ``--lane`` and ``--host`` for the send. Prints
``BUS-DRAIN replayed= refused= remaining= stopped=`` (``BUS-RETRY-REFUSED`` with ``--retry-refused``). Exit 0 when
nothing is left, 75 when requests still wait or the retry stopped, 65 when a replay was refused, 3 INDETERMINATE on
stderr for a bad input. ``main`` takes a ``sender`` so a test or a caller supplies its own.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from typing import Never

from omnimarket.nodes.node_ledger_outbox_drain_effect.handlers.handler_outbox_drain import (
    HandlerOutboxDrain,
    Sender,
)
from omnimarket.nodes.node_ledger_outbox_drain_effect.models.model_outbox_drain import (
    ModelOutboxDrainRequest,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def main(argv: list[str] | None = None, *, sender: Sender | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--retry-refused", action="store_true")
    parser.add_argument(
        "--publisher-argv", default="", help="the publisher's argv prefix"
    )
    parser.add_argument("--lane", default="outbox-drain")
    parser.add_argument("--host", default="")
    try:
        args = parser.parse_args(argv)
        publisher = tuple(shlex.split(args.publisher_argv))
        if sender is None and not publisher:
            raise ValueError("the publisher argv prefix is empty")
        request = ModelOutboxDrainRequest(
            ledger_path=args.ledger,
            refused=args.retry_refused,
            limit=args.limit,
            publisher_argv=publisher,
            lane=args.lane,
            host=args.host,
        )
        result = HandlerOutboxDrain(sender).handle(request)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    label = "BUS-RETRY-REFUSED" if args.retry_refused else "BUS-DRAIN"
    sys.stdout.write(
        f"{label} replayed={result.replayed} refused={result.refused} "
        f"remaining={result.remaining} stopped={result.stopped or 'no'}\n"
    )
    if result.refused:
        return 65
    return 75 if result.remaining or (args.retry_refused and result.stopped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
