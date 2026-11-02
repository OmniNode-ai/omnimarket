# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stop a running remote lane by name and close its ledger record once.

The lane's newest receipt names its host, pid, run and cloud session; nothing else does. A lab
lane is signalled over the runner's ssh transport (polite signal, bounded wait, forceful signal,
bounded wait), a cloud lane through the cloud client's archive call, a local lane through the
receipt's pid. Only after the process tree is gone does the node write the lane's TERMINAL
``outcome=stopped`` through the ledger writer. The writer refuses a second TERMINAL for one
closed CLAIM: that refusal is the single-closer rule of the stop model, and the loser reports
``already-ended``. The node runs no git or gh and never touches the lane's branch, worktree or PR.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_remote_lane_stop_effect.models.model_remote_lane_stop import (
    EnumStopOutcome,
    ModelRemoteLaneStopRequest,
    ModelRemoteLaneStopResult,
    ModelStopHost,
)
from omnimarket.nodes.node_remote_lane_stop_effect.protocols.local_remote_lane_stop_adapters import (
    CommandStopCloud,
    LocalStopLedger,
    LocalStopTransport,
)
from omnimarket.nodes.node_remote_lane_stop_effect.protocols.protocol_remote_lane_stop import (
    ProtocolStopCloud,
    ProtocolStopLedger,
    ProtocolStopTransport,
    RemoteLaneStopPortError,
)

NOT_FINAL_STATUSES = frozenset(
    {"", "placing", "preparing", "running", "queued", "submitted"}
)
SIDE_FILES = (".job.json", ".finished.json")
TEXT_MAX = 300


def _cells(row: str) -> dict[str, str]:
    parts = [p.strip() for p in row.split(" | ")]
    cells = {"ts": parts[0], "type": parts[1] if len(parts) > 1 else ""}
    for part in parts[2:]:
        key, sep, value = part.partition("=")
        if sep and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
            cells.setdefault(key, value)
    return cells


def newest_receipt(receipt_dir: Path, lane: str) -> dict[str, Any] | None:
    """The lane's newest receipt: ``<dir>/<lane>/<run id>.json``, latest start first."""
    found: list[tuple[str, float, dict[str, Any]]] = []
    for path in sorted((receipt_dir / lane).glob("*.json")):
        if path.name.endswith(SIDE_FILES):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("run_id") and data.get("lane") == lane:
            stamp = str(data.get("started_at") or data.get("submitted_at") or "")
            found.append((stamp, path.stat().st_mtime, data))
    return max(found, key=lambda item: (item[0], item[1]))[2] if found else None


def _section(receipt: dict[str, Any], key: str) -> dict[str, Any]:
    value = receipt.get(key)
    return value if isinstance(value, dict) else {}


def _lane_rows(ledger_text: str, lane: str) -> list[dict[str, str]]:
    rows = []
    for line in ledger_text.splitlines():
        if " | CLAIM | " in line or " | TERMINAL | " in line:
            cells = _cells(line)
            if cells.get("lane") == lane:
                rows.append(cells)
    return rows


def _claim_and_closer(
    rows: list[dict[str, str]], run_id: str
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    claims = [r for r in rows if r["type"] == "CLAIM"]
    own = [r for r in claims if r.get("run") == run_id] or claims
    claim = max(own, key=lambda r: r["ts"]) if own else None
    if claim is None:
        return None, None
    for row in rows:
        if row["type"] != "TERMINAL":
            continue
        named = row.get("closes-CLAIM") or row.get("closes")
        if named == claim["ts"] or (named is None and row["ts"] > claim["ts"]):
            return claim, row
    return claim, None


def _result(
    outcome: EnumStopOutcome, message: str, lane: str, **fields: Any
) -> ModelRemoteLaneStopResult:
    return ModelRemoteLaneStopResult(
        outcome=outcome, message=message, lane=lane, **fields
    )


class HandlerRemoteLaneStop:
    """``handle(request) -> result``: the ports are the constructor's, so tests run on fakes."""

    def __init__(
        self,
        transport: ProtocolStopTransport,
        ledger: ProtocolStopLedger,
        cloud: ProtocolStopCloud | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._ledger = ledger
        self._cloud = cloud
        self._clock = clock
        self._sleep = sleep

    def handle(self, request: ModelRemoteLaneStopRequest) -> ModelRemoteLaneStopResult:
        lane = request.lane
        receipt = newest_receipt(Path(request.receipt_dir), lane)
        if receipt is None:
            return _result(
                "unknown-lane",
                f"no receipt for lane {lane} under {request.receipt_dir}; no signal sent",
                lane,
            )
        run_id = str(receipt["run_id"])
        host_name = str(receipt.get("host") or "")
        status = str(receipt.get("status") or "")
        base = {"run_id": run_id, "host": host_name or None}
        try:
            rows = _lane_rows(self._ledger.read_text(request.ledger_path), lane)
            claim, closer = _claim_and_closer(rows, run_id)
            if receipt.get("final") is True or status not in NOT_FINAL_STATUSES:
                return self._ended(
                    lane, run_id, f"receipt status={status or 'final'}", closer, base
                )
            if closer is not None:
                return self._ended(
                    lane, run_id, f"receipt status={status}", closer, base
                )
            if claim is None:
                return _result(
                    "refused",
                    f"lane {lane} has no CLAIM on the ledger to close; no signal sent",
                    lane,
                    **base,
                )
            return self._stop(request, receipt, claim, base)
        except RemoteLaneStopPortError as exc:
            return _result(
                "refused", f"stop of lane {lane} refused: {exc}", lane, **base
            )

    def _ended(
        self,
        lane: str,
        run_id: str,
        why: str,
        closer: dict[str, str] | None,
        base: dict[str, Any],
    ) -> ModelRemoteLaneStopResult:
        row = (
            f"; ledger TERMINAL {closer['ts']} outcome={closer.get('outcome', '?')}"
            if closer
            else ""
        )
        return _result(
            "already-ended",
            f"lane {lane} (run {run_id}) already ended: {why}{row}; no signal sent",
            lane,
            **base,
        )

    def _wait(
        self, alive: Callable[[], str | None], budget_s: float, interval_s: float
    ) -> str | None:
        """The survivor's name once the budget is spent, or None when the tree is gone."""
        deadline = self._clock() + budget_s
        while True:
            survivor = alive()
            if survivor is None:
                return None
            if self._clock() >= deadline:
                return survivor
            self._sleep(interval_s)

    def _stop(
        self,
        request: ModelRemoteLaneStopRequest,
        receipt: dict[str, Any],
        claim: dict[str, str],
        base: dict[str, Any],
    ) -> ModelRemoteLaneStopResult:
        lane = request.lane
        run_id = str(receipt["run_id"])
        host_name = str(receipt.get("host") or "")
        pid = receipt.get("pid")
        signals: list[str] = []
        cloud_lane = receipt.get("placement") == "cloud" or host_name.startswith(
            "cloud:"
        )
        if cloud_lane:
            survivor = self._stop_cloud(request, receipt, signals)
        else:
            host = next((h for h in request.hosts if h.name == host_name), None)
            if host is None:
                raise RemoteLaneStopPortError(
                    f"the request gives no transport for host {host_name!r}"
                )
            survivor = self._stop_process(request, host, run_id, pid, receipt, signals)
        if survivor is not None:
            return _result(
                "refused",
                f"lane {lane} not stopped: survivor {survivor} outlived {'+'.join(signals)}; no row written",
                lane,
                survivor=survivor,
                signals=signals,
                **base,
            )
        if request.placement_dir and isinstance(pid, int):
            self._transport.remove_slot_markers(request.placement_dir, host_name, pid)
        return self._close(request, claim, run_id, host_name, signals, base)

    def _stop_process(
        self,
        request: ModelRemoteLaneStopRequest,
        host: ModelStopHost,
        run_id: str,
        pid: object,
        receipt: dict[str, Any],
        signals: list[str],
    ) -> str | None:
        if host.local:
            if not isinstance(pid, int) or pid <= 1:
                raise RemoteLaneStopPortError(
                    f"the receipt of {request.lane} names no usable pid"
                )
            local_pid = pid

            def alive() -> str | None:
                return (
                    f"pid {local_pid}"
                    if self._transport.local_alive(local_pid)
                    else None
                )

            def send(sig: str) -> None:
                self._transport.signal_local(local_pid, sig)
        else:

            def alive() -> str | None:
                return self._transport.lab_tree_alive(host, run_id)

            def send(sig: str) -> None:
                self._transport.signal_lab(host, run_id, sig)

        send("TERM")
        signals.append("TERM")
        survivor = self._wait(alive, request.polite_wait_s, request.poll_interval_s)
        if survivor is None:
            return None
        send("KILL")
        signals.append("KILL")
        unit = (
            (receipt.get("guard") or {}).get("unit")
            if isinstance(receipt.get("guard"), dict)
            else None
        )
        if unit and not host.local:
            self._transport.reap_lab_unit(host, str(unit))
        return self._wait(alive, request.kill_wait_s, request.poll_interval_s)

    def _stop_cloud(
        self,
        request: ModelRemoteLaneStopRequest,
        receipt: dict[str, Any],
        signals: list[str],
    ) -> str | None:
        if self._cloud is None:
            raise RemoteLaneStopPortError(
                "the request gives no cloud client for a cloud lane"
            )
        cloud = _section(receipt, "cloud")
        result = _section(receipt, "result")
        session = str(cloud.get("session_id") or result.get("session_id") or "")
        account = str(
            cloud.get("account") or str(receipt.get("host") or "").partition(":")[2]
        )
        if not session:
            raise RemoteLaneStopPortError(
                f"the receipt of {request.lane} names no cloud session"
            )
        client = self._cloud
        client.archive(session, account)
        signals.append("ARCHIVE")
        return self._wait(
            lambda: (
                None
                if client.session_ended(session, account)
                else f"cloud session {session}"
            ),
            request.polite_wait_s + request.kill_wait_s,
            request.poll_interval_s,
        )

    def _close(
        self,
        request: ModelRemoteLaneStopRequest,
        claim: dict[str, str],
        run_id: str,
        host_name: str,
        signals: list[str],
        base: dict[str, Any],
    ) -> ModelRemoteLaneStopResult:
        lane = request.lane
        cells: list[tuple[str, str]] = [
            ("--lane", lane),
            ("--ticket", claim.get("ticket", "")),
            ("--kv", f"host={host_name}"),
            ("--kv", f"run={run_id}"),
            ("--kv", f"stopped-by={request.stopping_lane}"),
        ]
        if claim.get("parent"):
            cells.append(("--kv", f"parent={claim['parent']}"))
        worktree = claim.get("worktree", "none")
        if worktree and worktree != "none":
            cells.append(
                (
                    "--kv",
                    f"worktree=kept:{worktree}:stopped-lane-closeout-left-to-the-host-reaper",
                )
            )
        cells += [
            ("--kv", "delegated=0 delegation_reason=no-text-or-code"),
            ("--actor", request.actor),
            ("--model", request.model),
            ("--friction", "none"),
            ("--outcome", "stopped"),
            ("--closes-claim", claim["ts"]),
        ]
        text = f"lane stopped by {request.stopping_lane}: {request.reason}"
        text = text.replace("|", "/").replace("\n", " ")[:TEXT_MAX]
        written = self._ledger.write_terminal(
            request.ledger_command, request.ledger_path, cells, text
        )
        if written.accepted:
            return _result(
                "stopped",
                f"lane {lane} stopped ({'+'.join(signals)}) and closed by TERMINAL {written.ts}",
                lane,
                terminal_ts=written.ts,
                signals=signals,
                **base,
            )
        rows = _lane_rows(self._ledger.read_text(request.ledger_path), lane)
        _, closer = _claim_and_closer(rows, run_id)
        if closer is not None:  # the other closer won: this stop is a no-op
            return self._ended(
                lane,
                run_id,
                "the lane's own closeout wrote its TERMINAL first",
                closer,
                base,
            )
        return _result(
            "refused",
            f"lane {lane} stopped but the ledger refused the TERMINAL: {written.output}",
            lane,
            signals=signals,
            **base,
        )


def create_handler(
    request: ModelRemoteLaneStopRequest, run: Callable[..., Any] = subprocess.run
) -> HandlerRemoteLaneStop:
    """The handler wired to the local adapters; a cloud lane's client is the request's command."""
    cloud = (
        CommandStopCloud(request.cloud_command, run) if request.cloud_command else None
    )
    return HandlerRemoteLaneStop(LocalStopTransport(run), LocalStopLedger(run), cloud)


class HandlerRemoteLaneStopRequest:
    """Boot-resolvable adapter that builds the stop ports from each request's overlay."""

    def handle(self, request: ModelRemoteLaneStopRequest) -> ModelRemoteLaneStopResult:
        return create_handler(request).handle(request)
