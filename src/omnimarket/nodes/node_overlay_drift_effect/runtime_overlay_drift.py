# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_drift_effect <verb>``.

``daily``    read the overlay home against the runtime and append the row (what the schedule runs).
``probe``    the same read, appending nothing.
``schedule`` check | install | uninstall | status | run-now, for a host in the overlay.

Exit codes: 0 the read ran and its row was written (a reading of NOT WORKING is still 0: a reading
changes nothing and refuses nothing); 1 the read or its row failed (an unreadable home or runtime,
an append that did not land); 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.emission_sink import JsonlEmissionSink, ProtocolEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.nodes.node_overlay_drift_effect.adapters.overlay_home import (
    LocalOverlayHome,
)
from omnimarket.nodes.node_overlay_drift_effect.adapters.pin_history import (
    GitPinHistory,
)
from omnimarket.nodes.node_overlay_drift_effect.adapters.runtime_revision import (
    GitRevisionTree,
    ProvenanceRuntimeRevision,
)
from omnimarket.nodes.node_overlay_drift_effect.adapters.runtime_validator import (
    InterpreterRuntimeValidator,
    RevisionTreeRuntimeValidator,
)
from omnimarket.nodes.node_overlay_drift_effect.handlers.handler_overlay_drift import (
    HandlerOverlayDrift,
)
from omnimarket.nodes.node_overlay_drift_effect.handlers.schedule import (
    DriftScheduler,
    load_schedule,
)
from omnimarket.nodes.node_overlay_drift_effect.handlers.upstream_chain import (
    newest_unanswered_upstream_cid,
)
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayDriftCommand,
)
from omnimarket.nodes.node_overlay_drift_effect.overlay import (
    NODE_NAME,
    ModelOverlayDriftOverlay,
    load_overlay,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import (
    ProtocolRuntimeValidator,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
    build_row_source,
)


def _required(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the {NODE_NAME} overlay names it")
    return value


def build_handler(
    overlay: ModelOverlayDriftOverlay, environ: Mapping[str, str], write: bool
) -> tuple[HandlerOverlayDrift, str]:
    host = _required(overlay.ledger_append.host_env, environ)
    home = LocalOverlayHome(
        Path(_required(overlay.home.root_env, environ)), overlay.home
    )
    interpreter = InterpreterRuntimeValidator(
        Path(_required(overlay.runtime.python_env, environ)),
        overlay.runtime.timeout_seconds,
    )
    source = overlay.runtime.revision_source
    validator: ProtocolRuntimeValidator = (
        RevisionTreeRuntimeValidator(
            interpreter,
            ProvenanceRuntimeRevision(source, environ),
            GitRevisionTree(
                Path(_required(source.clone_env, environ)), source.git_timeout_seconds
            ),
        )
        if source
        else interpreter
    )
    emissions: ProtocolEmissionSink | None = (
        JsonlEmissionSink(Path(_required(overlay.emission.journal_env, environ)))
        if overlay.emission
        else None
    )
    handler = HandlerOverlayDrift(
        overlay=overlay,
        host=host,
        home=home,
        validator=validator,
        appender=build_appender(overlay.ledger_append, overlay.publish.lane, environ)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
        pin_history=GitPinHistory(
            Path(_required(overlay.pin_history.clone_env, environ)),
            overlay.pin_history.merged_ref,
            overlay.pin_history.timeout_seconds,
        )
        if overlay.pin_history
        else None,
        emissions=emissions,
    )
    return handler, host


def _upstream_cid(overlay: ModelOverlayDriftOverlay) -> str | None:
    """The upstream chain this run answers (join J3), or ``None`` when the overlay names no upstream or the ledger cannot say."""
    if overlay.upstream is None:
        return None
    return newest_unanswered_upstream_cid(
        build_row_source(overlay.upstream.ledger_read, os.environ),
        overlay.upstream,
        overlay.publish.lane,
        datetime.now(UTC),
    )


def _run(write: bool) -> int:
    overlay = load_overlay()
    handler, host = build_handler(overlay, os.environ, write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelOverlayDriftCommand(
            correlation_id=f"overlay-drift-{host}-{now}",
            write=write,
            upstream_cid=_upstream_cid(overlay),
        )
    )
    if result.status == "failed":
        sys.stderr.write(f"OVERLAY-DRIFT-FAILED {result.failure}\n")
        return 1
    counts = result.counts
    runtime = result.runtime
    sys.stdout.write(
        f"READING host={result.host} verdict={result.verdict.value} "
        f"outcome={result.outcome.value} checked={counts.checked} accepted={counts.accepted} "
        f"rejected={counts.rejected} model_missing={counts.model_missing} "
        f"unreadable={counts.unreadable} overlay_missing={counts.overlay_missing} "
        f"unpinned={counts.unpinned} pin_unmerged={counts.pin_unmerged} runtime_version={runtime.version if runtime else 'unknown'} "
        f"runtime_revision={runtime.revision[:12] if runtime else 'unknown'} "
        f"home_revision={result.home_revision[:12]} ledger={result.ledger} "
        f"key={result.row_key} reason={','.join(result.reasons)}\n"
    )
    for check in result.failing:
        sys.stdout.write(
            f"FAILING overlay={check.overlay_id} verdict={check.verdict.value} "
            f"fields={'+'.join(check.fields) or '-'} error={check.error_class or '-'}\n"
        )
    if result.unpinned:
        sys.stdout.write(f"UNPINNED {' '.join(result.unpinned)}\n")
    if result.unmerged_pins:
        sys.stdout.write(f"UNMERGED-PIN {' '.join(result.unmerged_pins)}\n")
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
    scheduler = DriftScheduler(
        schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
    )
    if args.verb == "check":
        result = scheduler.check()
    elif args.verb == "install":
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --workspace-root was not passed")
        found = find_node_overlay(NODE_NAME)
        if found is None:
            raise NodeOverlayError(
                "the overlay file cannot be found to pin its root in the unit"
            )
        result = scheduler.install(
            args.host,
            omni_root=Path(args.workspace_root),
            overlay_root=found.parent.parent,
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
