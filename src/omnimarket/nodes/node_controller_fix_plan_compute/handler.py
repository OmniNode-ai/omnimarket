"""Controller repair lane routing and bounded brief construction (OMN-19998)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    require_lane_model,
    resolve_lane_model,
)

from .models import ModelControllerFixPlanRequest, ModelControllerFixPlanResult


def select_target(
    request: Mapping[str, Any], contract: Mapping[str, Any], controller_host: str | None
) -> tuple[dict[str, Any], str | None, str]:
    """Use the tick's runner admission read; an exhausted Claude pool falls through to Codex (OMN-19998).

    Prefer the controller host when admitted, then the host with the most admitted slots. The runner still
    rechecks capacity before placing. An absent or failed pool read preserves the normal runner preflight;
    it is not evidence that Claude is exhausted. Both the brief's route and the runner's model must agree.
    """
    selected = dict(contract)

    def select_model(model: str) -> None:
        route = str(contract["controller_fix_route"])
        for key, value in (
            ("model", model),
            ("dest", f"{model}-lane"),
            ("effort", "high"),
        ):
            route = re.sub(rf"\b{key}=\S+", f"{key}={value}", route)
        selected.update(
            controller_fix_model=model,
            controller_fix_effort="high",
            controller_fix_route=route,
        )

    select_model(
        require_lane_model(
            requested_engine=str(contract["controller_fix_model"]),
            lane_kind="controller-fix",
        )
    )
    pool = request.get("lab_headroom")
    if (
        not isinstance(pool, Mapping)
        or not pool.get("live_admission")
        or pool.get("placement_error")
    ):
        return selected, controller_host, ""
    hosts = pool.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        return selected, controller_host, ""

    def pick(engine: str) -> str | None:
        eligible: list[tuple[bool, int, str]] = []
        for host in hosts:
            if not isinstance(host, Mapping) or not host.get("name"):
                continue
            admission = host.get("admission") or {}
            if (
                not isinstance(admission, Mapping)
                or admission.get("probe_error")
                or admission.get(f"{engine}_refusal")
            ):
                continue
            slots = admission.get(f"{engine}_slots")
            if type(slots) is int and slots > 0:
                name = str(host["name"])
                eligible.append((name == controller_host, slots, name))
        return max(eligible)[2] if eligible else None

    targets = {engine: pick(engine) for engine in ("codex", "claude")}
    decision = resolve_lane_model(
        lane_kind="controller-fix",
        available_engines=tuple(
            engine for engine, host in targets.items() if host is not None
        ),
    )
    if decision.model is not None and decision.engine is not None:
        select_model(decision.model)
        host = targets[decision.engine]
        if controller_host and host != controller_host:
            selected["controller_evidence_host"] = controller_host
        return selected, host, ""
    return (
        selected,
        None,
        "no admitted Claude or Codex host for the controller fix lane; the next tick tries again",
    )


def build_brief(
    defects: list[dict[str, Any]],
    history: Mapping[str, list[str]],
    contract: Mapping[str, Any],
    lane: str,
    bound_min: float,
) -> str:
    shown = [
        SimpleNamespace(**d)
        for d in defects[: int(contract["controller_fix_brief_prs"])]
    ]
    facts = "\n".join(
        f"- {d.key} head {d.head} green for {d.age:.0f}m, merge_state {d.merge_state}, "
        f"{'armed' if d.armed else 'not armed'}, not merged or queued: {d.title[:100]}"
        for d in shown
    )
    hist = "\n".join(
        f"{d.key}:\n" + "\n".join(f"  {line}" for line in history.get(d.key, []))
        for d in shown
    )
    more = (
        f"\n(+{len(defects) - len(shown)} more green heads past the bound, oldest first above)"
        if len(defects) > len(shown)
        else ""
    )
    evidence = (
        f"Controller evidence host: {contract['controller_evidence_host']}. This lane runs on another admitted "
        "host; the controller files below belong to the evidence host, and the decision history above was read "
        "there. Use the host table for access; missing files on your executing host do not mean the controller "
        "state is absent.\n"
        if contract.get("controller_evidence_host")
        else ""
    )
    return (
        f"# Lane {lane}: fix the landing controller defect that leaves green PRs unlanded ({contract['ticket']})\n"
        f"{contract['controller_fix_route']}\n"
        f"Authority: {contract['ruling']} and RULING 2026-10-17 (OMN-19998): a PR green on every check and not merged or "
        "queued past the bound is a CONTROLLER_DEFECT, fixed in the controller; the throughput tick dispatched this lane "
        "by itself and a person is not waited on.\n"
        f"CONTROLLER_DEFECT facts at dispatch ({len(defects)} PRs green on every check, unlanded for more than "
        f"{bound_min:g} minutes):\n{facts}{more}\n"
        f"Decision history (decisions.jsonl rows for each PR, then the newest tick's reason):\n{hist}\n"
        f"{evidence}"
        "Kind fix: read why the landing controller did not merge or enqueue these heads (its decisions.jsonl, ticks.jsonl, "
        "state.json and workers/<lease>/result.json are under ~/.local/state/omni/landing on its host; read PRs only "
        "with pr_state_local.py) and fix that cause in the controller, with TDD, one pull request, hand-off needs=land. "
        "Before the first write, read the ledger for a live CLAIM on the same controller defect (for example a lane "
        "named controller-green-not-landed) and extend that lane's work instead of duplicating it; if one covers the "
        "cause, report that and stop. Merging, arming, re-running or updating a PR by hand is out of scope: the fix is "
        "the controller's.\n"
        "This is the one live controller fix lane: the tick dispatches no second while you run, and groups every PR that "
        "turns green-unlanded, or whose head moves, into you. Before you report, read each repository's open PRs again "
        "with pr_state_local.py and cover every PR green on every check and not merged or queued past the bound, not "
        "only the ones listed here; a PR in a merge queue is landing and is not yours.\n"
        "Report once at terminal: the cause, the rows that show it, and the pull request that fixes it.\n"
    )


# OMN-19998: a lane that ended one of these ways never ran its brief, so it is not live and the heads recorded against
# it are sent again: controller-fix-green-unlanded-20261019T0043Z ended no-host four times, then no-result on an
# unreachable host, and the four green heads recorded against it were never sent again.
DEAD_STATUSES = frozenset({"no-host", "no-result", "placement-lost", "interrupted"})
DEAD_TERMINAL = re.compile(r"\bstatus=(" + "|".join(sorted(DEAD_STATUSES)) + r")\b")


def _when(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def read_lanes(
    now: datetime,
    dispatched: Mapping[str, Any],
    idents: Mapping[str, str],
    receipts: Mapping[str, Mapping[str, Any]],
    ledger_lanes: Mapping[str, Mapping[str, Any]],
    live_min: float,
) -> tuple[str | None, list[str], list[str]]:
    """(the newest live controller fix lane, the lanes that ended without running their brief, the defect keys whose
    recorded lane is one of those). Live: dispatched (``dispatched``, a grouped head excepted, since it records when it
    was grouped), with a runner receipt not marked final, or with a ledger CLAIM and no TERMINAL, started within
    ``live_min`` minutes. Dead: a final newest receipt whose status, or a TERMINAL whose ``status=``, is in
    ``DEAD_STATUSES``."""
    started: dict[str, datetime] = {}
    ended: set[str] = set()
    dead: set[str] = set()

    def start(lane: str, at: datetime | None) -> None:
        if at is not None:
            started[lane] = max(at, started.get(lane, at))

    for entry in dispatched.values():
        if (
            isinstance(entry, Mapping)
            and entry.get("lane")
            and not entry.get("grouped")
        ):
            start(str(entry["lane"]), _when(entry.get("at")))
    for lane, rec in receipts.items():
        start(lane, _when(rec.get("started")))
        if rec.get("final"):
            ended.add(lane)
            if str(rec.get("status") or "") in DEAD_STATUSES:
                dead.add(lane)
    for lane, rec in ledger_lanes.items():
        start(lane, _when(rec.get("claim")))
        if rec.get("terminal"):
            ended.add(lane)
            if DEAD_TERMINAL.search(str(rec["terminal"])):
                dead.add(lane)
    cutoff, ahead = now - timedelta(minutes=live_min), now + timedelta(minutes=5)
    live = [
        (at, lane)
        for lane, at in started.items()
        if lane not in ended and cutoff <= at <= ahead
    ]
    resend = [
        key
        for key, ident in idents.items()
        if (dispatched.get(ident) or {}).get("lane") in dead
    ]
    return (max(live)[1] if live else None), sorted(dead), sorted(resend)


class HandlerControllerFixPlan:
    """Pure request-to-plan handler, used by the command topic and the CLI bus runtime."""

    def handle(
        self, request: ModelControllerFixPlanRequest
    ) -> ModelControllerFixPlanResult:
        if request.operation == "target":
            contract, host, error = select_target(
                {"lab_headroom": request.lab_headroom},
                request.contract,
                request.controller_host,
            )
            return ModelControllerFixPlanResult(
                contract=contract, host=host, error=error
            )
        if request.operation == "lanes":
            now = _when(request.now) or datetime.now(UTC)
            live, dead, resend = read_lanes(
                now,
                request.dispatched,
                request.idents,
                request.receipts,
                request.ledger_lanes,
                request.live_min,
            )
            return ModelControllerFixPlanResult(live=live, dead=dead, resend=resend)
        return ModelControllerFixPlanResult(
            brief=build_brief(
                request.defects,
                request.history,
                request.contract,
                request.lane,
                request.bound_min,
            )
        )
