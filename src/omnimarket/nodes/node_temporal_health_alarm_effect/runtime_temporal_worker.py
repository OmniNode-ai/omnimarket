# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Temporal worker of a lab host, run by its registry_module_tick unit (temporal-worker-systemd).

One persistent unit serves every task queue the ``worker`` block of this node's contract declares: workflow 1 (the
primary, ``worker.module``) and each ``worker.also_serves`` entry (the reader, the closer and the prune workflow). Each is
started as a child, ``python -m <module> <args>`` run in the environment of the checkout the unit names in
``$<worker.project_environment>`` (``uv run --frozen --project``), against the address in the contract
(``ONEX_TEMPORAL_ADDRESS`` and ``ONEX_TEMPORAL_NAMESPACE`` override it). When any child ends, the others are ended and
this process exits non-zero, so the unit's ``Restart=on-failure`` starts every queue again; a worker that cannot reach the
server ends the same way. An ``also_serves`` worker whose module the checkout does not carry is left out and named in the
start line. ``--probe`` checks that the checkout carries the primary module and names what would be served, without a
connection.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

CONTRACT = Path(__file__).with_name("contract.yaml")


def worker_settings() -> dict[str, Any]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    settings = dict(document.get("worker") or {})
    for key in ("module", "project_environment", "address", "namespace"):
        if not str(settings.get(key, "")).strip():
            raise ValueError(f"the worker block of {CONTRACT.name} lacks {key}")
    if os.environ.get("ONEX_TEMPORAL_ADDRESS"):
        settings["address"] = os.environ["ONEX_TEMPORAL_ADDRESS"]
    if os.environ.get("ONEX_TEMPORAL_NAMESPACE"):
        settings["namespace"] = os.environ["ONEX_TEMPORAL_NAMESPACE"]
    return settings


def worker_project(settings: dict[str, Any]) -> Path:
    """The checkout the unit names, which must carry the worker module's package."""
    name = str(settings["project_environment"])
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise ValueError(f"${name} names no checkout for the worker")
    project = Path(raw)
    package = project / "src" / Path(*str(settings["module"]).split("."))
    if not (project / "pyproject.toml").is_file() or not package.is_dir():
        raise ValueError(
            f"${name}={project} is not a checkout carrying {settings['module']}"
        )
    return project


def module_present(project: Path, module: str) -> bool:
    """Whether the checkout carries ``module``: a package directory, or a module file."""
    path = project / "src" / Path(*module.split("."))
    return path.is_dir() or path.with_suffix(".py").is_file()


def worker_argv(
    project: Path, uv: str, module: str, args: list[str], values: dict[str, str]
) -> list[str]:
    return [
        uv,
        "run",
        "--frozen",
        "--project",
        str(project),
        "python",
        "-m",
        module,
        *[str(arg).format(**values) for arg in args],
    ]


def worker_commands(
    settings: dict[str, Any], project: Path, uv: str, registry_root: str
) -> tuple[list[list[str]], list[str]]:
    """One command per worker the checkout carries (the primary first), and the modules it lacks."""
    values = {
        "address": str(settings["address"]),
        "namespace": str(settings["namespace"]),
        "registry_root": registry_root,
    }
    primary_args = [
        "worker",
        "--target-host",
        "{address}",
        "--namespace",
        "{namespace}",
    ]
    commands = [worker_argv(project, uv, str(settings["module"]), primary_args, values)]
    skipped: list[str] = []
    for entry in settings.get("also_serves") or []:
        module = str(entry["module"])
        if module_present(project, module):
            commands.append(
                worker_argv(project, uv, module, list(entry.get("args") or []), values)
            )
        else:
            skipped.append(module)
    return commands, skipped


def _end(children: list[subprocess.Popen[bytes]], grace: float) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + grace
    for child in children:
        try:
            child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def supervise(
    commands: list[list[str]],
    environ: dict[str, str],
    *,
    poll_interval: float = 1.0,
    grace: float = 15.0,
) -> int:
    """Run every command as a child until any one ends; end the rest and return that one's code (never 0).

    A worker is never meant to end, so a clean exit is a failure too: ``Restart=on-failure`` then starts the unit again.
    A SIGTERM (the unit's stop) ends every child and returns 0.
    """
    children = [subprocess.Popen(command, env=environ) for command in commands]
    stopped: list[bool] = []

    def on_term(_signum: int, _frame: object) -> None:
        stopped.append(True)

    previous = signal.signal(signal.SIGTERM, on_term)
    try:
        while not stopped:
            for child in children:
                code = child.poll()
                if code is not None:
                    _end(children, grace)
                    return code or 1
            time.sleep(poll_interval)
        _end(children, grace)
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
        _end(children, grace)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime_temporal_worker")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--omni-home", dest="registry_root", default="")
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args(argv)
    settings = worker_settings()
    project = worker_project(settings)
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv is not on PATH")
    commands, skipped = worker_commands(settings, project, uv, args.registry_root)
    served = [command[command.index("-m") + 1] for command in commands]
    if args.probe:
        sys.stdout.write(
            json.dumps(
                {
                    "probe": "ok",
                    "project": str(project),
                    "serves": served,
                    "skipped": skipped,
                }
            )
            + "\n"
        )
        return 0
    environ = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    sys.stdout.write(json.dumps({"start": commands, "skipped": skipped}) + "\n")
    sys.stdout.flush()
    return supervise(commands, environ)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
