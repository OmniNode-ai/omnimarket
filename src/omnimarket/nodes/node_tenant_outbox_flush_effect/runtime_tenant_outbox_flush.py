# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry of ``python -m omnimarket.nodes.node_tenant_outbox_flush_effect``.

``--probe`` loads the overlay and builds the handler without connecting, and prints what it found. ``--run`` performs one
flush pass with the overlay's batch limit and prints the typed answer. Exit 0 when the pass ended ``flushed`` or
``nothing_to_flush``; 1 for every other typed outcome; 3 for an overlay that does not load or an unknown mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from uuid import uuid4

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.handler_tenant_outbox_flush import (
    publish_topic_from_contract,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.models.model_tenant_outbox_flush import (
    EnumTenantOutboxFlushOutcome,
    ModelTenantOutboxFlushCommand,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.overlay import (
    ModelTenantOutboxFlushOverlay,
    build_flush,
    load_overlay,
)

_OK_OUTCOMES = frozenset(
    {
        EnumTenantOutboxFlushOutcome.FLUSHED,
        EnumTenantOutboxFlushOutcome.NOTHING_TO_FLUSH,
    }
)


async def _run(overlay: ModelTenantOutboxFlushOverlay) -> tuple[bool, str]:
    handler, closers = build_flush(overlay, topic=publish_topic_from_contract())
    try:
        result = await handler.handle(
            ModelTenantOutboxFlushCommand(
                command_id=uuid4(), batch_limit=overlay.batch_limit
            )
        )
    finally:
        for close in closers:
            await close()
    return result.outcome in _OK_OUTCOMES, result.model_dump_json()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_tenant_outbox_flush_effect")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true")
    mode.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    if not (args.probe or args.run):
        sys.stderr.write("INDETERMINATE: pass --probe or --run\n")
        return 3
    try:
        overlay = load_overlay()
        if args.probe:
            build_flush(overlay, topic=publish_topic_from_contract())
            sys.stdout.write(
                json.dumps(
                    {
                        "probe": "ready",
                        "lane": overlay.lane,
                        "batch_limit": overlay.batch_limit,
                    }
                )
                + "\n"
            )
            return 0
        ok, answer = asyncio.run(_run(overlay))
    except (NodeOverlayError, ValidationError, OSError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}\n")
        return 3
    sys.stdout.write(answer + "\n")
    return 0 if ok else 1
