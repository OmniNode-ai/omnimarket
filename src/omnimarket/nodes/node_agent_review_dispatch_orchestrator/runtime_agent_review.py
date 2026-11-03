# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request entry of the agent review dispatch: one pull request head, on request.

    python -m omnimarket.nodes.node_agent_review_dispatch_orchestrator.runtime_agent_review \
        --repo OWNER/NAME --pr N --head-sha SHA

The orchestrator calls this for one ``owner/name#n`` at its exact head, outside the
runtime that serves the node's command topic. It binds the handler as the runtime does
(the node overlay, whose ``scm_bus`` names the source-control bus kind and lane, and every
production port), runs it once and prints the one terminal as JSON on stdout.

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

from .adapters.adapter_agent_review_ports import PortNotBoundError
from .handlers.handler_agent_review_dispatch import HandlerAgentReviewDispatch
from .models import EnumAgentReviewOutcome, ModelAgentReviewRequest


def main(
    argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--command-id", default="")
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
    handler = HandlerAgentReviewDispatch(environ=environ)
    try:
        handler.bind()
    except (NodeOverlayError, PortNotBoundError) as exc:
        sys.stderr.write(f"agent review: not bound on this host: {exc}\n")
        return 1
    terminal = asyncio.run(handler.handle(request))
    sys.stdout.write(terminal.model_dump_json() + "\n")
    return 0 if terminal.outcome is EnumAgentReviewOutcome.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
