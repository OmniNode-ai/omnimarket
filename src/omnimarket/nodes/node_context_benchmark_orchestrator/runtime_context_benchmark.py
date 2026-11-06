# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime entry for node_context_benchmark_orchestrator.

Usage:
    python -m omnimarket.nodes.node_context_benchmark_orchestrator.runtime_context_benchmark \\
        --pilot --lanes claude,codex,qwen,route:openrouter-north-mini-code,jev \\
        --scratch DIR --out DIR [--host NAME] [--limit N]

``--pilot`` materializes the bundled 10-task fixture (with its own CLAUDE.md and
AGENTS.md) under ``--scratch``; ``--source DIR --tasks FILE`` runs another tree.
Writes ``result.json`` and ``report.md`` to ``--out``. Exit 0 when the run finished.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

import yaml

from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.handler_context_benchmark import (
    HandlerContextBenchmark,
)
from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.lane_runners import (
    LaneDelegateRoute,
)
from omnimarket.nodes.node_context_benchmark_orchestrator.models.model_context_benchmark import (
    EnumBenchmarkLane,
    ModelBenchmarkLaneSpec,
    ModelBenchmarkTask,
    ModelContextBenchmarkRequest,
)

_PILOT = Path(__file__).parent / "pilot" / "pilot_tasks.yaml"
_QWEN = "Qwen3.8-27B"


def materialize_pilot(dest: Path) -> list[ModelBenchmarkTask]:
    spec = yaml.safe_load(_PILOT.read_text(encoding="utf-8"))
    for rel, body in {**spec["files"], **spec["context_files"]}.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return [ModelBenchmarkTask.model_validate(t) for t in spec["tasks"]]


def lane_spec(name: str) -> ModelBenchmarkLaneSpec:
    if name == "claude":
        return ModelBenchmarkLaneSpec(
            lane=EnumBenchmarkLane.CLAUDE_HEADLESS, model_id="claude-headless"
        )
    if name == "codex":
        return ModelBenchmarkLaneSpec(
            lane=EnumBenchmarkLane.CODEX_HEADLESS, model_id="codex-headless"
        )
    if name == "jev":
        return ModelBenchmarkLaneSpec(
            lane=EnumBenchmarkLane.JEV_TYPED_DECISION,
            model_id="jev-latest",
            backend_id="cloud-typesafe-jev",
        )
    if name == "qwen":
        return ModelBenchmarkLaneSpec(
            lane=EnumBenchmarkLane.DELEGATE_ROUTE, model_id=_QWEN
        )
    if name.startswith("route:"):
        backend = name.split(":", 1)[1]
        return ModelBenchmarkLaneSpec(
            lane=EnumBenchmarkLane.DELEGATE_ROUTE, model_id=backend, backend_id=backend
        )
    raise SystemExit(f"unknown lane {name!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_context_benchmark_orchestrator")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("--tasks")
    parser.add_argument("--lanes", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("--limit", type=int)
    parser.add_argument("--agent-timeout", type=float, default=900.0)
    parser.add_argument("--onex-bin", default=os.environ.get("ONEX_BIN"))
    parser.add_argument("--state-root", default=os.environ.get("ONEX_STATE_DIR"))
    parser.add_argument(
        "--route-flags",
        default="--bus kafka --lane dev --locus deployed-lane",
        help="transport flags for onex delegate",
    )
    args = parser.parse_args(argv)

    scratch, out = Path(args.scratch), Path(args.out)
    scratch.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    if args.pilot:
        source = scratch / "source"
        tasks = materialize_pilot(source)
    else:
        if not args.source or not args.tasks:
            raise SystemExit("--source and --tasks are required without --pilot")
        source = Path(args.source)
        raw = yaml.safe_load(Path(args.tasks).read_text(encoding="utf-8"))
        tasks = [ModelBenchmarkTask.model_validate(t) for t in raw["tasks"]]
    if args.limit:
        tasks = tasks[: args.limit]

    lane_names = [n for n in args.lanes.split(",") if n]
    needs_route = any(n == "qwen" or n.startswith("route:") for n in lane_names)
    if needs_route and not (args.onex_bin and args.state_root):
        raise SystemExit(
            "a delegation-route lane needs --onex-bin and --state-root "
            "(or ONEX_BIN and ONEX_STATE_DIR)"
        )
    route = LaneDelegateRoute(
        onex_bin=args.onex_bin or "",
        state_root=Path(args.state_root or "."),
        lane="context-benchmark-1715",
        ticket="OMN-21896",
        transport_flags=tuple(args.route_flags.split()),
    )
    handler = HandlerContextBenchmark(runners={EnumBenchmarkLane.DELEGATE_ROUTE: route})
    request = ModelContextBenchmarkRequest(
        source_dir=str(source),
        tasks=tuple(tasks),
        lanes=tuple(lane_spec(n) for n in lane_names),
        scratch_dir=str(scratch / "arms"),
        host=args.host,
        agent_timeout_seconds=args.agent_timeout,
    )
    result = handler.handle(request)
    (out / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (out / "report.md").write_text(result.report_markdown, encoding="utf-8")
    sys.stdout.write(result.report_markdown)
    for line in result.blocked_lanes:
        sys.stdout.write(f"BLOCKED {line}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
