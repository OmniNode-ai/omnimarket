# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Subprocess runner, ledger cell parsing, compose listing and lane census discovery.

The reaper proves a stack's ownership from the ledger, the compose listing and the declared
lane census. Every helper here is a pure function over text, except the runner, which is the one
seam through which a docker command or a ledger append leaves the process.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..models import ModelLabStackReaperRequest

CENSUS_REL = Path("omnibase_infra/deploy/lane-census/lane-manifest.yaml")
CONFIG_CENSUS_REL = Path("config/lab/lane-census/lane-manifest.yaml")
CONFIG_ROOT_ENV = "ONEX_WORKSPACE_CONFIG_ROOT"
CENSUS_ENV = "ONEX_LANE_MANIFEST"
DECLARED_PROJECTS_ENV = "ONEX_LAB_DECLARED_PROJECTS"
APPEND_TOOL_ENV = "ONEX_LEDGER_APPEND_TOOL"
APPEND_COMMAND_ENV = "ONEX_LEDGER_COMMAND"
DOCKER_TIMEOUT_S = 600
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PROJECT_LABEL = "com.docker.compose.project"
DOCKER_HOST_RE = re.compile(r"^ssh://([A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+(:\d+)?$")
STAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
_TICKET_RE = re.compile(r"omn-?(\d+)", re.IGNORECASE)
_HOME_RE = re.compile(r"^/(?:home|[Uu]sers)/([^/]+)/")


@dataclass(frozen=True)
class Completed:
    """The part of a finished subprocess this reads."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], Completed]


def run_process(argv: Sequence[str]) -> Completed:
    """The production runner: one bounded subprocess that never raises on a non-zero exit."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=DOCKER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        return Completed(124, "", f"{argv[0]} timed out: {exc}")
    except OSError as exc:
        return Completed(127, "", f"{argv[0]} did not run: {exc}")
    return Completed(proc.returncode, proc.stdout, proc.stderr)


def append_argv(
    ledger: Path, row: str, env: Mapping[str, str] | None = None
) -> list[str]:
    """The argv that appends ``row`` to ``ledger``.

    ``ONEX_LEDGER_APPEND_TOOL`` names a replacement script taking the same
    ``<ledger> --append <row>`` arguments, run on the current interpreter; it is a test seam.
    Otherwise ``ONEX_LEDGER_COMMAND`` (default ``onex-ledger``) is the writer.
    """
    source = os.environ if env is None else env
    tool = source.get(APPEND_TOOL_ENV, "").strip()
    if tool:
        head = [sys.executable, tool]
    else:
        head = shlex.split(source.get(APPEND_COMMAND_ENV, "").strip() or "onex-ledger")
    return [*head, str(ledger), "--append", row]


def cells_of(line: str) -> list[str]:
    return [cell.strip() for cell in line.split("|")]


def cell_value(cells: Sequence[str], key: str) -> str:
    prefix = f"{key}="
    for cell in cells[2:]:
        if cell.startswith(prefix):
            return cell[len(prefix) :].strip()
    return ""


def tickets_of(text: str) -> set[str]:
    return {f"OMN-{n}" for n in _TICKET_RE.findall(text)}


def token_in(token: str, text: str) -> bool:
    """token appears in text as whole hyphen/underscore/slash-delimited words."""
    pad = re.compile(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])")
    return bool(pad.search(text.lower()))


def tail(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def cell(text: str, limit: int = 400) -> str:
    return re.sub(r"[|\r\n]+", "/", text).strip()[:limit]


def project_names(listing: str) -> set[str]:
    try:
        data = json.loads(listing or "[]")
    except json.JSONDecodeError:
        return set()
    return {
        str(item.get("Name"))
        for item in data
        if isinstance(item, dict) and item.get("Name")
    }


def config_files(listing: str, project: str) -> list[str]:
    """The compose config files `docker compose ls` reports for one project."""
    try:
        data = json.loads(listing or "[]")
    except json.JSONDecodeError:
        return []
    for item in data:
        if isinstance(item, dict) and item.get("Name") == project:
            return [
                p.strip()
                for p in str(item.get("ConfigFiles") or "").split(",")
                if p.strip()
            ]
    return []


def acting_user(docker_host: str | None) -> str:
    """The account the docker calls run as: the ssh user in the host URL, else this one."""
    if docker_host and "@" in docker_host:
        return docker_host.split("://", 1)[-1].split("@", 1)[0]
    return getpass.getuser()


def foreign_config_paths(paths: Sequence[str], user: str) -> list[str]:
    """Config files under a home directory that is not `user`'s (reported by home, not path)."""
    foreign: set[str] = set()
    for path in paths:
        match = _HOME_RE.match(path)
        if match and match.group(1) != user:
            foreign.add(f"a home directory of {match.group(1)}")
    return sorted(foreign)


def census_candidates(request: ModelLabStackReaperRequest) -> list[Path]:
    """Where the lane manifest is looked for, first readable wins."""
    paths: list[Path] = []
    if request.census is not None:
        return [request.census]
    env = os.environ.get(CENSUS_ENV, "").strip()
    if env:
        return [Path(env)]
    config_root = os.environ.get(CONFIG_ROOT_ENV, "").strip()
    if config_root:
        paths.append(Path(config_root) / CONFIG_CENSUS_REL)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / CONFIG_CENSUS_REL
        if candidate.is_file():
            paths.append(candidate)
            break
    home = request.registry_root or (
        Path(os.environ["OMNI_HOME"]) if "OMNI_HOME" in os.environ else None
    )
    if home is not None:
        paths.append(home / CENSUS_REL)
    return paths


def declared_lane_objects(census: Path) -> tuple[set[str], set[str]]:
    """(compose projects, container names) every declared lane owns, from the census."""
    try:
        data = yaml.safe_load(census.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{census}: {exc}") from exc
    lanes = data.get("lanes") if isinstance(data, dict) else None
    if not isinstance(lanes, dict) or not lanes:
        raise ValueError(f"{census}: no lanes mapping")
    projects: set[str] = set()
    names: set[str] = set()
    for lane in lanes.values():
        if not isinstance(lane, dict):
            continue
        project = lane.get("compose_project")
        if isinstance(project, str) and project:
            projects.add(project)
        for value in lane.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    names.add(item["name"])
    return projects, names


def declared_projects_from_environment() -> set[str]:
    """The compose projects the host declares as lanes in the environment, comma separated."""
    return {
        item.strip()
        for item in os.environ.get(DECLARED_PROJECTS_ENV, "").split(",")
        if item.strip()
    }


def load_declared(
    request: ModelLabStackReaperRequest,
) -> tuple[set[str], set[str], str]:
    """(projects, names, source). The environment's declared projects always count; a census adds."""
    projects = declared_projects_from_environment()
    errors: list[str] = []
    for path in census_candidates(request):
        try:
            census_projects, census_names = declared_lane_objects(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        return projects | census_projects, census_names, str(path)
    raise ValueError("no readable lane census: " + ("; ".join(errors) or "none found"))
