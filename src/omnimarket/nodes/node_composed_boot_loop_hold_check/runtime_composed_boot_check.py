# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI of the composed-runtime boot check: ``render-override`` and ``check``.

``render-override`` writes the compose override that applies the declared limits to the kernel services and loads the
stack sampler into them. ``check`` probes /health from the container start, reads the sampler's records out of the
container, judges the boot and exits 1 when it is red.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_composed_boot_loop_hold_check.adapters.adapter_boot_limits import (
    limits_applied_error,
    read_node_declaration,
    render_override,
    resolve_bounds,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.adapters.adapter_boot_observe import (
    DockerExecProbe,
    parse_hold_records,
    probe_until_ready,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.handlers.handler_boot_check import (
    HandlerBootCheck,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_check_request import (
    ModelBootCheckRequest,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_observation import (
    ModelBootObservation,
)

_CONTRACT = Path(__file__).parent / "contract.yaml"
_SAMPLER_DIR = Path(__file__).parent / "sampler"


def _seconds_since_container_start(container: str) -> float:
    started = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", container],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    stamp = started.split(".")[0] + "+00:00"
    return (
        datetime.now(tz=datetime.fromisoformat(stamp).tzinfo)
        - datetime.fromisoformat(stamp)
    ).total_seconds()


def _window_seconds(infra_root: Path) -> float:
    declaration = read_node_declaration(_CONTRACT)
    spec = declaration["observe_window_source"]
    policy = yaml.safe_load((infra_root / spec["file"]).read_text(encoding="utf-8"))
    node = policy
    for key in spec["key_path"]:
        node = node[key]
    return float(node)


def _render(args: argparse.Namespace) -> int:
    bounds = resolve_bounds(_CONTRACT, Path(args.infra_root))
    override = render_override(
        bounds,
        tuple(args.service),
        sampler_dir=str(_SAMPLER_DIR.resolve()),
        sampler_out=args.sampler_out,
        existing_pythonpath=args.existing_pythonpath,
    )
    Path(args.out).write_text(
        yaml.safe_dump(override, sort_keys=True), encoding="utf-8"
    )
    sys.stdout.write(f"limits: cpus={bounds.cpus:g} memory={bounds.memory_limit}\n")
    return 0


def _check(args: argparse.Namespace) -> int:
    infra_root = Path(args.infra_root)
    bounds = resolve_bounds(_CONTRACT, infra_root)
    declaration = read_node_declaration(_CONTRACT)
    applied = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}",
            args.container,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    limits_error = limits_applied_error(bounds, int(applied[0]), int(applied[1]))
    if limits_error is not None:
        sys.stderr.write(f"::error::{limits_error}\n")
        return 1
    samples = probe_until_ready(
        DockerExecProbe(args.container, args.health_url),
        window_seconds=_window_seconds(infra_root),
        probe_timeout_seconds=float(declaration["probe_timeout_seconds"]),
        interval_seconds=float(declaration["probe_interval_seconds"]),
        elapsed_at_start=_seconds_since_container_start(args.container),
    )
    records = subprocess.run(
        [
            "docker",
            "exec",
            args.container,
            "sh",
            "-c",
            f"cat {args.sampler_out} 2>/dev/null || true",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = HandlerBootCheck().handle(
        ModelBootCheckRequest(
            bounds=bounds,
            observation=ModelBootObservation(
                health_samples=samples, loop_holds=parse_hold_records(records)
            ),
        )
    )
    sys.stdout.write(json.dumps(result.model_dump(mode="json"), indent=2) + "\n")
    for finding in result.findings:
        sys.stderr.write(f"::error::{finding.detail}\n")
    return 0 if result.green else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_composed_boot_loop_hold_check")
    commands = parser.add_subparsers(dest="command", required=True)

    render = commands.add_parser("render-override")
    render.add_argument("--infra-root", required=True)
    render.add_argument("--out", required=True)
    render.add_argument("--service", action="append", required=True)
    render.add_argument("--sampler-out", required=True)
    render.add_argument("--existing-pythonpath", default="")
    render.set_defaults(run=_render)

    check = commands.add_parser("check")
    check.add_argument("--infra-root", required=True)
    check.add_argument("--container", required=True)
    check.add_argument("--health-url", required=True)
    check.add_argument("--sampler-out", required=True)
    check.set_defaults(run=_check)

    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
