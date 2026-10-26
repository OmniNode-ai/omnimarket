"""Declarative compute node and the legacy alarm's bus adapter (OMN-19998)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import ModelControllerFixPlanRequest, ModelControllerFixPlanResult

NODE = "node_controller_fix_plan_compute"
CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeControllerFixPlanCompute:
    contract_path = CONTRACT

    def handle(
        self, request: ModelControllerFixPlanRequest
    ) -> ModelControllerFixPlanResult:
        from .handler import HandlerControllerFixPlan

        return HandlerControllerFixPlan().handle(request)


def _invoke(request: dict[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    """Use the same onex node bus runtime as the throughput decision, with this packaged contract."""
    home = environ.get("ONEX_REGISTRY_ROOT", "").strip()
    command = environ.get("ONEX_BIN") or (
        str(Path(home) / "omnibase_infra/scripts/onex") if home else None
    )
    if command is None:
        raise RuntimeError(
            "controller fix plan unavailable: set ONEX_REGISTRY_ROOT or ONEX_BIN"
        )
    env = dict(environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(Path(__file__).parents[3]), env.get("PYTHONPATH")))
    )
    with tempfile.TemporaryDirectory(prefix="controller-fix-plan-") as scratch:
        source = Path(scratch) / "request.json"
        source.write_text(json.dumps(request, default=str), encoding="utf-8")
        argv = [
            command,
            "node",
            NODE,
            "--contract",
            str(CONTRACT),
            "--input",
            str(source),
            "--output",
            "receipt",
        ]
        if home:
            argv.extend(["--state-root", str(Path(home) / ".onex_state")])
        try:
            done = subprocess.run(
                argv,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"controller fix plan unavailable: {exc}") from exc
    for line in reversed(done.stdout.splitlines()):
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        if (
            done.returncode == 0
            and isinstance(envelope, dict)
            and envelope.get("status") == "success"
            and isinstance(envelope.get("result"), dict)
        ):
            result: dict[str, Any] = envelope["result"]
            return result
    detail = " ".join((done.stderr or done.stdout).split())[-200:]
    raise RuntimeError(
        f"controller fix plan unavailable: exit {done.returncode}: {detail}"
    )


def controller_fix_target(
    request: Mapping[str, Any],
    contract: Mapping[str, Any],
    controller_host: str | None,
    *,
    environ: Mapping[str, str],
) -> tuple[dict[str, Any], str | None, str]:
    try:
        result = _invoke(
            {
                "operation": "target",
                "contract": dict(contract),
                "controller_host": controller_host,
                "lab_headroom": request.get("lab_headroom"),
            },
            environ,
        )
        return result["contract"], result.get("host"), result.get("error", "")
    except (RuntimeError, ValueError, KeyError) as exc:
        error = str(exc)
        return {**contract, "controller_fix_plan_error": error}, None, error


def controller_fix_lanes(
    now: Any,
    dispatched: Mapping[str, Any],
    idents: Mapping[str, str],
    receipts: Mapping[str, Any],
    ledger_lanes: Mapping[str, Any],
    live_min: float,
    *,
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """The live lane, the dead lanes and the defect keys to send again (operation ``lanes``); raises RuntimeError."""
    return _invoke(
        {
            "operation": "lanes",
            "contract": {},
            "now": now.isoformat() if hasattr(now, "isoformat") else str(now),
            "dispatched": dict(dispatched),
            "idents": dict(idents),
            "receipts": dict(receipts),
            "ledger_lanes": dict(ledger_lanes),
            "live_min": live_min,
        },
        environ,
    )


def controller_fix_brief(
    defects: list[Any],
    history: Mapping[str, list[str]],
    contract: Mapping[str, Any],
    lane: str,
    bound_min: float,
    *,
    environ: Mapping[str, str],
) -> str:
    if contract.get("controller_fix_plan_error"):
        return str(contract["controller_fix_plan_error"])
    return str(
        _invoke(
            {
                "operation": "brief",
                "contract": dict(contract),
                "defects": [asdict(d) for d in defects],
                "history": dict(history),
                "lane": lane,
                "bound_min": bound_min,
            },
            environ,
        )["brief"]
    )
