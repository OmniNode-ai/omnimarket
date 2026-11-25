# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settle the refused requests of a host's bus outbox from the command line.

Takes ``--input`` (a ModelOutboxSettleRequest as JSON, the payload ``onex run-node --input`` takes) and
``--publisher-argv`` (the publisher's argv prefix, shell-quoted), with ``--lane`` and ``--host`` for the rows' send.
Exit 0 when the run ended without stopping early; 1 when it stopped (the result JSON on stdout, the reason on stderr);
3 INDETERMINATE on stderr for a bad input or an OSError. ``main`` takes a ``sender`` so a test or a caller supplies its
own; without one the publisher command builds it.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from typing import Never

from omnimarket.nodes.node_ledger_outbox_settle_effect.handlers.handler_outbox_settle import (
    HandlerOutboxSettle,
    Sender,
)
from omnimarket.nodes.node_ledger_outbox_settle_effect.models.model_outbox_settle import (
    ModelOutboxSettleRequest,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def main(argv: list[str] | None = None, *, sender: Sender | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument(
        "--input", required=True, help="a ModelOutboxSettleRequest as JSON"
    )
    parser.add_argument(
        "--publisher-argv", default="", help="the publisher's argv prefix"
    )
    parser.add_argument("--lane", default="outbox-settle")
    parser.add_argument("--host", default="")
    try:
        args = parser.parse_args(argv)
        request = ModelOutboxSettleRequest.model_validate_json(args.input)
        if sender is None:
            if not shlex.split(args.publisher_argv):
                raise ValueError("the publisher argv prefix is empty")
            request = request.model_copy(
                update={
                    "publisher_argv": tuple(shlex.split(args.publisher_argv)),
                    "lane": args.lane,
                    "host": args.host,
                }
            )
        result = HandlerOutboxSettle(sender).handle(request)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    sys.stdout.write(result.model_dump_json() + "\n")
    if result.stopped:
        sys.stderr.write(f"{result.stopped}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
