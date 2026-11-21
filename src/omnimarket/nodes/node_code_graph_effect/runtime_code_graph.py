# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_code_graph_effect <verb>``.

``nightly``  build the graph of every declared repository and append the row (what the timer runs).
``probe``    the same build and verdict, appending nothing.
``index``    ``[--repo R ...]``: write the version of each named declared repository (every one by default) into Memgraph; no row, no verdict.
``check``    ``--repo R --base REF --head REF``: name the modules the head adds that no root reaches.
``shadow``   check every open pull request of the overlay's shadow repositories, append one row, hold nothing.
``lookup``   ``--question Q --repo R`` and the question's argument (locate: ``--target``, a node name, contract path or workflow path): one MATCH row per answer, then a LOOKUP line
             carrying the answered commit's time and the read time. ``--host H`` reads the overlay from the
             overlays clone the lab host table declares for H; ``--retention-versions N`` is the caller's
             lookup block where the overlay carries none.
``schedule`` install | uninstall | status | run-now, for a host in the overlay; ``--unit shadow`` is the shadow's timer.

``--fault`` on nightly, probe or shadow injects a declared fault; the row then carries ``drill=yes``.

Exit codes: 0 the run was built and written (a NOT WORKING verdict is still 0: a reading changes
nothing and refuses nothing), or the check found nothing; 1 the run failed (an unreadable
repository, an append that did not land), or the check flagged a module or an allowlist entry
past its date (the shadow verb exits 0 whatever it would refuse: it holds nothing); 3 a deployment fact is
missing (INDETERMINATE). The lookup exits 0 on an answer (found or a clean not-found) and 1 on a
lookup that could not be answered.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.handlers.arch_graph_bolt_host_overlay import (
    resolve_arch_graph_bolt_uri,
)
from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_FILE_NAME,
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
    find_node_overlay,
)
from omnimarket.handlers.node_overlay_resolved_roots import (
    REGISTRY_ROOT_ENVS,
    registry_root_from,
)
from omnimarket.handlers.rules_overlay_root_declared import (
    HOST_TABLE_OVERLAY,
    OverlayRootRefusedError,
    declared_overlay_root,
    refuse_undeclared_overlay_root,
)
from omnimarket.models.model_code_graph import (
    EnumGraphFault,
    EnumShadowOutcome,
    ModelCodeGraphCommand,
    ModelCodeGraphResult,
    ModelShadowCommand,
)
from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    EnumRemovalKind,
    ModelCodeGraphLookupCommand,
    ModelCodeGraphLookupResult,
    ModelCodeGraphLookupSettings,
)
from omnimarket.nodes.node_code_graph_effect.adapters.memgraph_version_store import (
    DEFAULT_RETENTION_VERSIONS,
    MemgraphQueryError,
    MemgraphUnreachableError,
    MemgraphVersionStore,
    open_memgraph_store,
    revision_key,
)
from omnimarket.nodes.node_code_graph_effect.adapters.pull_request_sources import (
    WatcherStatePullRequestSource,
)
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import GitRepoSource
from omnimarket.nodes.node_code_graph_effect.adapters.version_stores import (
    InMemoryVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_index import (
    build_version,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup import (
    CodeGraphLookup,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup_routed import (
    MemgraphFirstLookup,
)
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


def _source(
    overlay: ModelCodeGraphOverlay, environ: Mapping[str, str] | None = None
) -> GitRepoSource:
    if overlay.graph_backend != "git":
        raise ValueError(
            "graph_backend in_memory is the test backend; a deployment reads git"
        )
    env = os.environ if environ is None else environ
    root = env.get(overlay.repos_root_env, "").strip() or registry_root_from(env)
    if not root:
        raise ValueError(
            f"{overlay.repos_root_env} is not set; the overlay names it as the clones' root, "
            f"and neither {' nor '.join(REGISTRY_ROOT_ENVS)} names the registry root"
        )
    return GitRepoSource(Path(root), max_file_bytes=overlay.graph_roots.max_file_bytes)


#: The versions this process holds. The persistent, merge-refreshed projection replaces it (OMN-23648, later slice).
_VERSIONS: dict[int, InMemoryVersionStore] = {}


def declared_lookup_roots(
    host: str, omni_root: Path, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment with the overlays clone the lab host table declares for ``host`` read first.

    The table is the first ``lab_hosts/overlay.yaml`` under the roots in ``ONEX_SKILL_OVERLAY_ROOTS``,
    then under the two registry-relative places the table itself declares clones at
    (``$OMNI_HOME/overlays`` and ``$OMNI_HOME/../overlays``). Refuses with
    :class:`OverlayRootRefusedError` when no table is found, the table cannot be read or is not a
    mapping, the table declares no overlays clone for the host, the declared clone holds no overlay of
    this node, or the host the overlay names in its ``host_env`` is set and is not ``host``.
    """
    env = dict(os.environ if environ is None else environ)
    omni_root = omni_root.resolve()
    roots = [
        Path(root) for root in env.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep) if root
    ]
    candidates = [
        *roots,
        omni_root / "overlays" / "overlays",
        omni_root.parent / "overlays" / "overlays",
    ]
    table = next(
        (
            root / HOST_TABLE_OVERLAY / OVERLAY_FILE_NAME
            for root in candidates
            if (root / HOST_TABLE_OVERLAY / OVERLAY_FILE_NAME).is_file()
        ),
        None,
    )
    if table is None:
        raise OverlayRootRefusedError(
            f"{host}: no {HOST_TABLE_OVERLAY}/{OVERLAY_FILE_NAME} under "
            + ", ".join(str(root) for root in candidates)
        )
    try:
        document = yaml.safe_load(table.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OverlayRootRefusedError(
            f"{host}: {table} cannot be read as YAML: {' '.join(str(exc).split())}"
        ) from None
    if not isinstance(document, dict):
        raise OverlayRootRefusedError(f"{host}: {table} is not a mapping")
    declared = declared_overlay_root(document, host, omni_root)
    if not (declared / NODE_NAME / OVERLAY_FILE_NAME).is_file():
        raise OverlayRootRefusedError(
            f"{host}: the declared overlays clone {declared} holds no {NODE_NAME}/{OVERLAY_FILE_NAME}"
        )
    env[OVERLAY_ROOTS_ENV] = os.pathsep.join(
        [str(declared), *(str(root) for root in roots)]
    )
    running = env.get(load_overlay(env).ledger_append.host_env, "").strip()
    if running and running != host:
        raise OverlayRootRefusedError(
            f"{host}: this process runs as host {running}, so the overlays clone declared for {host} "
            f"is not the one on this host"
        )
    return env


def answer_lookup(
    command: ModelCodeGraphLookupCommand,
    *,
    environ: Mapping[str, str] | None = None,
    settings: ModelCodeGraphLookupSettings | None = None,
) -> ModelCodeGraphLookupResult:
    """Answer a lookup from Memgraph by Cypher; from the file store only when Memgraph is unreachable.

    The fallback answer carries the typed reason (``EnumLookupFallbackReason``) it fell back for.
    ``environ`` and ``settings`` reach the file store's overlay read, as in ``answer_lookup_from_files``.
    """
    return MemgraphFirstLookup(
        open_store=lambda: _open_memgraph_store(None),
        fallback=lambda fallen: answer_lookup_from_files(
            fallen, environ=environ, settings=settings
        ),
    ).handle(command)


def answer_lookup_from_files(
    command: ModelCodeGraphLookupCommand,
    *,
    environ: Mapping[str, str] | None = None,
    settings: ModelCodeGraphLookupSettings | None = None,
) -> ModelCodeGraphLookupResult:
    """Answer a lookup from the overlay's versions, building the head version of a repository on first ask.

    ``environ`` is where the overlay is found (default: the process environment). ``settings`` is
    the caller's lookup block, used only where the overlay carries none. A lookup for a repository the overlay
    does not declare, or with no lookup block from either, or a repository that cannot be read, is a
    typed ``failed`` result and never an empty answer.
    """

    def failed(reason: str) -> ModelCodeGraphLookupResult:
        return ModelCodeGraphLookupResult(
            correlation_id=command.correlation_id,
            question=command.question,
            repo=command.repo,
            status="failed",
            reason=reason,
        )

    overlay = load_overlay(environ)
    lookup = overlay.lookup if overlay.lookup is not None else settings
    if lookup is None:
        return failed("the overlay has no lookup block")
    declared = {repo.name: repo for repo in overlay.repos}
    if command.repo not in declared:
        return failed(f"the overlay does not declare {command.repo}")
    source = _source(overlay)
    store = _VERSIONS.setdefault(
        lookup.retention_versions,
        InMemoryVersionStore(lookup.retention_versions),
    )
    if store.newest(command.repo) is None:
        try:
            snapshot = source.snapshot(
                command.repo,
                declared[command.repo].ref,
                GraphRules(roots=overlay.graph_roots).wanted,
            )
        except RepoSourceError as exc:
            return failed(f"repository unreadable: {exc}")
        store.put(
            build_version(
                snapshot,
                GraphRules(roots=overlay.graph_roots),
                committed_at=source.committed_at(command.repo, snapshot.head),
            )
        )
    return CodeGraphLookup(store, source).handle(command)


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
    source = _source(overlay)
    handler = CodeGraphRun(
        overlay=overlay,
        host=host,
        source=source,
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
        version_writer=_memgraph_writer(overlay) if write else None,
        commit_clock=source,
    )
    return handler, host


def _open_memgraph_store(overlay: ModelCodeGraphOverlay | None) -> MemgraphVersionStore:
    retention = (
        overlay.lookup.retention_versions
        if overlay is not None and overlay.lookup is not None
        else DEFAULT_RETENTION_VERSIONS
    )
    return open_memgraph_store(
        resolve_uri=resolve_arch_graph_bolt_uri, retention_versions=retention
    )


def _memgraph_writer(overlay: ModelCodeGraphOverlay) -> MemgraphVersionStore | None:
    """The nightly's Memgraph writer, or none where no Bolt address resolves (the file reading still runs)."""
    try:
        return _open_memgraph_store(overlay)
    except MemgraphUnreachableError as exc:
        sys.stderr.write(
            f"CODE-GRAPH-MEMGRAPH-SKIPPED {exc.reason.value}: {exc.detail}\n"
        )
        return None


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
        f"ledger={result.ledger} key={result.row_key} memgraph={result.memgraph} "
        f"reason={','.join(result.reasons[:8])}\n"
    )
    if result.failure:
        sys.stderr.write(f"CODE-GRAPH-FAILED {result.failure}\n")
    return 1 if result.status == "failed" else 0


def _index(arguments: argparse.Namespace) -> int:
    """Write the named repositories' versions into Memgraph, one revision per repository head.

    It builds no verdict and appends no ledger row, so one repository whose clone a host lacks
    names itself and never keeps the others out of the graph.
    """
    overlay = load_overlay()
    rules = GraphRules(roots=overlay.graph_roots)
    source = _source(overlay)
    declared = {repo.name: repo for repo in overlay.repos}
    names = list(arguments.repo or declared)
    unknown = [name for name in names if name not in declared]
    if unknown:
        sys.stderr.write(
            f"CODE-GRAPH-INDEX-FAILED the overlay does not declare {unknown}\n"
        )
        return 1
    try:
        store = _open_memgraph_store(overlay)
    except MemgraphUnreachableError as exc:
        sys.stderr.write(f"CODE-GRAPH-INDEX-FAILED {exc.reason.value}: {exc.detail}\n")
        return 1
    failed = 0
    try:
        for name in names:
            try:
                snapshot = source.snapshot(name, declared[name].ref, rules.wanted)
                version = build_version(
                    snapshot,
                    rules,
                    committed_at=source.committed_at(name, snapshot.head),
                )
                written, pruned = store.put(version)
            except (
                RepoSourceError,
                MemgraphUnreachableError,
                MemgraphQueryError,
            ) as exc:
                failed += 1
                sys.stdout.write(f"INDEX-FAILED {name} {type(exc).__name__}: {exc}\n")
                continue
            modules = len(
                {d.path for d in version.definitions}
                | {i.importer_path for i in version.imports}
                | {i.imported_path for i in version.imports}
            )
            sys.stdout.write(
                f"INDEX {name}@{snapshot.head[:12]} revision={revision_key(name, snapshot.head)} "
                f"written={'yes' if written else 'already'} modules={modules} "
                f"symbols={len(version.definitions)} imports={len(version.imports)} "
                f"pruned={len(pruned)}\n"
            )
    finally:
        store.close()
    return 1 if failed else 0


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


def _utc_stamp(moment: datetime) -> str:
    """``moment`` in UTC as ``YYYY-MM-DDTHH:MM:SSZ``; a time with no offset is read as UTC."""
    aware = (
        moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    )
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def _answered_commit_time(result: ModelCodeGraphLookupResult) -> str:
    """The time of the commit the answer came from; ``none`` when nothing answered or the clone gave none."""
    if result.answered_from is None:
        return "none"
    for store in _VERSIONS.values():
        version = store.get(result.repo, result.answered_from)
        if version is not None:
            return (
                "none"
                if version.committed_at is None
                else _utc_stamp(version.committed_at)
            )
    return "none"


def _lookup(arguments: argparse.Namespace) -> int:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    environ: dict[str, str] | None = None
    if arguments.host:
        if not arguments.workspace_root:
            raise ValueError("OMNI_HOME is not set and --workspace-root was not passed")
        try:
            environ = declared_lookup_roots(
                arguments.host, Path(arguments.workspace_root)
            )
        except OverlayRootRefusedError as exc:
            sys.stderr.write(f"{exc}\n")
            return 3
    settings = (
        ModelCodeGraphLookupSettings(retention_versions=arguments.retention_versions)
        if arguments.retention_versions is not None
        else None
    )
    result = answer_lookup(
        ModelCodeGraphLookupCommand(
            correlation_id=f"code-graph-lookup-{now}",
            question=EnumCodeGraphQuestion(arguments.question),
            repo=arguments.repo,
            symbol=arguments.symbol,
            path=arguments.path,
            paths=tuple(arguments.paths),
            archetype=arguments.archetype,
            hint=arguments.hint,
            removal_kind=EnumRemovalKind(arguments.kind) if arguments.kind else None,
            identity=arguments.identity,
            star_export_list=(
                tuple(arguments.star_export) if arguments.star_export else None
            ),
            head_all_lists_name=arguments.head_all_lists,
            rebound_submodules=tuple(arguments.rebound_submodule),
            target=arguments.target,
            as_of=arguments.as_of,
        ),
        environ=environ,
        settings=settings,
    )
    for match in result.matches:
        cells = [
            f"{key}={value}"
            for key in ("name", "line", "detail")
            if (value := getattr(match, key)) is not None
        ]
        sys.stdout.write(" ".join(["MATCH", match.path, *cells]) + "\n")
    nearest = result.nearest.commit[:12] if result.nearest else "none"
    read_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stdout.write(
        f"LOOKUP question={result.question.value} repo={result.repo} status={result.status} "
        f"answered_from={(result.answered_from or 'none')[:12]} committed_at={_answered_commit_time(result)} "
        f"read_at={read_at} matches={len(result.matches)} "
        f"nearest={nearest} served_from={result.served_from.value} "
        f"fallback_reason={result.fallback_reason.value if result.fallback_reason else 'none'} "
        f"reason={result.reason or 'none'}\n"
    )
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
        try:
            refuse_undeclared_overlay_root(
                found.parent.parent,
                host=args.host,
                omni_root=Path(args.workspace_root),
            )
        except OverlayRootRefusedError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
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
        index = verbs.add_parser("index")
        index.add_argument(
            "--repo", action="append", help="a declared repository; default every one"
        )
        check = verbs.add_parser("check")
        check.add_argument("--repo", required=True)
        check.add_argument("--base", required=True)
        check.add_argument("--head", required=True)
        lookup = verbs.add_parser("lookup")
        lookup.add_argument(
            "--question",
            required=True,
            choices=[question.value for question in EnumCodeGraphQuestion],
        )
        lookup.add_argument("--repo", required=True)
        lookup.add_argument(
            "--symbol", help="definition and same-shape: the class or function name"
        )
        lookup.add_argument(
            "--path",
            help="importers: the repository-relative path of the imported file",
        )
        lookup.add_argument(
            "--paths", nargs="+", default=[], help="reach: the changed files"
        )
        lookup.add_argument(
            "--archetype", help="exemplar: the archetype (effect, compute, ...)"
        )
        lookup.add_argument("--hint", help="exemplar: words that rank the nodes")
        lookup.add_argument(
            "--kind",
            choices=[kind.value for kind in EnumRemovalKind],
            help="users: what the removal took out",
        )
        lookup.add_argument(
            "--identity", help="users: the dotted identity (a.mod.f, or a.mod)"
        )
        lookup.add_argument(
            "--star-export",
            nargs="+",
            default=[],
            help="users: the names the base revision's star import exposed",
        )
        lookup.add_argument(
            "--head-all-lists",
            action="store_true",
            help="users: the head's literal __all__ still lists the removed name",
        )
        lookup.add_argument(
            "--rebound-submodule",
            action="append",
            default=[],
            help="users: a dotted submodule whose name the package binds to another object",
        )
        lookup.add_argument(
            "--target",
            help="locate: a node name, a contract path or a workflow path",
        )
        lookup.add_argument(
            "--as-of",
            dest="as_of",
            help="the commit to answer as of; absent means the head",
        )
        lookup.add_argument(
            "--host",
            help="read the overlay from the overlays clone the lab host table declares for this host",
        )
        lookup.add_argument(
            "--workspace-root",
            dest="workspace_root",
            default=os.environ.get("OMNI_HOME", ""),
        )
        lookup.add_argument(
            "--retention-versions",
            dest="retention_versions",
            type=int,
            help="the caller's lookup block, used only where the overlay has no lookup block",
        )
        args = parser.parse_args(arguments)
        if args.verb == "index":
            return _index(args)
        if args.verb == "check":
            return _check(args)
        if args.verb == "lookup":
            return _lookup(args)
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
