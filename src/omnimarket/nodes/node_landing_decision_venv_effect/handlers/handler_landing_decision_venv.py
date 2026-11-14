# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the landing controller's decision environment and refuse one that differs from the pin.

The controller decides what merges with the pinned decision node, run by an interpreter that holds
only the libraries the pin names (``pydantic``, ``pydantic_core``, ``pyyaml``). A copy whose versions
drift changes decisions silently, so every pass ends by asking the environment's own interpreter
which versions it holds and comparing them with the pin: any difference is a refusal, and a refusal
removes the ``landing_runtime.json`` the controller would read, so the controller cannot run on it.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..models import ModelLandingDecisionVenvRequest, ModelLandingDecisionVenvResult

# pin key -> distribution name the installer resolves and the environment reports
PINNED_LIBRARIES: dict[str, str] = {
    "pydantic": "pydantic",
    "pydantic_core": "pydantic-core",
    "pyyaml": "pyyaml",
}
VERSION = re.compile(r"[0-9]+(\.[0-9]+)*")
COMMIT = re.compile(r"[0-9a-f]{40}")
RUN_TIMEOUT_S = 600
PROBE = (
    "import importlib.metadata as m, json, sys\n"
    "out = {}\n"
    "for name in json.loads(sys.argv[1]):\n"
    "    try:\n"
    "        out[name] = m.version(name)\n"
    "    except m.PackageNotFoundError:\n"
    "        out[name] = None\n"
    "print(json.dumps(out))\n"
)

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False
    )


def _tail(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1] if lines else "no output"


def _refused(
    *reasons: str, commit: str = "", python: str = ""
) -> ModelLandingDecisionVenvResult:
    return ModelLandingDecisionVenvResult(
        status="refused", pin_commit=commit, python=python, reasons=tuple(reasons)
    )


def _read_pin(pin_file: Path) -> tuple[str, dict[str, str], list[str]]:
    """The pin's commit, its pinned versions by distribution name, and what is wrong with it."""
    try:
        pin = json.loads(pin_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "", {}, [f"cannot read the decision pin {pin_file}: {exc}"]
    if not isinstance(pin, dict):
        return "", {}, [f"the decision pin {pin_file} is not a JSON object"]
    problems: list[str] = []
    commit = pin.get("commit")
    if not isinstance(commit, str) or COMMIT.fullmatch(commit) is None:
        problems.append("the pin carries no 40-hex commit")
        commit = ""
    versions: dict[str, str] = {}
    for key, distribution in PINNED_LIBRARIES.items():
        value = pin.get(key)
        if not isinstance(value, str) or VERSION.fullmatch(value) is None:
            problems.append(f"the pin carries no exact version for {key}")
        else:
            versions[distribution] = value
    return commit, versions, problems


class HandlerLandingDecisionVenv:
    """Build (or only read) the decision environment, then check it against the pin."""

    def __init__(self, run: Runner = _run) -> None:
        self._run = run

    def handle(
        self, request: ModelLandingDecisionVenvRequest
    ) -> ModelLandingDecisionVenvResult:
        result = self._handle(request)
        if result.status == "refused" and request.runtime_file is not None:
            # the controller reads this file to find its interpreter: a refused environment must not be found
            request.runtime_file.unlink(missing_ok=True)
        return result

    def _handle(
        self, request: ModelLandingDecisionVenvRequest
    ) -> ModelLandingDecisionVenvResult:
        commit, pinned, problems = _read_pin(request.pin_file)
        if problems:
            return _refused(*problems, commit=commit)
        if (request.runtime_file is None) != (request.node_src is None):
            return _refused(
                "runtime_file and node_src are given together or not at all",
                commit=commit,
            )
        python = request.venv_dir / "bin" / "python"
        if request.operation == "build":
            if request.base_python is None:
                return _refused("build needs base_python", commit=commit)
            failed = self._build(request.base_python, request.venv_dir, pinned)
            if failed:
                return _refused(failed, commit=commit, python=str(python))
        elif not python.is_file():
            return _refused(f"no environment at {request.venv_dir}", commit=commit)
        held, reasons = self._read_back(python, pinned)
        if reasons:
            return _refused(*reasons, commit=commit, python=str(python))
        if request.runtime_file is not None and request.node_src is not None:
            request.runtime_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = request.runtime_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "python": str(python),
                        "node_src": str(request.node_src),
                        "commit": commit,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(request.runtime_file)
        return ModelLandingDecisionVenvResult(
            status="built" if request.operation == "build" else "verified",
            pin_commit=commit,
            python=str(python),
            installed=tuple(f"{name}=={held[name]}" for name in sorted(held)),
        )

    def _build(self, base_python: Path, venv_dir: Path, pinned: dict[str, str]) -> str:
        made = self._run([str(base_python), "-m", "venv", "--clear", str(venv_dir)])
        if made.returncode != 0:
            return f"venv creation failed: {_tail(made.stderr)}"
        python = venv_dir / "bin" / "python"
        installed = self._run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--no-input",
                *(f"{name}=={version}" for name, version in sorted(pinned.items())),
            ]
        )
        if installed.returncode != 0:
            return f"installing the pinned libraries failed: {_tail(installed.stderr)}"
        return ""

    def _read_back(
        self, python: Path, pinned: dict[str, str]
    ) -> tuple[dict[str, str], list[str]]:
        """Ask the environment's own interpreter what it holds; every difference is a reason."""
        probe = self._run([str(python), "-I", "-c", PROBE, json.dumps(sorted(pinned))])
        if probe.returncode != 0:
            return {}, [f"the environment's interpreter failed: {_tail(probe.stderr)}"]
        try:
            held = json.loads(probe.stdout)
        except ValueError:
            return {}, ["the environment's interpreter returned no version list"]
        if not isinstance(held, dict):
            return {}, ["the environment's interpreter returned no version list"]
        reasons = [
            f"{name} is {held.get(name) or 'not installed'} but the pin says {version}"
            for name, version in sorted(pinned.items())
            if held.get(name) != version
        ]
        return {name: str(held[name]) for name in pinned if held.get(name)}, reasons
