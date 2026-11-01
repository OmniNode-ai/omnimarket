# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ask the WIP admission from a process that is not Python.

``python -m omnimarket.nodes.node_wip_admission_compute.handlers.cli`` reads one JSON
object on standard input and prints the decision as one JSON object. Input keys: ``kind``
(new_work, fix_forward, ticket_create), ``repo``, ``ticket``, ``now`` (ISO 8601),
``pr_state`` (the PR watcher state), ``claims`` (``[{lane, ticket, repo}]`` for live CLAIMs),
``ticket_states`` (ticket -> state name) and, for a fix-forward, ``fix``. The policy comes
from the ``wip_admission`` overlay. Exit 0 admits, 3 refuses, 2 is a request or overlay that
cannot be read (stderr names why); the caller decides what an unreadable overlay means.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from datetime import datetime

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.rules_wip_admission import build_wip_read
from omnimarket.handlers.wip_admission_overlay import (
    load_wip_admission_config,
    load_wip_admission_policy,
)
from omnimarket.models.wip_admission import (
    EnumWipOutcome,
    ModelWipAdmissionRequest,
    ModelWipClaim,
)
from omnimarket.nodes.node_wip_admission_compute.container.container_wip_admission_compute import (
    create_handler_wip_admission,
)

EXIT_ADMIT, EXIT_UNREADABLE, EXIT_REFUSE = 0, 2, 3


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    try:
        raw = json.loads(sys.stdin.read())
        config = load_wip_admission_config()
        policy = load_wip_admission_policy()
        read = build_wip_read(
            pr_state=raw.get("pr_state"),
            claims=[ModelWipClaim.model_validate(c) for c in raw.get("claims", [])],
            ticket_states=raw.get("ticket_states", {}),
            config=config,
        )
        request = ModelWipAdmissionRequest(
            kind=raw["kind"],
            repo=raw.get("repo", ""),
            ticket=raw.get("ticket", ""),
            now=datetime.fromisoformat(str(raw["now"]).replace("Z", "+00:00")),
            read=read,
            policy=policy,
            config=config,
            fix=raw.get("fix"),
        )
    except (ValueError, KeyError, TypeError, ValidationError, NodeOverlayError) as exc:
        sys.stderr.write(f"wip admission: unreadable request or overlay: {exc}\n")
        return EXIT_UNREADABLE
    decision = create_handler_wip_admission().handle(request)
    sys.stdout.write(decision.model_dump_json() + "\n")
    return EXIT_ADMIT if decision.outcome is EnumWipOutcome.ADMIT else EXIT_REFUSE


if __name__ == "__main__":
    raise SystemExit(main())
