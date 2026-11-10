# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the delegation tracker reads (OMN-23081): the ticket mirror, the pull request watcher, the factory floor record, the
scenario workflows' runs and the lab nodes' scheduled results. Each reader names what it could not read; none returns an empty answer for an error."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.models.model_delegation_scenario import (
    EnumScenarioTrigger,
    from_status_row,
)
from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_tracker import (
    plan_ticket_ids,
    referenced_tickets,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelDelegationLane,
    ModelDelegationRuntime,
    ModelMirrorTicket,
    ModelScenarioDecl,
    ModelScenarioRead,
    ModelScenarioReads,
    ModelScenarioRun,
    ModelSourceReading,
    ModelWatcherPr,
    PrCi,
    PrState,
    ScenarioReadKind,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)

# Runs a command and returns (exit code, stdout, stderr). The tests replace it.
CommandRunner = Callable[[Sequence[str], int], tuple[int, str, str]]

_START_WHEN = re.compile(r"^Start when:\s*(.*)$", re.MULTILINE)
_PLAN_TASK = re.compile(r"Plan task:\s*\S+?\.md\s+(\S+)")
_TICKET_IN_TEXT = re.compile(r"OMN-\d+")
_TICKET_IN_BRANCH = re.compile(r"omn-(\d+)", re.IGNORECASE)
_CI_VERDICT: dict[str, PrCi] = {"GREEN": "green", "RED": "red", "PENDING": "pending"}
_PR_STATE: dict[str, PrState] = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}


def _age(read_at: str, now: datetime) -> int | None:
    try:
        moment = datetime.fromisoformat(read_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, int((now - moment).total_seconds()))


def _load_json(path: Path) -> tuple[Any, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc.strerror or exc}"
    except ValueError as exc:
        return None, f"{path.name} is not valid JSON: {exc}"


def read_mirror(
    mirror_dir: Path, ids: Sequence[str]
) -> tuple[tuple[ModelMirrorTicket, ...], str]:
    """The mirror documents of the ids, and the ids that were not found as a sentence (empty when all were)."""
    found: list[ModelMirrorTicket] = []
    missing: list[str] = []
    for tid in ids:
        doc, problem = _load_json(mirror_dir / f"{tid}.json")
        issues = doc.get("issues") if isinstance(doc, dict) else None
        issue = issues[0] if isinstance(issues, list) and issues else None
        if not isinstance(issue, dict) or problem:
            missing.append(tid)
            continue
        description = str(issue.get("description") or "")
        start = _START_WHEN.search(description)
        task = _PLAN_TASK.search(description)
        found.append(
            ModelMirrorTicket(
                id=tid,
                title=str(issue.get("title") or ""),
                plan_task=task.group(1) if task else "",
                linear_status=str(issue.get("status") or ""),
                status_type=str(issue.get("statusType") or ""),
                start_when=start.group(1).strip() if start else "",
                read_at=str(doc.get("read_at") or ""),
            )
        )
    detail = f"not in the mirror: {', '.join(missing)}" if missing else ""
    return tuple(found), detail


def read_mirror_with_references(
    mirror_dir: Path, rt: ModelDelegationRuntime
) -> tuple[tuple[ModelMirrorTicket, ...], ModelSourceReading]:
    """The plan tickets' mirror documents and those of the tickets their start conditions name."""
    plan = plan_ticket_ids(rt)
    first, detail = read_mirror(mirror_dir, plan)
    extra = [
        ref
        for t in first
        for ref in referenced_tickets(t.start_when)
        if ref not in plan
    ]
    second, _ = read_mirror(mirror_dir, sorted(set(extra)))
    found = (*first, *second)
    read_ats = [t.read_at for t in first if t.read_at]
    oldest = min(read_ats) if read_ats else ""
    ok = bool(first) and not detail
    return found, ModelSourceReading(
        id="mirror",
        title="Ticket mirror",
        ok=ok,
        read_at=oldest,
        detail=(
            f"{len(first)} of {len(plan)} plan tickets; oldest read {oldest or 'unknown'}"
            + (f"; {detail}" if detail else "")
            + ". A ticket closed since its read still shows its old status."
        ),
    )


def ticket_of(title: str, head_ref: str, wanted: set[str]) -> str:
    """The ticket a pull request belongs to: the first id in its title, else the one in its branch name. Empty when it is not a wanted one."""
    first = _TICKET_IN_TEXT.search(title)
    if first:
        found = first.group(0)
    else:
        branch = _TICKET_IN_BRANCH.search(head_ref)
        found = f"OMN-{branch.group(1)}" if branch else ""
    return found if found in wanted else ""


def read_watcher(
    path: Path,
    wanted: set[str],
    pull_url_prefix: str,
    max_age_seconds: int,
    now: datetime,
) -> tuple[tuple[ModelWatcherPr, ...], ModelSourceReading]:
    doc, problem = _load_json(path)
    title = "Pull request watcher"
    if problem or not isinstance(doc, dict) or not isinstance(doc.get("prs"), dict):
        return (), ModelSourceReading(
            id="watcher",
            title=title,
            ok=False,
            detail=problem or "the state has no pull request table",
        )
    last_tick = str(doc.get("last_tick") or "")
    age = _age(last_tick, now)
    prs: list[ModelWatcherPr] = []
    for ref, entry in doc["prs"].items():
        facts = entry.get("facts") if isinstance(entry, dict) else None
        if not isinstance(facts, dict):
            continue
        ticket = ticket_of(
            str(facts.get("title") or ""), str(facts.get("head_ref") or ""), wanted
        )
        state = _PR_STATE.get(str(facts.get("state") or "").upper())
        if not ticket or state is None:
            continue
        ci = entry.get("ci") if isinstance(entry.get("ci"), dict) else None
        verdict: PrCi = (
            _CI_VERDICT.get(str(ci.get("verdict") or "").upper(), "unread")
            if ci
            else "unread"
        )
        reason = str(entry.get("reason") or "")
        repo = str(facts.get("repo") or ref.split("#", 1)[0])
        number = int(facts.get("number") or ref.rsplit("#", 1)[-1])
        prs.append(
            ModelWatcherPr(
                ref=str(ref),
                repo=repo,
                number=number,
                ticket=ticket,
                title=str(facts.get("title") or ""),
                state=state,
                ci=verdict,
                ci_note=reason[:240] if verdict == "red" else "",
                ci_read_at=str(ci.get("read_at") or "") if ci else "",
                url=f"{pull_url_prefix}/{repo}/pull/{number}",
            )
        )
    fresh = age is not None and age <= max_age_seconds
    return tuple(prs), ModelSourceReading(
        id="watcher",
        title=title,
        ok=fresh,
        read_at=last_tick,
        age_seconds=age,
        detail=(
            f"{len(prs)} pull requests name a plan ticket"
            if fresh
            else f"the watcher's last tick is {age if age is not None else 'unknown'} s old, the limit is {max_age_seconds} s"
        ),
    )


def read_lanes(
    path: Path, max_age_seconds: int, now: datetime
) -> tuple[tuple[ModelDelegationLane, ...], ModelSourceReading]:
    doc, problem = _load_json(path)
    title = "Factory floor record"
    floor = doc.get("floor") if isinstance(doc, dict) else None
    lanes = floor.get("lanes") if isinstance(floor, dict) else None
    if problem or not isinstance(lanes, list):
        return (), ModelSourceReading(
            id="record",
            title=title,
            ok=False,
            detail=problem or "the record has no lane list",
        )
    generated = str(doc.get("generated_at") or "")
    age = _age(generated, now)
    out: list[ModelDelegationLane] = []
    for lane in lanes:
        if not isinstance(lane, dict) or not lane.get("ticket"):
            continue
        out.append(
            ModelDelegationLane(
                lane=str(lane.get("lane") or ""),
                ticket=str(lane["ticket"]),
                host=str(lane.get("host") or ""),
                liveness=str(lane.get("liveness") or ""),
                stalled=bool(lane.get("stalled")),
                started_at=str(lane.get("started_at") or ""),
            )
        )
    fresh = age is not None and age <= max_age_seconds
    return tuple(out), ModelSourceReading(
        id="record",
        title=title,
        ok=fresh,
        read_at=generated,
        age_seconds=age,
        detail=(
            f"{len(out)} lanes on tickets"
            if fresh
            else f"the record is {age if age is not None else 'unknown'} s old, the limit is {max_age_seconds} s"
        ),
    )


def run_command(argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout} s"
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def _json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def read_scenario(
    decl: ModelScenarioDecl,
    onex: Path,
    rt: ModelDelegationRuntime,
    run: CommandRunner,
    now: datetime,
) -> ModelScenarioRead:
    """The scheduled runs of one scenario's workflow through the source-control node, newest first."""
    at = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    argv = [
        str(onex),
        "scm",
        "run-list",
        decl.repo,
        "--workflow",
        decl.workflow,
        "--event",
        rt.scenario_event,
        "--limit",
        str(rt.scenario_run_limit),
    ]
    code, out, err = run(argv, rt.scenario_read_timeout_seconds)
    answer = _json_line(out)
    if answer is None:
        tail = (err or out).strip().splitlines()[-1:] or ["no answer"]
        return ModelScenarioRead(
            scenario=decl.id,
            read="unread",
            read_at=at,
            reason=f"exit {code}: {tail[0][:240]}",
        )
    result = answer.get("result")
    if not isinstance(result, dict):
        return ModelScenarioRead(
            scenario=decl.id,
            read="unread",
            read_at=at,
            reason="the answer has no result",
        )
    if answer.get("outcome") != "completed":
        reason = str(result.get("failure_reason") or answer.get("outcome") or "failed")
        kind: ScenarioReadKind = "no_workflow" if "HTTP 404" in reason else "unread"
        return ModelScenarioRead(
            scenario=decl.id, read=kind, read_at=at, reason=reason[:240]
        )
    runs = result.get("runs")
    if not isinstance(runs, list):
        return ModelScenarioRead(
            scenario=decl.id,
            read="unread",
            read_at=at,
            reason="the answer lists no runs",
        )
    parsed: list[ModelScenarioRun] = []
    for r in runs:
        if not isinstance(r, dict):
            continue
        parsed.append(
            ModelScenarioRun(
                run_id=int(r.get("id") or 0),
                status=str(r.get("status") or ""),
                conclusion=str(r.get("conclusion") or ""),
                event=str(r.get("event") or ""),
                created_at=str(r.get("created_at") or ""),
                updated_at=str(r.get("updated_at") or ""),
                url=str(r.get("html_url") or ""),
            )
        )
    return ModelScenarioRead(
        scenario=decl.id, read="read", read_at=at, runs=tuple(parsed)
    )


def read_lab_node_scenario(
    decl: ModelScenarioDecl,
    ledger: ProtocolLedgerPointRead,
    rt: ModelDelegationRuntime,
    now: datetime,
) -> ModelScenarioRead:
    """The scheduled results a lab node wrote as STATUS rows under its lane, read from the bus, newest first.

    Pages are followed to the end; a page that does not answer, or a read that reaches ``lab_node_max_pages`` with a cursor
    still open, is unread and carries no result, so a history that stopped early is never read as a pass. Only a row of this
    scenario with a scheduled trigger is a result (a manual run never fills a slot), and a rerun of a slot keeps its newest row.
    """
    at = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    since = now - timedelta(days=rt.lab_node_window_days)
    newest_per_slot: dict[datetime, Any] = {}
    cursor: str | None = None
    for number in range(1, rt.lab_node_max_pages + 1):
        answer = ledger.read(
            ModelPointQuery(
                query="rows",
                lanes=(decl.lane,),
                since=since,
                kinds=("STATUS",),
                page_size=rt.lab_node_rows_page_size,
                cursor=cursor,
            )
        )
        if answer.error:
            return ModelScenarioRead(
                scenario=decl.id,
                read="unread",
                read_at=at,
                reason=f"page {number} of lane {decl.lane} did not answer: {answer.error}"[
                    :240
                ],
            )
        for line in answer.lines:
            result = from_status_row(line)
            if (
                result is None
                or result.scenario_id != decl.id
                or result.trigger is not EnumScenarioTrigger.SCHEDULE
            ):
                continue
            kept = newest_per_slot.get(result.slot_start)
            if kept is None or result.recorded_at > kept.recorded_at:
                newest_per_slot[result.slot_start] = result
        cursor = answer.next_cursor
        if not cursor:
            break
    else:
        return ModelScenarioRead(
            scenario=decl.id,
            read="unread",
            read_at=at,
            reason=f"stopped after {rt.lab_node_max_pages} pages of lane {decl.lane} with more rows to read",
        )
    newest = sorted(newest_per_slot.values(), key=lambda r: r.slot_start, reverse=True)
    runs = tuple(
        ModelScenarioRun(
            run_id=int(r.slot_start.timestamp()),
            status="in_progress" if r.conclusion.unfinished else "completed",
            conclusion="" if r.conclusion.unfinished else r.conclusion.value,
            event=r.trigger.value,
            created_at=r.slot_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            updated_at=r.recorded_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            url="",
        )
        for r in newest[: rt.scenario_run_limit]
    )
    return ModelScenarioRead(scenario=decl.id, read="read", read_at=at, runs=runs)


def load_previous_reads(path: Path) -> dict[str, ModelScenarioRead]:
    """The scenario reads the last run kept, by scenario id; nothing when there is no usable file."""
    doc, problem = _load_json(path)
    if problem:
        return {}
    try:
        return {r.scenario: r for r in ModelScenarioReads.model_validate(doc).reads}
    except ValueError:
        return {}


def read_scenarios(
    rt: ModelDelegationRuntime,
    home: Path,
    now: datetime,
    run: CommandRunner = run_command,
    clock: Callable[[], datetime] | None = None,
    ledger: ProtocolLedgerPointRead | None = None,
) -> ModelScenarioReads:
    """Refresh the scenario reads one scenario at a time, the least recently read first, inside the stage deadline.

    A scenario whose overlay entry names the ``lab_node`` source is read from ``ledger``, the bus read of its node's STATUS
    rows; with no read route it is unread and the reason names the variable that would carry it.

    A scenario read within ``scenario_refresh_seconds`` is kept as it is; one whose read the deadline left no time for keeps
    its previous read, which the build then draws unread once it is older than ``scenario_reads_max_age_seconds``.
    """
    tick = clock or (lambda: datetime.now(UTC))
    kept = load_previous_reads(home / rt.scenario_reads_relpath)
    onex = home / rt.onex_relpath
    started = tick()
    result = dict(kept)
    stale_first = sorted(
        rt.scenarios, key=lambda d: kept[d.id].read_at if d.id in kept else ""
    )
    for decl in stale_first:
        previous = kept.get(decl.id)
        if previous is not None:
            try:
                age = (
                    now
                    - datetime.fromisoformat(previous.read_at.replace("Z", "+00:00"))
                ).total_seconds()
            except ValueError:
                age = float("inf")
            if age < rt.scenario_refresh_seconds:
                continue
        elapsed = (tick() - started).total_seconds()
        if (
            elapsed + rt.scenario_read_timeout_seconds
            > rt.scenario_stage_deadline_seconds
        ):
            break
        if decl.source == "lab_node":
            result[decl.id] = (
                read_lab_node_scenario(decl, ledger, rt, tick())
                if ledger is not None
                else ModelScenarioRead(
                    scenario=decl.id,
                    read="unread",
                    read_at=tick().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    reason=f"{rt.ledger_read_command_env} is not set; the lab node's results have no bus read route",
                )
            )
        else:
            result[decl.id] = read_scenario(decl, onex, rt, run, tick())
    return ModelScenarioReads(
        updated_at=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        reads=tuple(result[d.id] for d in rt.scenarios if d.id in result),
    )


def load_scenario_reads(
    path: Path, now: datetime
) -> tuple[ModelScenarioReads | None, ModelSourceReading]:
    doc, problem = _load_json(path)
    title = "Scenario workflow runs"
    if problem:
        return None, ModelSourceReading(
            id="scenario_reads", title=title, ok=False, detail=problem
        )
    try:
        reads = ModelScenarioReads.model_validate(doc)
    except ValueError as exc:
        return None, ModelSourceReading(
            id="scenario_reads",
            title=title,
            ok=False,
            detail=f"unreadable: {str(exc)[:160]}",
        )
    ages = [a for r in reads.reads if (a := _age(r.read_at, now)) is not None]
    oldest = max(ages) if ages else None
    unread = sum(1 for r in reads.reads if r.read == "unread")
    return reads, ModelSourceReading(
        id="scenario_reads",
        title=title,
        ok=True,
        read_at=reads.updated_at,
        age_seconds=_age(reads.updated_at, now),
        detail=f"{len(reads.reads)} workflows read, {unread} unread; the oldest read is {oldest if oldest is not None else 'unknown'} s old",
    )
