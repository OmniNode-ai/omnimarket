# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_order_advance_effect.runtime_order_advance tick``: one tick of the order writer.

The timer unit runs this command. It loads the ``gate_store`` and ``order_writer`` overlays,
builds the store and the readiness read they name, runs one tick and prints the typed result as
JSON. Exit 0: the tick advanced, waited or found the order complete. Exit 1: fenced or refused.
Exit 3: an overlay or the connection it names is missing.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_order_advance import (
    EnumOrderAdvanceOutcome,
    ModelOrderAdvanceRequest,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store import (
    GateStoreBackendNotBuiltError,
    build_store,
)
from omnimarket.nodes.node_gate_store_effect.overlay import load_gate_store_policy
from omnimarket.nodes.node_order_advance_effect.handlers.handler_order_advance import (
    HandlerOrderAdvance,
    OrderReadinessNotBuiltError,
    build_readiness,
)
from omnimarket.nodes.node_order_advance_effect.overlay import (
    load_order_writer_policy,
)

_OK = frozenset(
    {
        EnumOrderAdvanceOutcome.ADVANCED,
        EnumOrderAdvanceOutcome.WAITING,
        EnumOrderAdvanceOutcome.COMPLETE,
    }
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_order_advance_effect")
    parser.add_argument("command", choices=["tick"])
    parser.parse_args(argv)
    try:
        store_policy = load_gate_store_policy()
        writer_policy = load_order_writer_policy()
        handler = HandlerOrderAdvance(
            build_store(store_policy),
            build_readiness(writer_policy),
            writer_policy,
            store_policy.order,
        )
    except (
        NodeOverlayError,
        GateStoreBackendNotBuiltError,
        OrderReadinessNotBuiltError,
    ) as exc:
        sys.stderr.write(f"order writer cannot start: {exc}\n")
        return 3
    result = handler.handle(
        ModelOrderAdvanceRequest(correlation_id=f"tick-{uuid.uuid4()}", now=time.time())
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.outcome in _OK else 1


if __name__ == "__main__":
    sys.exit(main())
