# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_review_gate_context_orchestrator <verb>``.

``sweep``  read the open pull requests and the recorded review verdicts, decide each head's
           review-gate context check and post it.
``probe``  the same reads and decisions, posting nothing; the lines say what would be posted.

Exit codes: 0 every head the sweep read was decided and every post succeeded; 1 a source could
not be read (the sweep read nothing and posted nothing) or a post failed; 3 a deployment fact is
missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)
from omnimarket.nodes.node_review_gate_context_compute.handlers.handler_review_gate_context import (
    HandlerReviewGateContext,
)
from omnimarket.nodes.node_review_gate_context_compute.overlay import (
    load_overlay as load_policy_overlay,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.adapters.ledger_review_verdicts import (
    LedgerReviewVerdicts,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.adapters.watcher_open_pull_requests import (
    WatcherOpenPullRequests,
    load_watcher_state,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.handlers.handler_review_gate_context_orchestrator import (
    HandlerReviewGateContextOrchestrator,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.models.model_review_gate_sweep import (
    ModelReviewGateSweepCommand,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.overlay import (
    NODE_NAME,
    load_overlay,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_review_gate_context_effect.handlers.handler_review_gate_context_effect import (
        HandlerReviewGateContextEffect,
    )


def _poster(environ: Mapping[str, str]) -> HandlerReviewGateContextEffect:
    """The effect node's poster, wired from its overlay.

    The effect node's modules are imported here and nowhere else at module scope, so this file
    imports nothing of the effect until a sweep runs.
    """
    from omnimarket.handlers.emission_sink import (
        InMemoryEmissionSink,
        JsonlEmissionSink,
    )
    from omnimarket.nodes.node_lab_check_run_effect.overlay import ModelJournalEmission
    from omnimarket.nodes.node_review_gate_context_effect.handlers.handler_review_gate_context_effect import (
        HandlerReviewGateContextEffect,
    )
    from omnimarket.nodes.node_review_gate_context_effect.overlay import (
        build_ports,
    )
    from omnimarket.nodes.node_review_gate_context_effect.overlay import (
        load_overlay as load_effect_overlay,
    )

    effect = load_effect_overlay(environ)
    reader, poster = build_ports(effect, environ)
    emission = effect.emission
    sink = (
        JsonlEmissionSink(emission.journal())
        if isinstance(emission, ModelJournalEmission)
        else InMemoryEmissionSink()
    )
    return HandlerReviewGateContextEffect(
        reader=reader,
        poster=poster,
        sink=sink,
        emission_node=emission.node,
        emission_piece=emission.piece,
        emission_backend=emission.backend,
        route_backend=effect.route.backend,
        clock=lambda: datetime.now(UTC),
    )


def _watcher_state_path(environ: Mapping[str, str], env_name: str) -> Path:
    """The PR watcher's state file path, read from the environment variable the overlay names."""
    value = environ.get(env_name, "").strip()
    if not value:
        raise ValueError(
            f"{env_name} is not set; the overlay names it as the watcher state path"
        )
    return Path(value)


def _run(args: argparse.Namespace) -> int:
    environ = dict(os.environ)
    overlay = load_overlay(environ)
    policy = load_policy_overlay(environ).policy
    settings = overlay.settings
    state = load_watcher_state(
        _watcher_state_path(environ, settings.watcher_state_path_env)
    )
    handler = HandlerReviewGateContextOrchestrator(
        open_prs=WatcherOpenPullRequests(state, settings, datetime.now(UTC)),
        verdicts=LedgerReviewVerdicts(
            build_row_source(overlay.ledger_read, environ), settings
        ),
        decide=HandlerReviewGateContext(),
        poster=_poster(environ),
        policy=policy,
        clock=lambda: datetime.now(UTC),
    )
    result = handler.handle(
        ModelReviewGateSweepCommand(
            correlation_id=uuid4().hex, post=args.verb == "sweep"
        )
    )
    for entry in result.entries:
        sys.stdout.write(
            f"{entry.repository}#{entry.pr_number} {entry.state.value} "
            f"{entry.reason.value} {entry.outcome}\n"
        )
    sys.stdout.write(
        f"summary ok={str(result.ok).lower()} posted={result.posted} "
        f"unchanged={result.unchanged} failed={result.failed}\n"
    )
    if result.unread is not None:
        sys.stderr.write(f"unread: {result.unread}\n")
        return 1
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("sweep", "probe"))
    try:
        return _run(parser.parse_args(list(sys.argv[1:] if argv is None else argv)))
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
