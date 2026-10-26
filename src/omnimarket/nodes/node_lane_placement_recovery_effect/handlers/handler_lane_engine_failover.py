# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Re-place the same task after quota refusal, through the existing runner (OMN-17427).

Account selection and verification remain in HandlerClaudeAccountLimit. Attempts use the
runner's placement node, engine adapters, guard, cleanup and ledger relay. No credentials
are read here. Every retry has its own receipt; the original receipt follows the whole job.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from omnimarket.handlers.rules_lane_requirements import (
    claude_required as claude_required,
)
from omnimarket.handlers.rules_lane_requirements import (
    declarations as declarations,
)

from ..models.model_lane_engine_failover import (
    ModelLaneEngineFailoverRequest,
    ModelLaneEngineFailoverResult,
)

# Attributes the runner's argument namespace carries across failover attempts.
FAILOVER_HISTORY_ATTR = "_failover_history"
FAILOVER_MANAGED_ATTR = "_failover_managed"
FAILOVER_LOG_ATTR = "_failover_log"

CLAUDE = frozenset({"opus", "sonnet", "claude-opus", "claude-sonnet"})
UNAVAILABLE = frozenset(
    {
        "no-host",
        "codex-usage-limit",
        "capability-unavailable",
        "codex-missing",
        "codex-not-logged-in",
        "codex-sandbox-unavailable",
    }
)
MAX_CLAUDE_ATTEMPTS = 16


def parse_route(brief: str) -> dict[str, str] | None:
    """The runner's existing first column-zero ROUTE and its key=value fields."""
    found = re.search(r"^ROUTE:[ \t]*(\S.*?)[ \t]*$", brief, re.MULTILINE)
    if not found:
        return None
    return {
        k: v
        for k, _, v in (
            tok.partition("=") for tok in found.group(1).split() if "=" in tok
        )
    }


def engine_readings(readings: list[Any]) -> list[Any]:
    """GLM needs the Claude client, while Claude account quota and login do not apply."""
    return [
        replace(
            r,
            host=replace(r.host, engines=frozenset({"claude_sonnet"}))
            if r.claude_login_why != "no-claude"
            and (r.host.engines or r.claude_login_why)
            else replace(r.host, engines=frozenset()),
            claude_limited_until=None,
            claude_auth_expired_at=None,
        )
        for r in readings
    ]


def engine_admission(reading: Any, refused: str | None) -> dict[str, Any]:
    r = engine_readings([reading])[0]
    why = refused or r.error or r.lane_admission_refusal
    if "claude_sonnet" not in r.host.engines:
        why = why or "no Claude client for GLM"
    return {"glm_slots": r.lane_slots if why is None else 0, "glm_refusal": why}


def host_admission(
    reading: Any, refused: str | None, claude: str | None, codex: str | None
) -> dict[str, Any]:
    """Build the tick's engine admission facts from one placement reading."""
    return {
        "placed": reading.placed,
        "lanes": reading.lanes_placed,
        "cap": reading.lane_cap,
        "slots": 0 if refused is not None else reading.lane_slots,
        "probe_error": reading.error,
        "claude_slots": reading.lane_slots if claude is None else 0,
        "claude_refusal": claude,
        "codex_slots": reading.lane_slots if codex is None else 0,
        "codex_refusal": codex,
        **engine_admission(reading, refused),
    }


def initialize_receipt(receipt: dict[str, Any], args: Any, glm: Any) -> None:
    """Attach the selected engine and managed failover history before the first write."""
    if hasattr(args, "_failover_history"):
        receipt["failover"] = getattr(args, FAILOVER_HISTORY_ATTR)
    if glm:
        receipt.update(engine="glm", glm=glm.describe())
    if args.model in ("codex", "code-edit"):
        receipt["engine"] = args.model


def tick_request(request: dict[str, Any]) -> dict[str, Any]:
    """Keep local GLM admission facts out of the pinned tick node's strict wire model."""
    wire = copy.deepcopy(request)
    for host in (wire.get("lab_headroom") or {}).get("hosts") or []:
        admission = host.get("admission")
        if isinstance(admission, dict):
            admission.pop("glm_slots", None)
            admission.pop("glm_refusal", None)
    return wire


def rewrite_route(brief: str, model: str, effort: str = "high") -> tuple[str, str, str]:
    lines = declarations(brief)
    before = next((line for line in lines if line.startswith("ROUTE:")), "")
    if before:
        after = re.sub(r"\bmodel=\S+", f"model={model}", before)
        if after == before and "model=" not in before:
            after += f" model={model}"
        after = re.sub(r"\bdest=\S+", f"dest={model}", after)
        return brief.replace(before, after, 1), before, after
    after = f"ROUTE: model={model} effort={effort}"
    return after + "\n" + brief, before, after


class HandlerLaneEngineFailover:
    def __init__(self, runtime: Mapping[str, Any] | None = None) -> None:
        self.runtime = runtime if runtime is not None else {}

    def handle(
        self, request: ModelLaneEngineFailoverRequest
    ) -> ModelLaneEngineFailoverResult:
        original, rt = request.args, self.runtime
        if (
            original.model not in CLAUDE
            or getattr(original, "placement", None) == "cloud"
        ):
            return ModelLaneEngineFailoverResult(
                request.execute(original, **request.kwargs)
            )
        try:
            source = Path(original.brief).read_bytes()
            brief = source.decode("utf-8")
        except (OSError, UnicodeError):
            return ModelLaneEngineFailoverResult(
                request.execute(original, **request.kwargs)
            )
        current = copy.copy(original)
        current.run_id = (
            original.run_id
            or f"rlane-{original.lane[:40]}-{rt['secrets'].token_hex(3)}"
        )
        root_run = current.run_id
        root = (
            Path(original.receipt)
            if original.receipt
            else rt["state_dir"]() / original.lane / f"{root_run}.json"
        )
        attempt_dir = root.parent / f"{root.stem}.attempts"
        current.receipt = str(root)
        setattr(current, FAILOVER_MANAGED_ATTR, True)
        pinned = claude_required(brief)
        history: dict[str, Any] = {
            "original_model": original.model,
            "original_brief": original.brief,
            "original_sha256": hashlib.sha256(source).hexdigest(),
            "attempts": [],
            "transitions": [],
        }
        setattr(current, FAILOVER_HISTORY_ATTR, history)
        echo = request.kwargs.get("echo", print)
        start = time.monotonic()
        claude_attempts = 0
        quota_seen = False
        while True:
            code = request.execute(current, **request.kwargs)
            data = json.loads(Path(current.receipt).read_text())
            if current.model in CLAUDE:
                claude_attempts += 1
            status = data.get("status")
            quota_seen |= status == "host-limited" or bool(data.get("limited"))
            attempt_dir.mkdir(parents=True, exist_ok=True)
            attempt_path = attempt_dir / f"attempt-{len(history['attempts']) + 1}.json"
            rt["write_receipt"](
                attempt_path, {**data, "receipt": str(attempt_path), "final": True}
            )
            history["attempts"].append(
                {
                    "model": current.model,
                    "run_id": current.run_id,
                    "host": data.get("host"),
                    "status": status,
                    "exit_code": code,
                    "receipt": str(attempt_path),
                    "failure": data.get("failure"),
                    "workdir": data.get("workdir"),
                    "closeout": data.get("closeout"),
                }
            )
            next_model = None
            if (
                current.model in CLAUDE
                and status == "host-limited"
                and claude_attempts < MAX_CLAUDE_ATTEMPTS
            ):
                next_model = current.model
            elif (
                current.model in CLAUDE
                and quota_seen
                and status in ("host-limited", "no-host")
            ):
                if pinned:
                    history["waiting"] = "claude-required"
                else:
                    next_model = "codex"
            elif current.model == "codex" and status in UNAVAILABLE:
                refusal = rt["glm_refusal"](
                    current.lane, current.parent, getattr(current, "risk_class", None)
                )
                if refusal:
                    history["glm_refused"] = refusal
                else:
                    next_model = "glm"
            if next_model is None:
                if history.get("waiting") == "claude-required" or (
                    quota_seen
                    and status in (UNAVAILABLE | {"glm-key-missing", "glm-usage-limit"})
                ):
                    history.setdefault("waiting", "engine-pool")
                    data.update(
                        status="no-host",
                        reason=f"{history['waiting']}: {data.get('reason') or data.get('error') or status}",
                    )
                    code = rt["EXIT_NO_HOST"]
                    data["exit_code"] = code
                history["exhausted"] = bool(
                    quota_seen and code and history.get("waiting") != "claude-required"
                )
                break
            if time.monotonic() - start >= original.timeout_min * 60:
                history["stopped"] = "time-box-exhausted"
                break
            try:
                unchanged = Path(original.brief).read_bytes() == source
            except OSError:
                unchanged = False
            if not unchanged:
                history["stopped"] = "source-brief-changed"
                break
            rewritten, before, after = rewrite_route(
                brief, next_model, getattr(current, "effort", None) or "high"
            )
            _, before, _ = rewrite_route(Path(current.brief).read_text(), next_model)
            context = {
                key: data.get(key) for key in ("host", "run_id", "workdir", "closeout")
            }
            rewritten += (
                "\nRunner recovery context: "
                + json.dumps(context, sort_keys=True)
                + "\nInspect this lane's earlier work before writing; preserve any archived or kept changes.\n"
            )
            transition = {
                "from": current.model,
                "to": next_model,
                "reason": status,
                "route_before": before,
                "route_after": after,
            }
            history["transitions"].append(transition)
            echo(
                f"REMOTE-LANE-FAILOVER lane={original.lane} from={current.model} to={next_model} reason={status}"
            )
            data.update(status="placing", final=False, failover=history)
            rt["write_receipt"](root, data)
            n = len(history["attempts"]) + 1
            current = copy.copy(current)
            current.model = next_model
            current.run_id = f"{root_run}-f{n}"
            setattr(
                current, FAILOVER_LOG_ATTR, str(attempt_dir / f"attempt-{n}.log.jsonl")
            )
            # The brief's Required host and host-local inputs are re-read by the runner.
            current.host = None
            current.placement = "lab"
            current.timeout_min = max(
                1, int(original.timeout_min - (time.monotonic() - start) / 60)
            )
            route_path = attempt_dir / f"route-{n}.md"
            route_path.write_text(rewritten, encoding="utf-8")
            current.brief = str(route_path)
            current.brief_sha256 = hashlib.sha256(rewritten.encode()).hexdigest()
            current.base_url = None
        data.update(final=True, receipt=str(root), run_id=root_run, failover=history)
        # Queue and parent notification belong to the final result, never an intermediate refusal.
        if data.get("status") == "no-host":
            first = json.loads(Path(history["attempts"][0]["receipt"]).read_text())
            queued = {**data, "brief_copy": first.get("brief_copy") or original.brief}
            rt["queue_no_host"](
                original,
                queued,
                pin=original.host,
                reason=str(data.get("reason") or ""),
                echo=echo,
            )
            if "queued" in queued:
                data["queued"] = queued["queued"]
            history["queued_model"] = original.model
            history["queued_brief"] = queued["brief_copy"]
        notice = data.get("handoff_refusal") or (
            getattr(original, "detached_child", False)
            and data.get("status") not in rt["NOTICE_EXEMPT_STATUSES"]
        )
        if notice and not original.parent:
            data["parent_notice"] = {"sent": False, "reason": "no --parent"}
        elif notice:
            try:
                ts, out = rt["append"](
                    rt["parent_notice_row"](
                        current,
                        data.get("host") or "none",
                        root_run,
                        data["status"],
                        code,
                        root,
                        rt["queued_notice"](data, str(data.get("error") or "")[:160]),
                        failure=data.get("failure"),
                        handoff_refusal=data.get("handoff_refusal"),
                    ),
                    run=request.kwargs.get("run", subprocess.run),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                ts, out = None, f"{type(exc).__name__}: {exc}"
            data["parent_notice"] = {
                "sent": ts is not None,
                "row": ts,
                "to": original.parent,
                "output": out[:200],
            }
            if ts is not None:
                echo(
                    f"REMOTE-LANE-PARENT-NOTICE lane={original.lane} to={original.parent} status={data['status']} row={ts}"
                )
            else:
                echo(
                    f"REMOTE-LANE-PARENT-NOTICE-REFUSED lane={original.lane} to={original.parent}: {out}"
                )
        try:
            rt["write_receipt"](root, data)
        except OSError as exc:
            echo(f"REMOTE-LANE-RECEIPT-WRITE-RETRY {type(exc).__name__}: {exc}")
            code = rt["EXIT_REMOTE_FAILED"]
            data.update(
                status="crashed", exit_code=code, error=f"{type(exc).__name__}: {exc}"
            )
            rt["write_receipt"](root, data)
        echo(f"RECEIPT {root}")
        return ModelLaneEngineFailoverResult(code)
