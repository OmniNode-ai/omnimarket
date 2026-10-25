# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Close eligible released lease claims once, recording refused writes for retry."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

from omnimarket.handlers.node_overlay_reader import resolve_setting

from ..models.model_landing_lease_claim_close import (
    ModelLeaseClaimCloseRequest,
    ModelLeaseClaimCloseResult,
    utc_stamp,
)

CAUSE_RELAY_FILE = "cause-relay.json"
WORKER_RELAY_FILE = "relay.json"
LEDGER_CLOSE_FILE = "ledger-close.json"
NOT_STARTED_FILE = "not-started.json"
NO_RESULT_FILE = "no-result-recorded.json"
CONTROLLER_ACTOR = "script:landing-controller"
LANDING_CONTROLLER_OVERLAY = "landing_controller"
LANDING_CONTROLLER_FIELDS = frozenset({"cause_ticket", "fallback_ticket"})


def cause_ticket(environ: Mapping[str, str] | None = None) -> str:
    return resolve_setting(
        LANDING_CONTROLLER_OVERLAY,
        "cause_ticket",
        fields=LANDING_CONTROLLER_FIELDS,
        environ=environ,
    )


def fallback_ticket(environ: Mapping[str, str] | None = None) -> str:
    return resolve_setting(
        LANDING_CONTROLLER_OVERLAY,
        "fallback_ticket",
        fields=LANDING_CONTROLLER_FIELDS,
        environ=environ,
    )


CAUSE_LANE_PREFIX = "landing-cause-L"
LANE_RE = re.compile("^landing-(?:cause-)?L(\\d+)$")
RECENT = timedelta(hours=3)
CLOSE_REASON = "delegated=0 delegation_reason=controller-lease-close"


def _cell(value: object) -> str:
    return " ".join(str(value).replace("|", "/").split())


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _when(value: object) -> datetime | None:
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def lease_dir(state_dir: Path, lease_id: int) -> Path:
    return state_dir / "workers" / f"{lease_id:06d}"


def write_relay(directory: Path, relay: Mapping[str, Any]) -> None:
    """The worker's relay record, written the moment its CLAIM lands and again with its TERMINAL."""
    (directory / WORKER_RELAY_FILE).write_text(
        json.dumps(relay, sort_keys=True) + "\n", encoding="utf-8"
    )


def backfill_parser() -> argparse.ArgumentParser:
    """Parse options for closing eligible claims from a bounded ledger read."""
    ap = argparse.ArgumentParser(prog="landing_controller.py close-lease-claims")
    ap.add_argument("--since-days", type=float, default=14.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--omni-home", type=Path, default=None)
    return ap


def kill_exits() -> dict[int, Any]:
    """Make SIGTERM and SIGINT raise ``SystemExit`` so a ``finally`` that writes the closing row runs on a kill.

    The first signal raises and ignores any later one, so the ``finally`` itself is not cut short; the controller's
    kill sequence still ends the process with SIGKILL after its wait. Returns the previous handlers for
    ``restore_signals``; off the main thread it installs nothing and returns ``{}``."""
    previous: dict[int, Any] = {}

    def stop(signum: int, _frame: FrameType | None) -> None:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, stop)
    except ValueError:
        restore_signals(previous)
        return {}
    return previous


def restore_signals(previous: Mapping[int, Any]) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def cause_relay(directory: Path) -> tuple[Path, dict[str, Any]] | None:
    """Read the cause relay record, falling back to a worker relay that still names a cause lane."""
    for name in (CAUSE_RELAY_FILE, WORKER_RELAY_FILE):
        relay = _read(directory / name)
        if isinstance(relay, dict) and str(relay.get("lane") or "").startswith(
            CAUSE_LANE_PREFIX
        ):
            return (directory / name, relay)
    return None


def cause_claim(
    directory: Path,
    action: Mapping[str, Any],
    base_tip: str,
    writer: Callable[[str], tuple[bool, str]],
    clock: Callable[[], str] = utc_stamp,
) -> tuple[bool, str]:
    """Write a cause CLAIM and record its acceptance in the separate cause relay file."""
    lid = int(action["lease_id"])
    brief = action.get("cause_brief") or action.get("brief") or {}
    key = str(action["subject"])
    checks = sorted(
        {
            str(p.get("check"))
            for p in brief.get("pairs") or ()
            if isinstance(p, dict) and p.get("check")
        }
    )
    members = list(brief.get("members") or ())
    engine = _cell(brief.get("engine") or "claude_opus")
    lane = f"{CAUSE_LANE_PREFIX}{lid}"
    now = clock()
    ticket = cause_ticket()
    row = " | ".join(
        [
            now,
            "CLAIM",
            f"lane={lane}",
            f"ticket={ticket}",
            f"actor={CONTROLLER_ACTOR}",
            f"model={engine}",
            f"cause={_cell(key)}",
            f"check={_cell(','.join(checks)) or 'unread'}",
            *([f"base={base_tip}"] if base_tip else []),
            "relay=landing-controller",
            "worktree=none",
            f"est ~1.5 lane-hours; displaces per-PR fix workers on {len(members)} member PRs; ({ticket})",
            f"landing cause worker for {_cell(key)} over {len(members)} member PRs (engine {engine}); the controller host writes this lane's rows",
        ]
    )
    ok, err = writer(row)
    directory.mkdir(parents=True, exist_ok=True)
    _write(
        directory / CAUSE_RELAY_FILE,
        {
            "lane": lane,
            "cause": key,
            "base": base_tip,
            "claim": {"ts": now, "ok": ok, "error": "" if ok else err},
        },
    )
    return (ok, err)


def cause_terminal(
    directory: Path,
    writer: Callable[[str], tuple[bool, str]],
    clock: Callable[[], str] = utc_stamp,
) -> None:
    """The cause lane's TERMINAL once its lease is released: hands the cause back to the controller. A refused append
    is recorded and tried again on the next call; one that landed is never written twice."""
    found = cause_relay(directory)
    if found is None:
        return
    path, relay = found
    if not (relay.get("claim") or {}).get("ok") or (relay.get("terminal") or {}).get(
        "ok"
    ):
        return
    result = _read(directory / "result.json")
    kind = (
        str(result.get("kind"))
        if isinstance(result, dict) and result.get("kind")
        else "none"
    )
    now = clock()
    ticket = cause_ticket()
    row = " | ".join(
        [
            now,
            "TERMINAL",
            f"lane={relay['lane']}",
            f"ticket={ticket}",
            f"actor={CONTROLLER_ACTOR}",
            "model=none",
            "friction=none",
            "outcome=handed-off",
            "handed_to=landing-controller",
            f"closes-CLAIM={relay['claim']['ts']}",
            f"cause={_cell(relay.get('cause') or '')}",
            f"result={_cell(kind)}",
            *([f"base={relay['base']}"] if relay.get("base") else []),
            *(
                [f"reason={_cell(result['reason'])}"]
                if isinstance(result, dict) and result.get("reason")
                else []
            ),
            "cause lease released; the controller reads its result file",
        ]
    )
    ok, err = writer(row)
    relay["terminal"] = {"ts": now, "ok": ok, "error": "" if ok else err}
    _write(path, relay)


def _kind(path: Path) -> str:
    raw = _read(path)
    return _cell(raw["kind"]) if isinstance(raw, dict) and raw.get("kind") else ""


def _relay_closed(directory: Path, lane: str, claim_ts: str) -> bool:
    """The lane's own relay record says its TERMINAL for this CLAIM landed (the read may predate the row)."""
    for name in (CAUSE_RELAY_FILE, WORKER_RELAY_FILE):
        relay = _read(directory / name)
        if (
            not isinstance(relay, dict)
            or relay.get("lane") != lane
            or (not (relay.get("terminal") or {}).get("ok"))
        ):
            continue
        if str((relay.get("claim") or {}).get("ts") or claim_ts) == claim_ts:
            return True
    return False


def close_row(
    claim: Mapping[str, Any],
    lease_id: int,
    directory: Path,
    revoked: bool,
    released: str,
    now: str,
    *,
    cause_ticket_override: str | None = None,
    fallback_ticket_override: str | None = None,
) -> str:
    """The closing TERMINAL, in the shape ``cause_terminal`` writes."""
    result, no_result = (
        _kind(directory / "result.json"),
        _kind(directory / NO_RESULT_FILE),
    )
    not_started = (directory / NOT_STARTED_FILE).exists()
    if not_started:
        end = "not_started"
    elif revoked:
        end = "revoked"
    elif result:
        end = "result"
    elif no_result:
        end = "no_result"
    else:
        end = "released"
    kind = result or no_result or ("not_started" if not_started else "none")
    lane = str(claim["lane"])
    ticket_value = claim.get("ticket")
    if not ticket_value:
        if lane.startswith(CAUSE_LANE_PREFIX):
            ticket_value = (
                cause_ticket_override
                if cause_ticket_override is not None
                else cause_ticket()
            )
        else:
            ticket_value = (
                fallback_ticket_override
                if fallback_ticket_override is not None
                else fallback_ticket()
            )
    ticket = _cell(ticket_value)
    scope = (
        f"cause={_cell(claim['cause'])}"
        if claim.get("cause")
        else f"pr={_cell(claim['pr'])}"
        if claim.get("pr")
        else ""
    )
    return " | ".join(
        [
            now,
            "TERMINAL",
            f"lane={lane}",
            f"ticket={ticket}",
            f"actor={CONTROLLER_ACTOR}",
            "model=none",
            "friction=none",
            "outcome=handed-off",
            "handed_to=landing-controller",
            f"closes-CLAIM={claim['ts']}",
            f"lease=L{lease_id}",
            f"lease_end={end}",
            f"result={kind}",
            *([scope] if scope else []),
            CLOSE_REASON,
            f"lease ended {released or 'unknown'}; the controller closes the CLAIM its worker or relay left open",
        ]
    )


class HandlerLandingLeaseClaimClose:
    def handle(
        self, request: ModelLeaseClaimCloseRequest
    ) -> ModelLeaseClaimCloseResult:
        read_at = _when(request.read_at)
        if read_at is None:
            raise ValueError(
                f"ledger read time {request.read_at!r} is not a ledger stamp"
            )
        live = skipped_recent = skipped_closed = 0
        eligible: list[tuple[dict[str, Any], int, Path, str]] = []
        for claim in sorted(
            request.claims,
            key=lambda c: (str(c.get("ts") or ""), str(c.get("lane") or "")),
        ):
            lane, ts = (str(claim.get("lane") or ""), str(claim.get("ts") or ""))
            m = LANE_RE.match(lane)
            if (
                not m
                or not ts
                or str(claim.get("actor") or CONTROLLER_ACTOR) != CONTROLLER_ACTOR
            ):
                continue
            lid = int(m[1])
            directory = lease_dir(request.state_dir, lid)
            lease = _read(directory / "lease.json")
            released_at = (
                str(lease.get("released_at") or "") if isinstance(lease, dict) else ""
            )
            released = _when(released_at)
            if (
                lid in request.live_lease_ids
                or lid in request.released_this_tick
                or (released and released >= read_at)
            ):
                live += 1
                continue
            record = _read(directory / LEDGER_CLOSE_FILE)
            if (
                isinstance(record, dict) and (record.get(lane) or {}).get("ok")
            ) or _relay_closed(directory, lane, ts):
                skipped_closed += 1
                continue
            claimed = _when(ts)
            if released is None and (claimed is None or read_at - claimed < RECENT):
                skipped_recent += 1
                continue
            eligible.append((claim, lid, directory, released_at))
        limit = len(eligible) if request.limit is None else max(0, request.limit)
        rows: list[str] = []
        closed: list[str] = []
        failed: list[str] = []
        for claim, lid, directory, released_at in eligible[:limit]:
            lane = str(claim["lane"])
            row = close_row(
                claim,
                lid,
                directory,
                lid in request.revoked_lease_ids,
                released_at,
                request.clock(),
                cause_ticket_override=request.cause_ticket,
                fallback_ticket_override=request.fallback_ticket,
            )
            rows.append(row)
            if request.dry_run:
                closed.append(lane)
                continue
            ok, err = request.writer(row)
            (closed if ok else failed).append(lane)
            record = _read(directory / LEDGER_CLOSE_FILE)
            record = record if isinstance(record, dict) else {}
            record[lane] = {
                "ts": row.split(" | ", 1)[0],
                "ok": ok,
                "error": "" if ok else str(err)[:300],
            }
            directory.mkdir(parents=True, exist_ok=True)
            _write(directory / LEDGER_CLOSE_FILE, record)
        return ModelLeaseClaimCloseResult(
            rows=tuple(rows),
            closed=tuple(closed),
            failed=tuple(failed),
            skipped_live=live,
            skipped_recent=skipped_recent,
            skipped_closed=skipped_closed,
            left=len(eligible) - min(limit, len(eligible)),
        )
