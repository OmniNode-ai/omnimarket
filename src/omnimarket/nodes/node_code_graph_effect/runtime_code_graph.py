# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_code_graph_effect <verb>``.

``nightly``  build the graph of every declared repository and append the row (what the timer runs).
``probe``    the same build and verdict, appending nothing.
``check``    ``--repo R --base REF --head REF``: name the modules the head adds that no root reaches.
``shadow``   check every open pull request of the overlay's shadow repositories, append one row, hold nothing.
``schedule`` install | uninstall | status | run-now, for a host in the overlay; ``--unit shadow`` is the shadow's timer.

``--fault`` on nightly, probe or shadow injects a declared fault; the row then carries ``drill=yes``.

Exit codes: 0 the run was built and written (a NOT WORKING verdict is still 0: a reading changes
nothing and refuses nothing), or the check found nothing; 1 the run failed (an unreadable
repository, an append that did not land), or the check flagged a module or an allowlist entry
past its date (the shadow verb exits 0 whatever it would refuse: it holds nothing); 3 a deployment fact is
missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.models.model_code_graph import (
    EnumGraphFault,
    EnumShadowOutcome,
    ModelCodeGraphCommand,
    ModelCodeGraphResult,
    ModelShadowCommand,
)
from omnimarket.nodes.node_code_graph_effect.adapters.pull_request_sources import (
    WatcherStatePullRequestSource,
)
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import GitRepoSource
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_run import (
    CodeGraphRun,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    GraphRules,
    check_added,
)
from omnimarket.nodes.node_code_graph_effect.handlers.schedule import (
    CodeGraphScheduler,
    load_schedule,
    state_directory,
)
from omnimarket.nodes.node_code_graph_effect.handlers.shadow_check import (
    CodeGraphShadowCheck,
)
from omnimarket.nodes.node_code_graph_effect.overlay import (
    NODE_NAME,
    ModelCodeGraphOverlay,
    load_overlay,
)
from omnimarket.nodes.node_code_graph_effect.protocols import (
    PullRequestSourceError,
    RepoSourceError,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)

LATEST_RESULT = "code-graph-latest.json"
#: The contract block each ``schedule --unit`` names.
SCHEDULE_BLOCKS = {
    "nightly": "schedule",
    "shadow": "shadow_schedule",
    "drill": "drill_schedule",
}


def _source(overlay: ModelCodeGraphOverlay) -> GitRepoSource:
    if overlay.graph_backend != "git":
        raise ValueError(
            "graph_backend in_memory is the test backend; a deployment reads git"
        )
    root = os.environ.get(overlay.repos_root_env, "").strip()
    if not root:
        raise ValueError(
            f"{overlay.repos_root_env} is not set; the overlay names it as the clones' root"
        )
    return GitRepoSource(Path(root), max_file_bytes=overlay.graph_roots.max_file_bytes)


def _keep(result: ModelCodeGraphResult) -> None:
    directory = state_directory(dict(os.environ))
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / LATEST_RESULT
    staged = target.with_suffix(".tmp")
    staged.write_text(result.model_dump_json(indent=1), encoding="utf-8")
    staged.replace(target)


def build_handler(write: bool) -> tuple[CodeGraphRun, str]:
    """The nightly handler as the overlay and the environment name it, and this host's name."""
    overlay = load_overlay()
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    handler = CodeGraphRun(
        overlay=overlay,
        host=host,
        source=_source(overlay),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )
    return handler, host


def _run(arguments: argparse.Namespace, write: bool) -> int:
    handler, host = build_handler(write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelCodeGraphCommand(
            correlation_id=f"code-graph-{host}-{now}",
            write=write,
            fault=EnumGraphFault(arguments.fault),
        )
    )
    _keep(result)
    for repo in result.repos:
        sys.stdout.write(
            f"REPO {repo.repo}@{repo.head[:12]} modules={repo.modules} tests={repo.test_modules} "
            f"roots={repo.roots} reachable={repo.reachable} unreachable={len(repo.unreachable)} "
            f"test_only={len(repo.test_only)}\n"
        )
    sample = result.sample
    sys.stdout.write(
        f"RUN host={host} status={result.status} verdict={result.verdict.value} "
        f"outcome={result.outcome.value} fault={result.fault.value} "
        f"sample={f'{sample.agree}/{sample.total}' if sample else 'none'} "
        f"controls={f'{sample.controls_reached}/{sample.controls_total}' if sample else 'none'} "
        f"ledger={result.ledger} key={result.row_key} reason={','.join(result.reasons[:8])}\n"
    )
    if result.failure:
        sys.stderr.write(f"CODE-GRAPH-FAILED {result.failure}\n")
    return 1 if result.status == "failed" else 0


def _check(arguments: argparse.Namespace) -> int:
    overlay = load_overlay()
    rules = GraphRules(roots=overlay.graph_roots)
    source = _source(overlay)
    base = source.snapshot(arguments.repo, arguments.base, rules.wanted)
    head = source.snapshot(arguments.repo, arguments.head, rules.wanted)
    result = check_added(
        base,
        head,
        rules,
        allowlist=overlay.allowlist,
        today=datetime.now(UTC).date(),
    )
    for path in result.flagged:
        sys.stdout.write(f"UNREACHABLE-ADDED {result.repo} {path}\n")
    for path in result.allowed:
        sys.stdout.write(f"ALLOWED {result.repo} {path}\n")
    for entry in result.expired:
        sys.stdout.write(f"ALLOWLIST-EXPIRED {result.repo} {entry}\n")
    sys.stdout.write(
        f"CHECK repo={result.repo} base={result.base[:12]} head={result.head[:12]} "
        f"added={result.added} flagged={len(result.flagged)} "
        f"verdict={'pass' if result.passed else 'fail'} "
        f"allowed={len(result.allowed)} expired={len(result.expired)}\n"
    )
    return 0 if result.passed else 1


def build_shadow_handler(write: bool) -> tuple[CodeGraphShadowCheck, str]:
    """The shadow handler as the overlay and the environment name it, and this host's name."""
    overlay = load_overlay()
    shadow = overlay.shadow
    if shadow is None:
        raise ValueError("the overlay declares no shadow")
    state = os.environ.get(shadow.pr_state_env, "").strip()
    if not state:
        raise ValueError(
            f"{shadow.pr_state_env} is not set; the overlay names it as the pull request watcher's state file"
        )
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    handler = CodeGraphShadowCheck(
        overlay=overlay,
        host=host,
        source=_source(overlay),
        pull_requests=WatcherStatePullRequestSource(
            Path(state),
            max_age=timedelta(seconds=shadow.shadow_max_age_seconds),
            clock=lambda: datetime.now(UTC),
        ),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )
    return handler, host


def _shadow(arguments: argparse.Namespace) -> int:
    write = not arguments.probe
    handler, host = build_shadow_handler(write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelShadowCommand(
            correlation_id=f"code-graph-shadow-{host}-{now}",
            write=write,
            fault=EnumGraphFault(arguments.fault),
        )
    )
    for verdict in result.verdicts:
        sys.stdout.write(
            f"SHADOW {verdict.repo}#{verdict.number}@{verdict.head[:12]} "
            f"outcome={verdict.outcome.value} added={verdict.added} flagged={len(verdict.flagged)} "
            f"expired={len(verdict.expired)} reason={verdict.reason or 'none'}\n"
        )
        for path in verdict.flagged:
            sys.stdout.write(f"WOULD-REFUSE {verdict.repo}#{verdict.number} {path}\n")
    sys.stdout.write(
        f"RUN host={host} status={result.status} fault={result.fault.value} "
        f"checked={len(result.verdicts)} "
        f"would_refuse={result.count(EnumShadowOutcome.WOULD_REFUSE)} "
        f"would_pass={result.count(EnumShadowOutcome.WOULD_PASS)} "
        f"indeterminate={result.count(EnumShadowOutcome.INDETERMINATE)} "
        f"holds_merge={'yes' if result.holds_merge else 'no'} "
        f"ledger={result.ledger} key={result.row_key}\n"
    )
    if result.failure:
        sys.stderr.write(f"CODE-GRAPH-SHADOW-FAILED {result.failure}\n")
    return 1 if result.status == "failed" else 0


def _schedule(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb in ("install", "uninstall", "status", "run-now"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        sub.add_argument(
            "--unit",
            choices=sorted(SCHEDULE_BLOCKS),
            default="nightly",
            help="the nightly graph (default), the pull request check in shadow, or the scheduled drill",
        )
        if verb == "install":
            sub.add_argument(
                "--workspace-root",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    scheduler = CodeGraphScheduler(
        schedule=load_schedule(block=SCHEDULE_BLOCKS[args.unit]),
        overlay=load_overlay(),
        home=Path.home(),
    )
    if args.verb == "install":
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
        verbs = parser.add_subparsers(dest="verb", required=True)
        for verb in ("nightly", "probe"):
            sub = verbs.add_parser(verb)
            sub.add_argument(
                "--fault",
                choices=[fault.value for fault in EnumGraphFault],
                default=EnumGraphFault.NONE.value,
            )
        shadow = verbs.add_parser("shadow")
        shadow.add_argument("--probe", action="store_true")
        shadow.add_argument(
            "--fault",
            choices=[fault.value for fault in EnumGraphFault],
            default=EnumGraphFault.NONE.value,
        )
        check = verbs.add_parser("check")
        check.add_argument("--repo", required=True)
        check.add_argument("--base", required=True)
        check.add_argument("--head", required=True)
        args = parser.parse_args(arguments)
        if args.verb == "check":
            return _check(args)
        if args.verb == "shadow":
            return _shadow(args)
        return _run(args, write=args.verb == "nightly")
    except (
        NodeOverlayError,
        LedgerRouteError,
        RepoSourceError,
        PullRequestSourceError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
