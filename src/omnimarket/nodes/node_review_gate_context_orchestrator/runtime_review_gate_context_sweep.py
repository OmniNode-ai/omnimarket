# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_review_gate_context_orchestrator.runtime_review_gate_context_sweep <verb>``.

``sweep``  read the open pull requests and the recorded review verdicts, decide each head's
           review-gate context check and post it.
``probe``  the same reads and decisions, posting nothing; the lines say what would be posted.

Exit codes: 0 every head the sweep read was decided and every post succeeded; 1 a source could
not be read (the sweep read nothing and posted nothing) or a post failed; 3 a deployment fact is
missing or the watcher state is not an object (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from omnibase_infra.cli.delegate_lane import (
    DelegateLaneSelectionError,
    resolve_lane_selection_at,
)
from pydantic import ValidationError

from omnimarket.handlers.delegation_scenario_runtime import write_atomic
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
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
from omnimarket.nodes.node_review_gate_context_orchestrator.protocols.protocol_review_gate_sources import (
    ReviewGateSourceError,
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
    verb = args.verb
    if verb is None:
        if overlay.schedule is None:
            raise ValueError("review-gate context overlay has no schedule")
        if not isinstance(overlay.ledger_read, ModelCommandLedgerReadRoute):
            raise ValueError(
                "scheduled review-gate probe requires a ledger read command"
            )
        verb = overlay.schedule.mode
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
        ModelReviewGateSweepCommand(correlation_id=uuid4().hex, post=verb == "sweep")
    )
    if args.state_dir is not None:
        reading = {
            "ran_at": datetime.now(UTC).isoformat(),
            "ok": result.ok,
            "unread": result.unread,
            "heads": len(result.entries),
            "posted": result.posted,
        }
        write_atomic(args.state_dir / "last-run.json", json.dumps(reading) + "\n")
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
    parser.add_argument("verb", nargs="?", choices=("sweep", "probe"))
    parser.add_argument(
        "--omni-home",
        dest="registry_root",
        type=Path,
        help="Registry root supplied by the installer",
    )
    parser.add_argument(
        "--state-dir", type=Path, help="Directory for the scheduled run receipt"
    )
    parser.add_argument(
        "--check-ledger-route",
        action="store_true",
        help="Check the installed ledger lane declaration before loading the unit",
    )
    args = parser.parse_args(argv)
    if (args.registry_root is None) != (args.state_dir is None) or (
        args.verb is None and args.state_dir is None
    ):
        parser.error("supply a verb or both registry unit arguments")
    if args.verb is not None and args.state_dir is not None:
        parser.error("the registry unit takes its mode from the overlay schedule")
    if args.check_ledger_route and args.verb is not None:
        parser.error("--check-ledger-route requires the registry unit arguments")
    try:
        if args.check_ledger_route:
            # The installer sets OMNIBASE_PATH to its registry root. Resolve the
            # declaration as ledger query does, without opening a bus connection.
            resolve_lane_selection_at("dev", args.registry_root)
            return 0
        return _run(args)
    except (
        DelegateLaneSelectionError,
        NodeOverlayError,
        LedgerRouteError,
        ReviewGateSourceError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
