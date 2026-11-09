# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Link this host's lane context ledger archive rolls to one shared copy, in process.

Run by the lab daemon sync's per-tick chore, or by hand: ``python -m
omnimarket.nodes.node_lane_ctx_ledger_dedupe_effect [--check]``. ``--check`` reads what would
change and changes nothing. The context directories, the store and the settle window come from
the node's overlay (``ONEX_SKILL_OVERLAY_ROOTS``); with none the run is indeterminate.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Never

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_node_overlay,
)

from .handlers.adapter_fs import HostLaneCtxFs
from .handlers.adapter_publisher import ContractEventPublisher, contract_topics
from .handlers.handler_lane_ctx_ledger_dedupe import HandlerLaneCtxLedgerDedupe
from .models.model_lane_ctx_ledger_dedupe import ModelLaneCtxLedgerDedupeCommand

NODE_NAME = "node_lane_ctx_ledger_dedupe_effect"
OVERLAY_FIELDS = frozenset({"runs_root", "store_dir", "settle_seconds"})


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse(argv)
        overlay = load_node_overlay(NODE_NAME, fields=OVERLAY_FIELDS)
        missing = sorted(OVERLAY_FIELDS - set(overlay))
        if missing:
            raise ValueError(
                f"no overlay supplies {missing}: put {NODE_NAME}/overlay.yaml under a root "
                "of ONEX_SKILL_OVERLAY_ROOTS"
            )
        command = ModelLaneCtxLedgerDedupeCommand(
            runs_root=overlay["runs_root"],
            store_dir=overlay["store_dir"],
            settle_seconds=int(overlay["settle_seconds"]),
            execute=not args.check,
        )
    except (NodeOverlayError, ValueError, ValidationError) as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    refused_topic, completed_topic, _ = contract_topics()
    handler = HandlerLaneCtxLedgerDedupe(
        fs=HostLaneCtxFs(),
        publisher=ContractEventPublisher(),
        refused_topic=refused_topic,
        completed_topic=completed_topic,
        clock=time.time,
    )
    result = handler.handle(command)
    sys.stdout.write(result.line() + "\n")
    for error in result.errors:
        sys.stderr.write(f"[lane-ctx-ledger-dedupe] {error}\n")
    return result.exit_code


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
