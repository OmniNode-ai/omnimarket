# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The workflow 1 worker of a lab host, run by its registry_module_tick unit (temporal-worker-systemd).

Replaces this process with the declared CLI of the workflow's node (``python -m <worker.module> worker``), run in the
environment of the checkout the unit names in ``$<worker.project_environment>`` (``uv run --frozen --project``), against
the address in the ``worker`` block of this node's contract (``ONEX_TEMPORAL_ADDRESS`` and ``ONEX_TEMPORAL_NAMESPACE``
override it). A worker that cannot reach the server exits, and its unit's timer starts it again. ``--probe`` checks
that the checkout carries the worker module, and returns without a connection.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, NoReturn

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


def worker_argv(settings: dict[str, Any], project: Path, uv: str) -> list[str]:
    return [
        uv,
        "run",
        "--frozen",
        "--project",
        str(project),
        "python",
        "-m",
        str(settings["module"]),
        "worker",
        "--target-host",
        str(settings["address"]),
        "--namespace",
        str(settings["namespace"]),
    ]


def replace_process(command: list[str], environ: dict[str, str]) -> NoReturn:
    """Replace this process with the worker command; ``os.execve`` does not return."""
    os.execve(command[0], command, environ)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime_temporal_worker")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--omni-home", default="")
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args(argv)
    settings = worker_settings()
    project = worker_project(settings)
    if args.probe:
        sys.stdout.write(json.dumps({"probe": "ok", "project": str(project)}) + "\n")
        return 0
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv is not on PATH")
    command = worker_argv(settings, project, uv)
    environ = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    sys.stdout.write(json.dumps({"start": command}) + "\n")
    sys.stdout.flush()
    return replace_process(command, environ)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
