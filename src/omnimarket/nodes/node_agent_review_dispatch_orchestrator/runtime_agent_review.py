# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request entry of the agent review dispatch: one pull request head, on request.

    python -m omnimarket.nodes.node_agent_review_dispatch_orchestrator.runtime_agent_review \
        --repo OWNER/NAME --pr N --head-sha SHA --scm-bus inmemory|kafka [--bus-lane LANE]

The orchestrator calls this for one ``owner/name#n`` at its exact head. Nothing starts
it by itself: there is no trigger, no tick and no subscriber that runs it. It loads the
node overlay, binds every production port (``build_production_ports``), runs the
handler once and prints the one terminal as JSON on stdout.

A port whose environment is absent on this host ends the request before any port is
called, with the port named on stderr. Exit 0 when the terminal is ``completed``; 1 for
a refused or failed terminal and for an unbound port; 2 for input the entry refuses.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Mapping

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

from .adapters.adapter_agent_review_ports import (
    PortNotBoundError,
    build_production_ports,
)
from .handlers.handler_agent_review_dispatch import HandlerAgentReviewDispatch
from .models import EnumAgentReviewOutcome, ModelAgentReviewRequest
from .overlay import load_agent_review_config


def main(
    argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--command-id", default="")
    parser.add_argument("--scm-bus", required=True, choices=("inmemory", "kafka"))
    parser.add_argument("--bus-lane", default=None)
    args = parser.parse_args(argv)
    try:
        request = ModelAgentReviewRequest(
            command_id=args.command_id or str(uuid.uuid4()),
            repo=args.repo,
            pr=args.pr,
            head_sha=args.head_sha,
        )
    except ValidationError as exc:
        parser.error(f"the request is not accepted: {exc}")
    try:
        config = load_agent_review_config(environ)
        ports = build_production_ports(
            config, scm_bus=args.scm_bus, scm_lane=args.bus_lane, environ=environ
        )
    except (NodeOverlayError, PortNotBoundError) as exc:
        sys.stderr.write(f"agent review: not bound on this host: {exc}\n")
        return 1
    handler = HandlerAgentReviewDispatch(
        config=config,
        head=ports.head,
        lab=ports.lab,
        signer=ports.signer,
        store=ports.store,
        poster=ports.poster,
        terminal_log=ports.terminal_log,
        clock=ports.clock,
    )
    terminal = asyncio.run(handler.handle(request))
    sys.stdout.write(terminal.model_dump_json() + "\n")
    return 0 if terminal.outcome is EnumAgentReviewOutcome.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
