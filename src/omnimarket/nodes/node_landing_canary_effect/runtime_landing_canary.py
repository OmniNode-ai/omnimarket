# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_landing_canary_effect <verb>``.

``daily``     open the canary pull request, wait for it to land, append the piece-9 row (what the schedule runs).
``probe``     the same run, appending nothing.
``schedule``  check | install | uninstall | status | run-now, for a host in the overlay.

Exit codes: 0 the run finished and its row was written (NOT_WORKING is still 0: a reading changes nothing and
refuses nothing); 1 the run or its row failed (a pull request that could not be opened or read, an append that
did not land); 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    pinned_overlay_root,
)
from omnimarket.nodes.node_landing_canary_effect.adapters.canary_repo import (
    InMemoryCanaryRepo,
    ScmCanaryRepo,
)
from omnimarket.nodes.node_landing_canary_effect.handlers.handler_landing_canary import (
    HandlerLandingCanary,
)
from omnimarket.nodes.node_landing_canary_effect.handlers.schedule import (
    LandingCanaryScheduler,
    load_schedule,
)
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    ModelLandingCanaryCommand,
)
from omnimarket.nodes.node_landing_canary_effect.overlay import (
    NODE_NAME,
    ModelCanaryTarget,
    ModelLandingCanaryOverlay,
    load_overlay,
)
from omnimarket.nodes.node_landing_canary_effect.protocols import ProtocolCanaryRepo
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def _required(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the {NODE_NAME} overlay names it")
    return value


def _in_memory_repo(
    target: ModelCanaryTarget, environ: Mapping[str, str]
) -> ProtocolCanaryRepo:
    return InMemoryCanaryRepo(clock=lambda: datetime.now(UTC))


def _scm_repo(
    target: ModelCanaryTarget, environ: Mapping[str, str]
) -> ProtocolCanaryRepo:
    if target.scm is None:
        raise ValueError("the scm backend needs the overlay's target.scm block")
    return ScmCanaryRepo(target.scm, target.repo, environ)


#: The overlay's ``target.backend`` names the backend; this table is the only place it is resolved.
REPO_BACKENDS: dict[
    str, Callable[[ModelCanaryTarget, Mapping[str, str]], ProtocolCanaryRepo]
] = {"scm": _scm_repo, "inmemory": _in_memory_repo}


def build_handler(
    overlay: ModelLandingCanaryOverlay, environ: Mapping[str, str], write: bool
) -> tuple[HandlerLandingCanary, str]:
    host = _required(overlay.ledger_append.host_env, environ)
    handler = HandlerLandingCanary(
        overlay=overlay,
        host=host,
        repo=REPO_BACKENDS[overlay.target.backend](overlay.target, environ),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane, environ)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
        sleep=time.sleep,
    )
    return handler, host


def _run(write: bool) -> int:
    overlay = load_overlay()
    handler, host = build_handler(overlay, os.environ, write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelLandingCanaryCommand(
            correlation_id=f"landing-canary-{host}-{now}", write=write
        )
    )
    if result.status == "failed":
        sys.stderr.write(f"LANDING-CANARY-FAILED {result.failure}\n")
        return 1
    sys.stdout.write(
        f"READING host={result.host} backend={overlay.target.backend} verdict={result.verdict.value} "
        f"outcome={result.outcome.value} ending={result.ending.value if result.ending else 'none'} "
        f"repo={result.repo} pr={result.pr_number} elapsed_s={result.elapsed_seconds} "
        f"bound_s={result.bound_seconds} ledger={result.ledger} key={result.row_key} "
        f"reason={','.join(result.reasons)}\n"
    )
    return 1 if result.failed else 0


def _schedule(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("check")
    for verb in ("install", "uninstall", "status", "run-now"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument(
                "--workspace-root",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    scheduler = LandingCanaryScheduler(
        schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
    )
    if args.verb == "check":
        result = scheduler.check()
    elif args.verb == "install":
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --workspace-root was not passed")
        result = scheduler.install(
            args.host,
            omni_root=Path(args.workspace_root),
            overlay_root=pinned_overlay_root(NODE_NAME),
            dry_run=args.dry_run,
        )
    elif args.verb == "uninstall":
        result = scheduler.uninstall(args.host)
    elif args.verb == "status":
        result = scheduler.status(args.host)
    else:
        result = scheduler.run_now(args.host)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "schedule":
            return _schedule(arguments[1:])
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("daily", "probe"))
        args = parser.parse_args(arguments)
        return _run(write=args.verb == "daily")
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
