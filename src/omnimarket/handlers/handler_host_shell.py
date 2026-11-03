# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host measurement and actions over a shell: local, or ssh to the profile's target."""

import shlex
import subprocess
import sys
from collections.abc import Callable

from omnimarket.handlers.handler_host_profile import (
    HandlerHostProfileCompute,
    prepush_target_files,
)
from omnimarket.models.model_host_profile import (
    EnumGapActionKind,
    ModelGapAction,
    ModelHostMeasurement,
    ModelHostProfile,
    ModelHostProfileDeriveRequest,
    ModelHostStateRequest,
)

ShellRunner = Callable[[ModelHostProfile, str], str]
"""Runs a script on the host named by the profile and returns its stdout."""


def run_script(profile: ModelHostProfile, script: str) -> str:
    """Run a script through bash, locally or over the existing ssh transport."""
    if profile.ssh_target is None:
        argv = ["bash", "-s"]
    else:
        argv = ["ssh", "-o", "BatchMode=yes", profile.ssh_target, "bash", "-s"]
    done = subprocess.run(
        argv, input=script, text=True, capture_output=True, check=False, timeout=120
    )
    if done.stderr:
        sys.stderr.write(done.stderr)
    if done.returncode != 0:
        raise RuntimeError(f"{profile.host}: host script exited {done.returncode}")
    return done.stdout


def measure_script(profile: ModelHostProfile) -> str:
    derived = HandlerHostProfileCompute().handle(
        ModelHostProfileDeriveRequest(profile=profile)
    )
    q = shlex.quote
    lines = [
        "set -euo pipefail",
        f"test -d {q(profile.registry_root)}",
        f"test -r {q(profile.registry_root)}",
        f"test -x {q(profile.registry_root)}",
        f"for d in {q(profile.registry_root.rstrip('/'))}/*/; do "
        '[ -e "${d}.git" ] && printf "G\\t%s\\n" "$(basename "$d")"; done; true',
    ]
    for c in derived.clones:
        lines.append(f'[ -e {q(c.path)}/.git ] && printf "C\\t%s\\n" {q(c.path)}; true')
    for v in derived.venvs:
        lines.append(
            f'[ -x {q(v.venv_path)}/bin/python ] && printf "V\\t%s\\n" {q(v.venv_path)}; true'
        )
    if derived.prepush_workroot is not None:
        lines.append(f'[ -d {q(derived.prepush_workroot)} ] && printf "W\\n"; true')
    for target in prepush_target_files(profile):
        lines.append(
            f'[ -f {q(target)} ] && while IFS= read -r l; do printf "P\\t%s\\t%s\\n" {q(target)} "$l"; '
            f"done < {q(target)}; true"
        )
    if profile.liveness is not None:
        rows = q(profile.resolve(profile.liveness.rows_target))
        lines.append(f"[ -f {rows} ] && sed -n 's/^ *native_id: /L\\t/p' {rows}; true")
    glob = q(profile.unit_prefix + "*")
    for scope in ("--user", "--system"):
        lines.append(
            f"(systemctl {scope} list-unit-files --no-legend {glob} | "
            "awk '{print \"U\\t\" $1}') || "
            f"printf 'E\\t%s\\n' {q(scope + ' unit inventory unavailable')}"
        )
    return "\n".join(lines) + "\n"


def parse_measurement(profile: ModelHostProfile, output: str) -> ModelHostMeasurement:
    errors: list[str] = []
    git_dirs: list[str] = []
    clones: list[str] = []
    venvs: list[str] = []
    units: list[str] = []
    native_ids: list[str] = []
    rows: dict[str, list[str]] = {}
    workroot = False
    for raw in output.splitlines():
        tag, _, rest = raw.partition("\t")
        if tag == "E":
            errors.append(rest)
        elif tag == "G":
            git_dirs.append(rest)
        elif tag == "C":
            clones.append(rest)
        elif tag == "V":
            venvs.append(rest)
        elif tag == "W":
            workroot = True
        elif tag == "P":
            target, _, line = rest.partition("\t")
            rows.setdefault(target, []).append(line)
        elif tag == "L":
            native_ids.append(rest.strip())
        elif tag == "U":
            units.append(rest)
    return ModelHostMeasurement(
        host=profile.host,
        errors=tuple(errors),
        git_dirs_in_registry=tuple(git_dirs),
        present_clone_paths=tuple(clones),
        present_venvs=tuple(venvs),
        workroot_present=workroot,
        prepush_rows={k: tuple(v) for k, v in rows.items()},
        liveness_native_ids=tuple(native_ids),
        units=tuple(sorted(set(units))),
    )


def action_script(profile: ModelHostProfile, action: ModelGapAction) -> str:
    q = shlex.quote
    if action.kind is EnumGapActionKind.CLONE:
        url = profile.git_remote_template.format(repo=action.repo)
        return f"git clone --branch {q(action.ref)} {q(url)} {q(action.path)}\n"
    if action.kind is EnumGapActionKind.MKDIR:
        return f"mkdir -p {q(action.path)}\n"
    if action.kind is EnumGapActionKind.VENV:
        return f"uv venv --python {q(action.python)} {q(action.path)}\n"
    parent = action.path.rsplit("/", 1)[0]
    return f"set -e\nmkdir -p {q(parent)}\nprintf %s {q(action.content)} > {q(action.path)}\n"


class HandlerHostShell:
    """Implements the measurement and action ports through an injected shell runner."""

    def __init__(self, runner: ShellRunner = run_script) -> None:
        self._runner = runner

    def handle(self, request: ModelHostStateRequest) -> ModelHostMeasurement:
        """Read the host state as a contract-dispatched shared node operation."""
        return self.measure(request.profile)

    def measure(self, profile: ModelHostProfile) -> ModelHostMeasurement:
        return parse_measurement(
            profile, self._runner(profile, measure_script(profile))
        )

    def apply(self, profile: ModelHostProfile, action: ModelGapAction) -> None:
        self._runner(profile, action_script(profile, action))
