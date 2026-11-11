# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lanes one merge sweep dispatches (OMN-20676).

A port of the merge-sweep skill's ``plan`` rule and the sweep workflow's dispatch cap. The lanes
come out in order: diagnose, escalation, land-chain-head, fix. Load decides only placement
(``lab_only``); it never removes a lane. Merged or closed PRs, live-owned PRs and PRs of a
repository outside the scope are skipped with a reason. A chain head that is red is not landed:
it leads its repository's fix lane. The cap (``max_lanes``) defers the lanes beyond it to the
next sweep and never filters or reorders one.

A red whose remedy is a landing operation (rerun, update-branch, arm/enqueue) also plans a
watch-only landing command; a PR with a command loses its lane (a real failure keeps its fix
lane). The commands are pinned to the reading's facts and never issued twice at the same head.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)

from ..models import (
    DEFAULT_MAX_LANES,
    MAX_LANES_CEILING,
    ModelMergeSweepChainHead,
    ModelMergeSweepLandingCommand,
    ModelMergeSweepLane,
    ModelMergeSweepLanePr,
    ModelMergeSweepOwner,
    ModelMergeSweepPlanRequest,
    ModelMergeSweepPlanResult,
    ModelMergeSweepReading,
    ModelMergeSweepRed,
    ModelMergeSweepSkipped,
)

# The repositories a sweep lane may work in. Anything else (a parked repository) is skipped.
FLEET_REPOS = frozenset(
    {
        "omnibase_core",
        "omnibase_spi",
        "omnibase_infra",
        "omnibase_internal",
        "omniclaude",
        "omniclaude-internal",
        "omnimarket",
        "omnidash",
        "omniintelligence",
        "omninode_infra",
    }
)
LOAD_PER_CORE_BAR = 1.0
MAX_PRS_PER_LANE = 5

_NO_OWNER = ModelMergeSweepOwner(state="none")


def pr_key(token: str) -> str:
    """``OmniNode-ai/omnimarket#3059`` and ``omnimarket#3059`` are the same key."""
    return str(token).strip().strip("`").split("/")[-1].lower()


def repo_of(key: str) -> str:
    return key.split("#", 1)[0]


def max_lanes_of(raw: int | None) -> int:
    """The dispatch cap: absent or zero reads as the default, anything else is clamped to 1..12."""
    return max(1, min(MAX_LANES_CEILING, raw or DEFAULT_MAX_LANES))


def _lab_only(reading: ModelMergeSweepReading) -> bool:
    cpus = float(reading.cpus or 0)
    if reading.load1 is None or cpus <= 0:
        return True  # unread load: place on the lab, never withhold
    return float(reading.load1) / cpus > LOAD_PER_CORE_BAR


def _owner_gate(
    key: str,
    state: str,
    owner: ModelMergeSweepOwner | None,
    scope: frozenset[str],
    skipped: list[ModelMergeSweepSkipped],
) -> dict[str, Any] | None:
    if repo_of(key) not in scope:
        skipped.append(ModelMergeSweepSkipped(pr=key, why="out-of-scope-repo"))
        return None
    if str(state).upper() != "OPEN":
        skipped.append(ModelMergeSweepSkipped(pr=key, why="merged-or-closed"))
        return None
    held = owner or _NO_OWNER
    if held.state == "live":
        skipped.append(
            ModelMergeSweepSkipped(pr=key, why=f"live-owner lane={held.lane}")
        )
        return None
    if held.state not in {"none", "stale"}:
        skipped.append(
            ModelMergeSweepSkipped(pr=key, why=f"owner-unread state={held.state}")
        )
        return None
    entry: dict[str, Any] = {"pr": key}
    if held.state == "stale":
        entry["supersedes_claim"] = held.claim_ts
    return entry


def _head_first(heads: list[str]) -> Callable[[dict[str, Any]], tuple[bool, int]]:
    """Sort key: the repository's red chain heads first, in their order; the rest keep theirs."""

    def key(entry: dict[str, Any]) -> tuple[bool, int]:
        pr = str(entry["pr"])
        return (pr not in heads, heads.index(pr) if pr in heads else 0)

    return key


def _lane(
    kind: str, repo: str | None, prs: list[dict[str, Any]], **extra: Any
) -> ModelMergeSweepLane:
    return ModelMergeSweepLane(
        kind=kind,
        repo=repo,
        prs=[ModelMergeSweepLanePr(**p) for p in prs],
        **extra,
    )


_Row = ModelMergeSweepRed | ModelMergeSweepChainHead
_UPDATE_CLASSES = ("draft-era", "stale-base-sql")


def _repository_of(row: _Row) -> str | None:
    """The owner/name slug a command is pinned to: ``repository``, else a ``repo`` that is one."""
    repo = getattr(row, "repository", None)
    if repo:
        return str(repo)
    slug = getattr(row, "repo", None)
    if slug and "/" in str(slug):
        return str(slug)
    return None


def _correlation_of(request: ModelMergeSweepPlanRequest, idempotency_key: str) -> UUID:
    """The sweep run's correlation id, or one derived from the command's own key."""
    if request.correlation_id is not None:
        return request.correlation_id
    return uuid5(NAMESPACE_URL, idempotency_key)


def _landing_command(
    request: ModelMergeSweepPlanRequest,
    *,
    repository: str,
    number: int,
    head_sha: str,
    operation: EnumPrLandingGithubOperation,
    reason: str,
    **fields: Any,
) -> ModelMergeSweepLandingCommand:
    """One watch-only landing command, pinned to the facts that planned it."""
    idempotency_key = f"{repository}#{number}@{head_sha}:{operation.value}"
    correlation_id = _correlation_of(request, idempotency_key)
    body: dict[str, Any] = {
        "correlation_id": correlation_id,
        "operation": operation,
        "mode": request.landing_mode,
        "repository": repository,
        "pr_number": number,
        "head_sha": head_sha,
    }
    body.update(fields)
    return ModelMergeSweepLandingCommand(
        request=ModelPrLandingGithubRequest.model_validate(body),
        idempotency_key=idempotency_key,
        reason=reason,
    )


def _issued_at_head(
    reading: ModelMergeSweepReading, row: _Row, operation: EnumPrLandingGithubOperation
) -> bool:
    """True when this PR at this head already carries this operation's issued command."""
    for issued in reading.issued_commands or ():
        if (
            pr_key(issued.repo) == pr_key(row.repo)
            and issued.number == row.number
            and issued.head_sha == row.head_sha
            and issued.operation == operation.value
        ):
            return True
    return False


def _has_real(row: ModelMergeSweepRed) -> bool:
    """A real failure is never a landing remedy: it keeps its fix lane."""
    return any(c.get("cls") == "real" for c in (row.classes or ()))


def _red_operation(row: ModelMergeSweepRed) -> EnumPrLandingGithubOperation | None:
    """The one landing operation a red's classes plan, or None when none is a landing remedy.

    update-branch (draft-era, stale-base-sql) outranks the rest: the head will move, so the
    other remedies are moot. A real failure plans no command (it is named in the skip record).
    """
    classes = row.classes or []
    if any(c.get("cls") in _UPDATE_CLASSES for c in classes):
        return EnumPrLandingGithubOperation.UPDATE_BRANCH
    if any(c.get("cls") == "cancelled-latest" for c in classes):
        return EnumPrLandingGithubOperation.RERUN_RUNS
    if any(c.get("cls") == "workflow-scope-arm" for c in classes):
        return (
            EnumPrLandingGithubOperation.ENQUEUE
            if row.merge_queue
            else EnumPrLandingGithubOperation.ARM_AUTO_MERGE
        )
    return None


def _red_fields(
    row: ModelMergeSweepRed, operation: EnumPrLandingGithubOperation
) -> dict[str, Any] | None:
    """The operation's extra request fields, or None when a fact the operation needs is unread."""
    if operation is EnumPrLandingGithubOperation.RERUN_RUNS:
        run_ids = sorted(
            {
                int(c["run_id"])
                for c in (row.classes or [])
                if c.get("cls") == "cancelled-latest" and int(c.get("run_id") or 0) > 0
            }
        )
        if not run_ids:
            return None
        return {"run_ids": run_ids}
    if operation in (
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
        EnumPrLandingGithubOperation.ENQUEUE,
    ):
        return None if row.pr_node_id is None else {"pr_node_id": row.pr_node_id}
    return {}


def _red_reason(
    row: ModelMergeSweepRed, operation: EnumPrLandingGithubOperation
) -> str:
    """The class and remedy that planned the command (the first of its kind)."""
    cls = {
        EnumPrLandingGithubOperation.UPDATE_BRANCH: _UPDATE_CLASSES,
        EnumPrLandingGithubOperation.RERUN_RUNS: ("cancelled-latest",),
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE: ("workflow-scope-arm",),
        EnumPrLandingGithubOperation.ENQUEUE: ("workflow-scope-arm",),
    }[operation]
    for c in row.classes or []:
        if c.get("cls") in cls:
            return f"{c['cls']}: {c.get('remedy', '')}"
    return ""


def _base_takes_update(paced: set[tuple[str, str]], base: tuple[str, str]) -> bool:
    """One update_branch per (repository, base branch) per sweep: the first claims it."""
    if base in paced:
        return False
    paced.add(base)
    return True


def _plan_red_commands(
    row: ModelMergeSweepRed,
    *,
    key: str,
    request: ModelMergeSweepPlanRequest,
    reading: ModelMergeSweepReading,
    skipped: list[ModelMergeSweepSkipped],
    paced: set[tuple[str, str]],
) -> ModelMergeSweepLandingCommand | None:
    """The one landing command a red plans, or None (with a skip record when it should have)."""
    if _has_real(row):
        skipped.append(
            ModelMergeSweepSkipped(
                pr=key, why=f"real failure-record={request.real_failure_record}"
            )
        )
    operation = _red_operation(row)
    if operation is None:
        return None
    if row.head_sha is None:
        skipped.append(ModelMergeSweepSkipped(pr=key, why="unread head_sha"))
        return None
    repository = _repository_of(row)
    if repository is None:
        skipped.append(ModelMergeSweepSkipped(pr=key, why="unread repository"))
        return None
    if operation is EnumPrLandingGithubOperation.UPDATE_BRANCH and row.base_ref is None:
        skipped.append(ModelMergeSweepSkipped(pr=key, why="unread base_ref"))
        return None
    if _issued_at_head(reading, row, operation):
        skipped.append(
            ModelMergeSweepSkipped(pr=key, why=f"issued-at-head {operation.value}")
        )
        return None
    fields = _red_fields(row, operation)
    if fields is None:
        field = (
            "run_ids"
            if operation is EnumPrLandingGithubOperation.RERUN_RUNS
            else "pr_node_id"
        )
        skipped.append(ModelMergeSweepSkipped(pr=key, why=f"unread {field}"))
        return None
    if (
        operation is EnumPrLandingGithubOperation.UPDATE_BRANCH
        and row.base_ref is not None
    ):
        base = (repository, row.base_ref)
        if not _base_takes_update(paced, base):
            skipped.append(
                ModelMergeSweepSkipped(
                    pr=key, why=f"paced-base {operation.value} base={row.base_ref}"
                )
            )
            return None
    return _landing_command(
        request,
        repository=repository,
        number=row.number,
        head_sha=row.head_sha,
        operation=operation,
        reason=_red_reason(row, operation),
        **fields,
    )


def _plan_head_command(
    row: ModelMergeSweepChainHead,
    *,
    key: str,
    request: ModelMergeSweepPlanRequest,
    reading: ModelMergeSweepReading,
    skipped: list[ModelMergeSweepSkipped],
) -> ModelMergeSweepLandingCommand | None:
    """The arm/enqueue command a green-unarmed chain head plans, or None (with a skip record)."""
    operation = (
        EnumPrLandingGithubOperation.ENQUEUE
        if row.merge_queue
        else EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    )
    repository = _repository_of(row)
    if repository is None:
        skipped.append(ModelMergeSweepSkipped(pr=key, why="unread repository"))
        return None
    if row.head_sha is None:
        skipped.append(ModelMergeSweepSkipped(pr=key, why="unread head_sha"))
        return None
    if row.pr_node_id is None:
        skipped.append(ModelMergeSweepSkipped(pr=key, why="unread pr_node_id"))
        return None
    if _issued_at_head(reading, row, operation):
        skipped.append(
            ModelMergeSweepSkipped(pr=key, why=f"issued-at-head {operation.value}")
        )
        return None
    return _landing_command(
        request,
        repository=repository,
        number=row.number,
        head_sha=row.head_sha,
        operation=operation,
        reason="green-unarmed: arm",
        pr_node_id=row.pr_node_id,
    )


class HandlerMergeSweepPlan:
    """Plan the lanes of one merge sweep from one evaluated reading."""

    def handle(self, request: ModelMergeSweepPlanRequest) -> ModelMergeSweepPlanResult:
        reading = request.reading
        scope = frozenset(reading.scope or FLEET_REPOS)
        skipped: list[ModelMergeSweepSkipped] = []
        lanes: list[ModelMergeSweepLane] = []
        taken: set[str] = set()
        landing_commands: list[ModelMergeSweepLandingCommand] = []
        landing_skipped: list[ModelMergeSweepSkipped] = []
        paced: set[tuple[str, str]] = set()

        if reading.controller is None:
            stalled, reasons = True, ["controller unread"]
        else:
            stalled, reasons = (
                reading.controller.stalled,
                reading.controller.reasons or ["stalled"],
            )
        why: list[str] = []
        if stalled:
            why += [f"controller: {r}" for r in reasons]
        under_floor = (
            reading.product.under_floor if reading.product is not None else ["unread"]
        )
        if under_floor:
            why.append("under floor: " + ",".join(under_floor))
        if why:
            lanes.append(_lane("diagnose", None, [], reasons=why))

        for e in reading.escalations or ():
            key = pr_key(e.pr)
            entry = _owner_gate(key, e.state, e.owner, scope, skipped)
            if entry and key not in taken:
                taken.add(key)
                lanes.append(_lane("escalation", repo_of(key), [entry]))

        red_heads: dict[str, list[str]] = {}
        for h in reading.chain_heads or ():
            key = pr_key(f"{h.repo}#{h.number}")
            if h.cls == "red":
                red_heads.setdefault(h.repo, []).append(key)
                continue
            if h.cls != "green-unarmed" or key in taken:
                continue
            entry = _owner_gate(key, h.state, h.owner, scope, skipped)
            if not entry:
                continue
            taken.add(key)
            entry["children"] = list(h.children)
            cmd = _plan_head_command(
                h, key=key, request=request, reading=reading, skipped=landing_skipped
            )
            if cmd is not None:
                landing_commands.append(cmd)
                continue
            lanes.append(_lane("land-chain-head", h.repo, [entry]))

        fix_by_repo: dict[str, list[dict[str, Any]]] = {}
        for r in reading.reds or ():
            key = pr_key(f"{r.repo}#{r.number}")
            if key in taken:
                continue
            entry = _owner_gate(key, r.state, r.owner, scope, skipped)
            if not entry:
                continue
            taken.add(key)
            entry.update({"ticket": r.ticket, "classes": list(r.classes or [])})
            cmd = _plan_red_commands(
                r,
                key=key,
                request=request,
                reading=reading,
                skipped=landing_skipped,
                paced=paced,
            )
            if cmd is not None:
                landing_commands.append(cmd)
                if not _has_real(r):
                    continue
            fix_by_repo.setdefault(r.repo, []).append(entry)
        for repo in sorted(fix_by_repo):
            heads = red_heads.get(repo, [])
            prs = sorted(fix_by_repo[repo], key=_head_first(heads))
            for i in range(0, len(prs), MAX_PRS_PER_LANE):
                lanes.append(_lane("fix", repo, prs[i : i + MAX_PRS_PER_LANE]))

        cap = max_lanes_of(request.max_lanes)
        dispatch = lanes[:cap]
        load_per_core = (
            float(reading.load1) / float(reading.cpus)
            if reading.load1 is not None and reading.cpus
            else None
        )
        return ModelMergeSweepPlanResult(
            now=reading.now,
            lab_only=_lab_only(reading),
            load_per_core=load_per_core,
            lanes=lanes,
            skipped=skipped,
            dispatch=dispatch,
            deferred=len(lanes) - len(dispatch),
            landing_commands=landing_commands,
            landing_skipped=landing_skipped,
        )
