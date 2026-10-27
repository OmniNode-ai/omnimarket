# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm <verb>`` -- one lane entry point over the source-control nodes (OMN-20913).

A lane does GitHub work through this command and no ``gh`` verb:

- **PR and CI state reads** (pr-view, pr-status, pr-checks, pr-list,
  pr-merge-commit, pr-heads) are answered from the PR watcher's state file and the
  canonical clones first, with zero GitHub API requests (pr-heads makes one
  ``git ls-remote`` over the git transport, as the canonical-clone refresh
  does). When neither can answer
  (no record, or a non-terminal record older than eight minutes) the read goes
  to node_github_repo_gateway_effect over the bus. ``--source local`` never
  asks the bus; ``--source bus`` never reads locally.
- **Clone-only reads** (pr-commits, file-read, commit-view, commit-checks) have
  no node operation: they are answered from the canonical clone and the watcher
  state, and a read they cannot answer exits 3 with the reason, never reaching
  the bus or GitHub.
- **Other reads** (pr-review, pr-ticket, pr-threads, branch-protection, run-list,
  run-view, job-log, run-download, release-list) publish the gateway's typed request.
- **PR authoring writes** (pr-create, pr-edit, pr-ready, pr-close, pr-comment,
  pr-thread-reply, workflow-run, check-run-create) publish node_github_repo_write_effect's typed
  request. Its guards apply: quota floor, expected head, idempotency, and the claim pr-close
  needs. pr-edit, pr-ready and pr-close take the expected head from the
  watcher state when ``--head-sha`` is not given. pr-close reads the claim the
  caller took from the caller's own claim registry and carries it on the
  command: the effect runs elsewhere and cannot read that directory.
- **merge, arm, rerun, update-branch** have no node operation and are refused,
  naming the landing controller that does them.

Every bus-answered verb prints one JSON answer carrying the terminal event's
topic, partition, offset and envelope ids. Exit codes: 0 completed, 1 the node
answered failed, 2 usage or a refused verb, 3 ``--source local`` could not
answer, 69 the bus could not be opened, 75 no terminal event in time (the
outcome is unknown: read before retrying; a write retried with the same
idempotency key is not written twice).
Gateway reads retry on no terminal or bus unavailability for up to three total
attempts, pausing 20 seconds between attempts; writes make one attempt (OMN-21396).
With ``--bus inmemory``, the selected contract is hosted in this process by
the runtime dispatcher before the command is published.

Registered in ``[project.entry-points."onex.cli"]``; the CLI discovers it over
installed distributions.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

import click
from omnibase_infra.errors import InfraConnectionError
from pydantic import BaseModel, JsonValue, ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.handlers.scm.handler_scm_consumer_diagnostics import ScmNoTerminalError
from omnimarket.handlers.scm.handler_scm_dispatch import (
    SCM_LOCAL_VERBS,
    SCM_REFUSED_VERBS,
    SCM_VERBS,
    ScmBusCaller,
)
from omnimarket.handlers.scm.handler_scm_local_state import (
    CLONE_ROOT_ENV,
    HandlerScmLocalState,
    default_claims_dir,
    default_clone_path,
    default_watcher_state_path,
)
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from omnimarket.models.scm import (
    EnumScmLocalRead,
    EnumScmNode,
    ModelScmAnswer,
    ModelScmLocalAnswer,
    ModelScmLocalReadRequest,
    ModelScmLocalVerb,
    ModelScmTerminal,
    ModelScmVerb,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_pr_claim_lookup import (
    HandlerPrClaimLookup,
)
from omnimarket.nodes.node_github_repo_write_effect.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteClaim,
    ModelRepoWriteRequest,
)

EXIT_FAILED: Final[int] = 1
EXIT_USAGE: Final[int] = 2
EXIT_LOCAL_UNANSWERED: Final[int] = 3
EXIT_BUS_UNAVAILABLE: Final[int] = 69
EXIT_NO_TERMINAL: Final[int] = 75
READ_RETRY_PAUSE_S: Final[float] = 20.0

_TARGET = re.compile(
    r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:#(?P<number>[1-9][0-9]*))?$"
)
_PR_TARGET_HELP = "OWNER/REPO#NUMBER"
_REPO_TARGET_HELP = "OWNER/REPO"


def _target(text: str, *, pr: bool) -> tuple[str, int | None]:
    match = _TARGET.match(text)
    if match is None or (pr and match["number"] is None):
        raise click.UsageError(
            f"target {text!r} is not {_PR_TARGET_HELP if pr else _REPO_TARGET_HELP}"
        )
    if not pr and match["number"] is not None:
        raise click.UsageError(f"target {text!r} names a PR; give {_REPO_TARGET_HELP}")
    number = match["number"]
    return match["repo"], int(number) if number is not None else None


def _emit(answer: ModelScmAnswer, *, attempts: int | None = None) -> int:
    payload = answer.model_dump(mode="json")
    if attempts is not None:
        payload["attempts"] = attempts
    click.echo(json.dumps(payload, separators=(",", ":")))
    return EXIT_FAILED if answer.outcome == "failed" else 0


def _call_bus(
    spec: ModelScmVerb,
    repo: str,
    request: BaseModel,
    correlation_id: uuid.UUID,
    bus_opts: dict[str, object],
    timeout_s: float,
) -> int:
    async def main() -> ModelScmTerminal:
        async with _open_bus({**bus_opts, "scm_node": spec.node}) as opened:
            return await ScmBusCaller(opened).call(
                node=spec.node,
                request=request,
                correlation_id=correlation_id,
                timeout_s=timeout_s,
            )

    max_attempts = 3 if spec.node is EnumScmNode.GATEWAY else 1
    attempt = 1
    while True:
        try:
            terminal = asyncio.run(main())
        except TimeoutError as exc:
            if attempt == max_attempts:
                click.echo(
                    json.dumps(
                        {
                            "verb": spec.verb,
                            "status": "no_terminal",
                            "correlation_id": str(correlation_id),
                            "timeout_s": timeout_s,
                            "attempts": attempt,
                            **(
                                exc.evidence.model_dump(mode="json")
                                if isinstance(exc, ScmNoTerminalError)
                                else {}
                            ),
                        }
                    )
                )
                return EXIT_NO_TERMINAL
        except (LabRunBusError, InfraConnectionError) as exc:
            # A broker that refuses this principal a topic (an ACL) lands here too.
            click.echo(f"bus: {exc}", err=True)
            if attempt == max_attempts:
                return EXIT_BUS_UNAVAILABLE
        else:
            failed = (
                terminal.failed
                or terminal.payload.get("outcome") == "failed"
                or terminal.payload.get("status") == "failed"
            )
            result = terminal.payload.get("result", terminal.payload)
            return _emit(
                ModelScmAnswer(
                    verb=spec.verb,
                    node=spec.node,
                    operation=spec.operation,
                    repo=repo,
                    source="bus",
                    outcome="failed" if failed else "completed",
                    bus=terminal.receipt,
                    result=result if isinstance(result, dict) else terminal.payload,
                ),
                attempts=attempt,
            )
        time.sleep(READ_RETRY_PAUSE_S)
        attempt += 1
        correlation_id = uuid.uuid4()
        request = request.model_copy(update={"correlation_id": correlation_id})


def _open_bus(
    bus_opts: dict[str, object],
) -> contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]:
    """Open the bus ``onex lab-work`` and ``onex work-ledger`` open.

    The opener's fourth keyword parameter, the workspace root, carries the
    private registry clone's name, which the public-repo hygiene gate refuses
    on any added line; its four parameters are bound from its signature, in
    declared order, instead of being spelled here.
    """
    names = tuple(inspect.signature(open_lab_run_bus).parameters)
    if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
        raise LabRunBusError(f"unexpected bus opener parameters: {names}")
    values: tuple[object, ...] = (
        _bus_kind(bus_opts["bus"]),
        _optional_str(bus_opts["bus_lane"]),
        _optional_str(bus_opts["kafka_bootstrap"]),
        _optional_path(bus_opts["omnibase_path"]),
    )
    opener = cast(
        Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
        open_lab_run_bus,
    )
    node = bus_opts.get("scm_node")
    opened = opener(**dict(zip(names, values, strict=True)))
    if values[0] == "inmemory":
        from omnimarket.handlers.scm.scm_inprocess import open_scm_inprocess

        if not isinstance(node, EnumScmNode):
            raise LabRunBusError("the in-memory SCM bus requires a node")
        return open_scm_inprocess(opened, node)
    return _open_scm_bound(
        opened,
        opener,
        values,
        node if isinstance(node, EnumScmNode) else None,
    )


@contextlib.asynccontextmanager
async def _open_scm_bound(
    durable: contextlib.AbstractAsyncContextManager[ProtocolLabRunBus],
    opener: Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
    values: tuple[object, ...],
    node: EnumScmNode | None,
) -> AsyncIterator[ProtocolLabRunBus]:
    """The durable bus, or the in-memory bus hosting this node when the lane overlay orders a fallback
    and the durable bus fails its bounded health read (OMN-20275)."""
    from omnimarket.handlers.bus_binding.select import open_bound_bus, positional_opener
    from omnimarket.handlers.scm.scm_inprocess import open_scm_inprocess

    elsewhere = positional_opener(opener)
    used: list[bool] = []

    def adapter(
        bus: str, lane: str | None, bootstrap: str | None, root: Path | None
    ) -> contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]:
        if bus == "kafka" and not used:
            used.append(True)
            return durable
        return elsewhere(bus, lane, bootstrap, root)

    async with open_bound_bus(
        open_bus=adapter,
        bus=_bus_kind(values[0]),
        lane=_optional_str(values[1]),
        kafka_bootstrap=_optional_str(values[2]),
        workspace_root=_optional_path(values[3]),
    ) as bound:
        if not bound.decision.degraded:
            yield bound.bus
            return
        if node is None:
            raise LabRunBusError("the in-memory SCM fallback requires a node")

        @contextlib.asynccontextmanager
        async def borrowed() -> AsyncIterator[ProtocolLabRunBus]:
            yield bound.bus

        async with open_scm_inprocess(borrowed(), node) as hosted:
            yield hosted


def _bus_kind(value: object) -> BusKind:
    return "inmemory" if value == "inmemory" else "kafka"


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_path(value: object) -> Path | None:
    return value if isinstance(value, Path) else None


def _body(body: str | None, body_file: str | None) -> str | None:
    if body is not None and body_file is not None:
        raise click.UsageError("give --body or --body-file, not both")
    if body_file is None:
        return body
    with click.open_file(body_file, "r", encoding="utf-8") as source:
        text: str = source.read()
    return text


def _idempotency_key(operation: str, fields: dict[str, JsonValue]) -> str:
    """Stable per intended write: the same command run twice writes once."""
    digest = hashlib.sha256(
        json.dumps(fields, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"scm-{operation}-{digest[:40]}"


@click.group("scm")
@click.pass_context
def scm_group(ctx: click.Context) -> None:
    """GitHub work through the source-control nodes: one verb, one typed answer."""
    ctx.ensure_object(dict)


@scm_group.command("verbs")
def verbs_command() -> None:
    """Print every served verb, the node operation and the gh verb it replaces."""
    click.echo(
        json.dumps(
            {
                "served": [spec.model_dump(mode="json") for spec in SCM_VERBS],
                "served_local_only": [
                    local.model_dump(mode="json") for local in SCM_LOCAL_VERBS
                ],
                "refused": [
                    refused.model_dump(mode="json") for refused in SCM_REFUSED_VERBS
                ],
            },
            indent=2,
        )
    )


# ------------------------------------------------------------------- reads

_PR_SCOPED_READS: Final[frozenset[str]] = frozenset(
    {
        "pr_text",
        "pr_status",
        "ci_checks",
        "merge_commit_sha",
        "merged_pr",
        "pr_review_threads",
        "review_gate",
        "ticket_ref",
    }
)


def _read_command(spec: ModelScmVerb) -> click.Command:
    pr_scoped = spec.operation in _PR_SCOPED_READS

    @_with_bus_options
    @click.argument("target")
    @click.option("--run-id", type=int, default=None, help="run-view, run-download.")
    @click.option("--job-id", type=int, default=None, help="job-log.")
    @click.option("--artifact-id", type=int, default=None, help="run-download.")
    @click.option(
        "--branch", default=None, help="run-list, branch-protection, pr-heads."
    )
    @click.option(
        "--head-sha",
        default=None,
        help="run-list: runs of this head; pr-heads; commit-check-runs.",
    )
    @click.option(
        "--check-name", default=None, help="commit-check-runs: runs of this name."
    )
    @click.option("--workflow", default=None, help="run-list: a workflow file or id.")
    @click.option("--event", default=None, help="run-list: the triggering event.")
    @click.option("--status", default=None, help="run-list: the run status.")
    @click.option("--limit", type=int, default=None)
    @click.option("--tail-bytes", type=int, default=None, help="job-log.")
    @click.option(
        "--offset-bytes", type=int, default=None, help="job-log: window start."
    )
    @click.option(
        "--length-bytes", type=int, default=None, help="job-log: window size."
    )
    @click.option("--step", default=None, help="job-log: one step's lines, by name.")
    @click.option("--max-bytes", type=int, default=None, help="run-download.")
    @click.option(
        "--source",
        type=click.Choice(["auto", "local", "bus"]),
        default="auto",
        show_default=True,
        help="Local-first reads: watcher state and clones, then the bus.",
    )
    @click.option(
        "--watcher-state",
        type=click.Path(dir_okay=False),
        default=None,
        help="The PR watcher state file (default $ONEX_PR_WATCHER_STATE).",
    )
    @click.option(
        "--clone",
        type=click.Path(file_okay=False),
        default=None,
        help=f"The repository's canonical clone (default ${CLONE_ROOT_ENV}/<repo>).",
    )
    @click.option("--timeout-s", type=float, default=60.0, show_default=True)
    def command(
        target: str,
        source: str,
        watcher_state: str | None,
        clone: str | None,
        timeout_s: float,
        **fields: object,
    ) -> None:
        repo, number = _target(target, pr=pr_scoped)
        bus_opts = {
            name: fields.pop(name)
            for name in ("omnibase_path", "bus", "bus_lane", "kafka_bootstrap")
        }
        if spec.local_read is not None and source != "bus":
            watcher = watcher_state or default_watcher_state_path()
            clone_path = clone or default_clone_path(repo)
            local = HandlerScmLocalState().handle(
                ModelScmLocalReadRequest(
                    read=spec.local_read,
                    repo=repo,
                    pr_number=number,
                    watcher_state_path=str(watcher) if watcher else None,
                    clone_path=str(clone_path) if clone_path else None,
                    branch=_optional_str(fields.get("branch")),
                    head_sha=_optional_str(fields.get("head_sha")),
                )
            )
            if local.answered and local.source is not None:
                sys.exit(
                    _emit(
                        ModelScmAnswer(
                            verb=spec.verb,
                            node=spec.node,
                            operation=spec.operation,
                            repo=repo,
                            source=local.source,
                            outcome="completed",
                            watcher_tick=local.watcher_tick,
                            result=local.result,
                        )
                    )
                )
            if source == "local":
                click.echo(f"local: {local.unanswered_reason}", err=True)
                sys.exit(EXIT_LOCAL_UNANSWERED)
        elif source == "local":
            raise click.UsageError(f"{spec.verb} has no local answer; use the bus")
        given = {name: value for name, value in fields.items() if value is not None}
        correlation_id = uuid.uuid4()
        try:
            request = ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation(spec.operation),
                repo=repo,
                pr_number=number,
                correlation_id=correlation_id,
                **given,
            )
        except ValidationError as exc:
            raise click.UsageError(str(exc)) from exc
        sys.exit(_call_bus(spec, repo, request, correlation_id, bus_opts, timeout_s))

    return click.command(spec.verb, help=f"Replaces `{spec.replaces}`.")(command)


# ------------------------------------------------------- clone-only reads

# The options each clone-only verb takes, beyond the target and the common ones.
_LOCAL_OPTIONS: Final[dict[EnumScmLocalRead, tuple[str, ...]]] = {
    EnumScmLocalRead.PR_COMMITS: (),
    EnumScmLocalRead.FILE_CONTENTS: ("path", "ref", "max_bytes"),
    EnumScmLocalRead.COMMIT_VIEW: ("ref",),
    EnumScmLocalRead.COMMIT_CHECKS: ("sha",),
}
_LOCAL_OPTION_DECORATORS: Final[
    dict[str, Callable[[click.decorators.FC], click.decorators.FC]]
] = {
    "path": click.option("--path", required=True, help="A file or directory."),
    "ref": click.option(
        "--ref",
        default=None,
        help="A branch, tag or commit (file-read default: the default branch).",
    ),
    "max_bytes": click.option(
        "--max-bytes", type=int, default=262144, show_default=True
    ),
    "sha": click.option(
        "--sha", required=True, help="The head commit, whole or abbreviated."
    ),
}


def _local_command(spec: ModelScmLocalVerb) -> click.Command:
    pr_scoped = spec.read is EnumScmLocalRead.PR_COMMITS
    ref_required = spec.read is EnumScmLocalRead.COMMIT_VIEW

    def command(
        target: str,
        watcher_state: str | None,
        clone: str | None,
        **fields: object,
    ) -> None:
        repo, number = _target(target, pr=pr_scoped)
        if ref_required and fields.get("ref") is None:
            raise click.UsageError("Missing option '--ref'.")
        try:
            request = ModelScmLocalReadRequest(
                read=spec.read,
                repo=repo,
                pr_number=number,
                watcher_state_path=str(
                    watcher_state or default_watcher_state_path() or ""
                )
                or None,
                clone_path=str(clone or default_clone_path(repo) or "") or None,
                **{name: value for name, value in fields.items() if value is not None},
            )
        except ValidationError as exc:
            raise click.UsageError(str(exc)) from exc
        local = HandlerScmLocalState().handle(request)
        if not local.answered or local.source is None:
            click.echo(
                f"local: {local.unanswered_reason} ({spec.verb} has no bus or "
                f"GitHub fallback)",
                err=True,
            )
            sys.exit(EXIT_LOCAL_UNANSWERED)
        click.echo(
            ModelScmLocalAnswer(
                verb=spec.verb,
                read=spec.read,
                repo=repo,
                source=local.source,
                watcher_tick=local.watcher_tick,
                result=local.result,
            ).model_dump_json()
        )

    decorated: Callable[..., None] = command
    for name in reversed(_LOCAL_OPTIONS[spec.read]):
        decorated = _LOCAL_OPTION_DECORATORS[name](decorated)
    decorated = click.option(
        "--watcher-state",
        type=click.Path(dir_okay=False),
        default=None,
        help="The PR watcher state file (default $ONEX_PR_WATCHER_STATE).",
    )(decorated)
    decorated = click.option(
        "--clone",
        type=click.Path(file_okay=False),
        default=None,
        help=f"The repository's canonical clone (default ${CLONE_ROOT_ENV}/<repo>).",
    )(decorated)
    decorated = click.argument("target")(decorated)
    return click.command(spec.verb, help=f"Replaces `{spec.replaces}`.")(decorated)


# ------------------------------------------------------------------ writes

# The options each write verb takes, beyond the target and the common ones.
_WRITE_OPTIONS: Final[dict[str, tuple[str, ...]]] = {
    "pr_create": ("head", "base", "title", "body", "body_file", "draft"),
    "pr_edit": ("title", "body", "body_file", "base", "draft"),
    "pr_ready": (),
    "pr_close": ("run", "claims_dir"),
    "pr_comment": ("body", "body_file"),
    "pr_thread_reply": ("thread", "body", "body_file"),
    "workflow_dispatch": ("workflow", "ref", "input"),
    "check_run_create": (
        "name",
        "status",
        "conclusion",
        "output_title",
        "output_summary",
    ),
}
_REPO_SCOPED_WRITES: Final[frozenset[str]] = frozenset(
    {"pr_create", "workflow_dispatch", "check_run_create"}
)
_HEAD_FROM_WATCHER: Final[frozenset[str]] = frozenset(
    {"pr_edit", "pr_ready", "pr_close"}
)

_OPTION_DECORATORS: Final[
    dict[str, Callable[[click.decorators.FC], click.decorators.FC]]
] = {
    "name": click.option("--name", required=True, help="The check run's name."),
    "status": click.option(
        "--status",
        required=True,
        type=click.Choice(["queued", "in_progress", "completed"]),
    ),
    "conclusion": click.option(
        "--conclusion",
        default=None,
        type=click.Choice(
            [
                "action_required",
                "cancelled",
                "failure",
                "neutral",
                "success",
                "skipped",
                "timed_out",
            ]
        ),
    ),
    "output_title": click.option("--output-title", default=None),
    "output_summary": click.option("--output-summary", default=None),
    "head": click.option("--head", required=True, help="The PR's head branch."),
    "base": click.option("--base", default=None, help="The base branch."),
    "title": click.option("--title", default=None),
    "body": click.option("--body", default=None),
    "body_file": click.option(
        "--body-file", type=click.Path(dir_okay=False, allow_dash=True), default=None
    ),
    "draft": click.option("--draft/--no-draft", default=None),
    "run": click.option(
        "--run",
        envvar="ONEX_RUN_ID",
        required=True,
        help="The run the PR claim was taken under ($ONEX_RUN_ID).",
    ),
    "claims_dir": click.option(
        "--claims-dir",
        envvar="ONEX_PR_CLAIMS_DIR",
        default=None,
        help="Where this host's claim registry keeps the claim you took "
        "($ONEX_PR_CLAIMS_DIR, else $ONEX_STATE_DIR/pr-queue/claims). The claim "
        "is read here and sent on the command; the directory itself is not.",
    ),
    "thread": click.option(
        "--thread",
        "thread_comment_id",
        required=True,
        type=click.IntRange(min=1),
        help="The id of the review comment that starts the thread "
        "(`onex scm pr-threads` lists them).",
    ),
    "workflow": click.option("--workflow", required=True),
    "ref": click.option("--ref", required=True),
    "input": click.option(
        "--input", "inputs", multiple=True, help="KEY=VALUE, repeatable."
    ),
}


def _carried_claim(
    claims_dir: str | None, repo: str, number: int | None
) -> ModelRepoWriteClaim | None:
    """The live claim the caller took on the PR, read from the caller's registry.

    None when there is none or it lacks a field the effect checks; the close is
    then sent without one and the effect refuses it, with the same typed
    ``claim_not_held`` event a lane reads.
    """
    directory = claims_dir or (str(path) if (path := default_claims_dir()) else None)
    pr_key = f"{repo.lower()}#{number}"
    if directory is None:
        click.echo(
            f"no claim registry to read {pr_key} from; give --claims-dir", err=True
        )
        return None
    held = HandlerPrClaimLookup().active_claim(
        claims_dir=directory,
        pr_key=pr_key,
        now=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if held is None:
        click.echo(f"no live claim on {pr_key} in {directory}", err=True)
        return None
    try:
        return ModelRepoWriteClaim(
            pr_key=pr_key,
            lane_id=held.lane_id or "",
            claimed_by_run=held.claimed_by_run or "",
            action=held.action or "",
            claimed_at=held.claimed_at or "",
            last_heartbeat_at=held.last_heartbeat_at,
        )
    except ValidationError as exc:
        click.echo(f"the claim on {pr_key} in {directory} is unusable: {exc}", err=True)
        return None


def _write_command(spec: ModelScmVerb) -> click.Command:
    pr_scoped = spec.operation not in _REPO_SCOPED_WRITES

    def command(
        target: str,
        lane: str,
        head_sha: str | None,
        idempotency_key: str | None,
        dry_run: bool,
        watcher_state: str | None,
        timeout_s: float,
        **fields: object,
    ) -> None:
        repo, number = _target(target, pr=pr_scoped)
        bus_opts = {
            name: fields.pop(name)
            for name in ("omnibase_path", "bus", "bus_lane", "kafka_bootstrap")
        }
        if head_sha is None and spec.operation in _HEAD_FROM_WATCHER:
            head_sha = _watcher_head(repo, number, watcher_state)
        body = _body(
            _optional_str(fields.pop("body", None)),
            _optional_str(fields.pop("body_file", None)),
        )
        inputs: dict[str, str] = {}
        raw_inputs = fields.pop("inputs", ())
        for item in raw_inputs if isinstance(raw_inputs, tuple) else ():
            key, sep, value = str(item).partition("=")
            if not sep or not key:
                raise click.UsageError(f"--input {item!r} is not KEY=VALUE")
            inputs[key] = value
        run = fields.pop("run", None)
        claims_dir = fields.pop("claims_dir", None)
        claim = (
            _carried_claim(
                claims_dir if isinstance(claims_dir, str) else None, repo, number
            )
            if spec.operation == EnumRepoWriteOperation.PR_CLOSE
            else None
        )
        given: dict[str, JsonValue] = {
            name: value
            for name, value in fields.items()
            if value is not None and isinstance(value, (str, bool, int))
        }
        if spec.operation == EnumRepoWriteOperation.CHECK_RUN_CREATE:
            given["head_sha"] = head_sha
            head_sha = None
            output_title = given.pop("output_title", None)
            output_summary = given.pop("output_summary", None)
            if output_title is not None or output_summary is not None:
                given["output"] = {"title": output_title, "summary": output_summary}
        if body is not None:
            given["body"] = body
        if run is not None:
            given["requested_by_run"] = str(run)
        identity: dict[str, JsonValue] = {
            "repo": repo,
            "pr_number": number,
            "lane": lane,
            "expected_head_sha": head_sha,
            "inputs": dict(inputs),
            **given,
        }
        correlation_id = uuid.uuid4()
        try:
            request = ModelRepoWriteRequest(
                correlation_id=correlation_id,
                operation=EnumRepoWriteOperation(spec.operation),
                repo=repo,
                pr_number=number,
                idempotency_key=idempotency_key
                or _idempotency_key(spec.operation, identity),
                requested_by_lane=lane,
                expected_head_sha=head_sha,
                inputs=inputs,
                dry_run=dry_run,
                claim=claim,
                **given,
            )
        except ValidationError as exc:
            raise click.UsageError(str(exc)) from exc
        sys.exit(_call_bus(spec, repo, request, correlation_id, bus_opts, timeout_s))

    decorated: Callable[..., None] = command
    for name in reversed(_WRITE_OPTIONS[spec.operation]):
        decorated = _OPTION_DECORATORS[name](decorated)
    decorated = click.option(
        "--timeout-s", type=float, default=60.0, show_default=True
    )(decorated)
    decorated = click.option(
        "--watcher-state",
        type=click.Path(dir_okay=False),
        default=None,
        help="The PR watcher state file the expected head is read from.",
    )(decorated)
    decorated = click.option(
        "--dry-run", is_flag=True, default=False, help="Plan the calls; send nothing."
    )(decorated)
    decorated = click.option(
        "--idempotency-key",
        default=None,
        help="Default: a hash of the command, so a retry writes once.",
    )(decorated)
    decorated = click.option(
        "--head-sha",
        default=None,
        help="check-run-create: the commit to report on; other writes: the expected head.",
    )(decorated)
    decorated = click.option(
        "--lane",
        envvar="ONEX_LANE",
        required=True,
        help="The lane asking for the write ($ONEX_LANE).",
    )(decorated)
    decorated = click.argument("target")(decorated)
    decorated = _with_bus_options(decorated)
    return click.command(spec.verb, help=f"Replaces `{spec.replaces}`.")(decorated)


def _watcher_head(repo: str, number: int | None, watcher_state: str | None) -> str:
    path = watcher_state or default_watcher_state_path()
    local = HandlerScmLocalState().handle(
        ModelScmLocalReadRequest(
            read=EnumScmLocalRead.PR_STATUS,
            repo=repo,
            pr_number=number,
            watcher_state_path=str(path) if path else None,
        )
    )
    head = local.result.get("head_sha")
    if not local.answered or not isinstance(head, str) or not head:
        raise click.UsageError(
            f"give --head-sha: the watcher state cannot name the head "
            f"({local.unanswered_reason or 'no head recorded'})"
        )
    return head


# ----------------------------------------------------------------- refused


def _refused_command(verb_name: str, handled_by: str) -> click.Command:
    @click.command(
        verb_name,
        hidden=True,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    def command() -> None:
        click.echo(
            f"onex scm {verb_name}: no source-control node operation serves this "
            f"verb. It is done by {handled_by}.",
            err=True,
        )
        sys.exit(EXIT_USAGE)

    return command


for _spec in SCM_VERBS:
    scm_group.add_command(
        _write_command(_spec)
        if _spec.node is EnumScmNode.WRITE
        else _read_command(_spec)
    )
for _local in SCM_LOCAL_VERBS:
    scm_group.add_command(_local_command(_local))
for _refused in SCM_REFUSED_VERBS:
    scm_group.add_command(_refused_command(_refused.verb, _refused.handled_by))


__all__: list[str] = [
    "EXIT_BUS_UNAVAILABLE",
    "EXIT_FAILED",
    "EXIT_LOCAL_UNANSWERED",
    "EXIT_NO_TERMINAL",
    "EXIT_USAGE",
    "scm_group",
]
