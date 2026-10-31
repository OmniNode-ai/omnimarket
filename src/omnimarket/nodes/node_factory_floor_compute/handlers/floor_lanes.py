# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Machine bays, live lanes and the cloud bay of the factory floor: pure functions over the request's facts."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorLedgerRow,
    ModelFloorMachineDecl,
    ModelFloorPr,
    ModelFloorReceipt,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorCloud,
    ModelFloorCloudAccountOut,
    ModelFloorCloudSession,
    ModelFloorLamp,
    ModelFloorLane,
    ModelFloorLine,
    ModelFloorMachine,
    ModelFloorRunnersOut,
    ModelFloorStationRef,
)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes(now: datetime, then: datetime) -> int:
    return max(0, int((now - then).total_seconds() // 60))


def _short(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _key(row: ModelFloorLedgerRow) -> tuple[datetime, str, str]:
    return row.ts, row.row_id, row.lane


def pr_key(value: str) -> str:
    """``OmniNode-ai/omnimarket#4702`` and ``omnimarket#4702`` are the same pull request."""
    return value.rsplit("/", 1)[-1]


def _lane_rows(
    request: ModelFactoryFloorRequest,
) -> dict[str, tuple[ModelFloorLedgerRow, ...]]:
    grouped: dict[str, list[ModelFloorLedgerRow]] = {}
    for row in request.ledger:
        if row.lane:
            grouped.setdefault(row.lane, []).append(row)
    return {lane: tuple(sorted(rows, key=_key)) for lane, rows in grouped.items()}


def _newest_receipts(
    request: ModelFactoryFloorRequest,
) -> dict[str, ModelFloorReceipt]:
    newest: dict[str, ModelFloorReceipt] = {}
    for receipt in request.receipts or ():
        held = newest.get(receipt.lane)
        if held is None or receipt.started_at > held.started_at:
            newest[receipt.lane] = receipt
    return newest


def _kind(claim: ModelFloorLedgerRow, request: ModelFactoryFloorRequest) -> str:
    actor, lane, text = (
        claim.actor.casefold(),
        claim.lane.casefold(),
        claim.text.casefold(),
    )
    for rule in request.config.lane_kinds:
        if (
            any(m.casefold() in actor for m in rule.actor_contains)
            or any(m.casefold() in lane for m in rule.lane_contains)
            or any(m.casefold() in text for m in rule.text_contains)
        ):
            return rule.kind
    return request.config.lane_kind_default


def _engine(model: str, request: ModelFactoryFloorRequest) -> str:
    if not model:
        return ""
    folded = model.casefold()
    return (
        "codex"
        if any(m.casefold() in folded for m in request.config.codex_model_markers)
        else "claude"
    )


def _station(
    lane_tickets: tuple[str, ...],
    pr: str,
    lines: tuple[ModelFloorLine, ...],
) -> ModelFloorStationRef | None:
    for line in lines:
        for station in line.stations:
            for item in station.items:
                if (pr and pr in item.prs) or set(lane_tickets) & set(item.tickets):
                    return ModelFloorStationRef(
                        line_id=line.line_id, station_id=station.id, item_id=item.id
                    )
    return None


def _first_sentence(text: str, limit: int) -> str:
    head = text.split(". ")[0].split("; ")[0]
    return _short(head, limit)


def build_lanes(
    request: ModelFactoryFloorRequest, lines: tuple[ModelFloorLine, ...]
) -> tuple[ModelFloorLane, ...]:
    """Every lane with a CLAIM no TERMINAL has closed and a row inside the live window."""
    now, config = request.generated_at, request.config
    window = config.lane_live_window_minutes
    receipts = _newest_receipts(request)
    titles = {issue.id: issue.title for issue in request.linear if issue.title}
    prs: dict[str, ModelFloorPr] = {pr.key: pr for pr in request.watcher.prs}
    declared = {m.host for m in request.machines}
    lanes: list[ModelFloorLane] = []
    for lane, rows in _lane_rows(request).items():
        claims = [r for r in rows if r.kind == "CLAIM"]
        if not claims:
            continue
        claim = max(claims, key=_key)
        if any(r.kind == "TERMINAL" and r.ts >= claim.ts for r in rows):
            continue
        newest = rows[-1]
        silent = _minutes(now, newest.ts)
        if silent > window:
            continue
        statuses = [r for r in rows if r.kind == "STATUS" and r.ts >= claim.ts]
        status = max(statuses, key=_key) if statuses else None
        status_row = status or claim
        status_full = status_row.text or status_row.reason
        receipt = receipts.get(lane)
        hosted = [r for r in rows if r.host and r.ts >= claim.ts]
        host = (
            receipt.host
            if receipt and receipt.host
            else max(hosted, key=_key).host
            if hosted
            else ""
        )
        cloud = host.startswith(config.cloud_host_prefix)
        placement = "cloud" if cloud else "machine" if host in declared else "unplaced"
        started = (
            receipt.started_at
            if receipt is not None
            and not receipt.final
            and receipt.started_at >= claim.ts
            else claim.ts
        )
        tickets = claim.tickets
        ticket = tickets[0] if tickets else ""
        linear_title = titles.get(ticket, "")
        title_text, source = (
            (linear_title, "linear")
            if linear_title
            else (claim.text, "claim")
            if claim.text
            else ("", "unread")
        )
        with_prs = [r for r in rows if r.prs and r.ts >= claim.ts]
        pr = pr_key(max(with_prs, key=_key).prs[0]) if with_prs else ""
        if not pr and receipt is not None and receipt.pr_heads:
            pr = pr_key(receipt.pr_heads[0])
        watched = prs.get(pr)
        lanes.append(
            ModelFloorLane(
                lane=lane,
                ticket=ticket,
                title=_short(
                    title_text
                    if source == "linear"
                    else _first_sentence(title_text, config.title_chars),
                    config.title_chars,
                ),
                title_source=source,
                kind=_kind(claim, request),
                engine=_engine(
                    (receipt.model if receipt else "") or claim.model, request
                ),
                placement=placement,
                host=host if placement == "machine" else "",
                cloud_account=(receipt.account if receipt and cloud else ""),
                started_at=_stamp(started),
                run_minutes=_minutes(now, started),
                status_text=_short(status_full, config.status_chars),
                status_full=status_full,
                status_at=_stamp(status_row.ts),
                status_age_min=_minutes(now, status_row.ts),
                status_source="status" if status else "claim",
                silent_minutes=silent,
                stalled=silent >= config.stall_minutes,
                pr=pr,
                pr_title=watched.title if watched else "",
                pr_state=watched.state if watched else ("unread" if pr else ""),
                pr_ci=watched.ci if watched else ("unread" if pr else ""),
                pr_ci_note=watched.ci_note if watched else "",
                station=_station(tickets, pr, lines),
                claim_full=claim.text,
            )
        )
    ordered = sorted(
        lanes, key=lambda item: (item.stalled, item.silent_minutes, item.lane)
    )
    return tuple(ordered[: config.lane_limit])


def _runners(
    decl: ModelFloorMachineDecl, request: ModelFactoryFloorRequest
) -> ModelFloorRunnersOut:
    fleet = request.runner_fleet
    if fleet is None:
        return ModelFloorRunnersOut(
            state="unread",
            total=None,
            online=None,
            busy=None,
            observed_at="",
            note="no runner fleet observation has been recorded",
        )
    if not decl.runner_labels:
        return ModelFloorRunnersOut(
            state="unread",
            total=None,
            online=None,
            busy=None,
            observed_at=_stamp(fleet.observed_at),
            note="the overlay declares no runner label for this machine",
        )
    mine = [r for r in fleet.runners if r.host_label in decl.runner_labels]
    return ModelFloorRunnersOut(
        state="read",
        total=len(mine),
        online=sum(r.status != "offline" for r in mine),
        busy=sum(r.status == "busy" for r in mine),
        observed_at=_stamp(fleet.observed_at),
        note="",
    )


def build_machines(
    request: ModelFactoryFloorRequest, lanes: tuple[ModelFloorLane, ...]
) -> tuple[ModelFloorMachine, ...]:
    now, config = request.generated_at, request.config
    readings = {r.host: r for r in request.pool or ()}
    table = {row.host: row for row in request.host_table or ()}
    placed = {p.host: p for p in request.placed or ()}
    machines: list[ModelFloorMachine] = []
    for decl in request.machines:
        row, reading = table.get(decl.host), readings.get(decl.host)
        mine = tuple(
            lane.lane
            for lane in lanes
            if lane.placement == "machine" and lane.host == decl.host
        )
        marker = placed.get(decl.host)
        units = (
            (marker.units if marker is not None else 0)
            if request.placed is not None
            else None
        )
        unit_work = (
            max(0, units - (marker.lanes if marker is not None else 0))
            if units is not None
            else None
        )
        age = _minutes(now, reading.read_at) if reading is not None else None
        stale = age is not None and age > config.pool_stale_minutes
        if row is not None and not row.available:
            state, note = (
                "repair",
                _short(
                    row.unavailable_reason.split(";")[0], config.repair_reason_chars
                ),
            )
        elif request.pool is None:
            state, note = "unread", "no pool read has been recorded"
        elif reading is None:
            state, note = "unread", "the pool read held no row for this machine"
        elif reading.error:
            state, note = "unread", _short(reading.error, config.note_chars)
        else:
            state, note = "read", ""
        measured = reading if state == "read" and reading is not None else None
        cores = measured.cores if measured else None
        load = measured.load1 if measured else None
        machines.append(
            ModelFloorMachine(
                host=decl.host,
                label=decl.label,
                state=state,
                note=note,
                read_at=_stamp(reading.read_at) if reading else "",
                read_age_min=age,
                stale=stale,
                cores=cores,
                load1=load,
                load_per_core=round(load / cores, 2)
                if load is not None and cores
                else None,
                busy_cores=measured.busy_cores if measured else None,
                mem_free_gb=measured.mem_free_gb if measured else None,
                mem_total_gb=measured.mem_total_gb if measured else None,
                swap_used_pct=measured.swap_used_pct if measured else None,
                cap=(
                    row.cap
                    if row is not None and row.cap is not None
                    else (reading.cap if reading else None)
                ),
                refusal=measured.refusal if measured else "",
                lanes_placed=len(mine),
                lanes=mine,
                units_placed=units,
                unit_work=unit_work,
                runners=_runners(decl, request),
            )
        )
    return tuple(machines)


def build_cloud(request: ModelFactoryFloorRequest) -> ModelFloorCloud:
    now, config = request.generated_at, request.config
    declared = {m.host for m in request.machines}
    cloud_receipts = sorted(
        (
            r
            for r in request.receipts or ()
            if r.host.startswith(config.cloud_host_prefix) and r.account in declared
        ),
        key=lambda r: r.started_at,
        reverse=True,
    )
    sessions = tuple(
        ModelFloorCloudSession(
            lane=r.lane,
            run_id=r.run_id,
            account=r.account,
            started_at=_stamp(r.started_at),
            run_minutes=_minutes(now, r.started_at),
            status=r.status,
            open=not r.final,
            result=_short(r.session_detail or r.result, config.status_chars),
        )
        for r in cloud_receipts[: config.cloud_sessions_limit]
    )
    finished = [r for r in cloud_receipts if r.final]
    newest = max(finished, key=lambda r: r.ended_at or r.started_at, default=None)
    lamps: tuple[ModelFloorLamp, ...] = ()
    if newest is not None and newest.status in config.cloud_no_push_statuses:
        lamps = (
            ModelFloorLamp(
                id="cloud-no-push",
                text=config.cloud_no_push_lamp_text,
                since=_stamp(newest.ended_at or newest.started_at),
                evidence=_short(
                    " ".join(
                        p for p in (newest.session_detail, newest.session_needs) if p
                    )
                    or newest.result,
                    config.note_chars,
                ),
            ),
        )
    if request.cloud_accounts is None:
        return ModelFloorCloud(
            state="unread",
            note="the cloud accounts file could not be read",
            accounts=(),
            sessions=sessions,
            lamps=lamps,
        )
    accounts = tuple(
        ModelFloorCloudAccountOut(
            name=a.name,
            ready=a.login in config.cloud_ready_logins,
            login=a.login,
            cap=a.cap,
            open_sessions=len(a.active),
            credit_remaining=a.credit_remaining,
            credit_read_at=a.credit_read_at,
            rate_status=a.rate_status,
            rate_resets_at=a.rate_resets_at,
        )
        for a in sorted(request.cloud_accounts, key=lambda a: a.name)
        if a.name in declared and a.login in config.cloud_ready_logins
    )
    others = sum(
        1
        for a in request.cloud_accounts
        if a.name in declared and a.login not in config.cloud_ready_logins
    )
    return ModelFloorCloud(
        state="read",
        note=f"{others} listed machines hold no cloud login" if others else "",
        accounts=accounts,
        sessions=sessions,
        lamps=lamps,
    )
