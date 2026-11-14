# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the landing controller's decision environment and refuse one that differs from the pin.

The controller decides what merges with the pinned decision node, run by an interpreter that holds
only the libraries the pin names. The overlay holds the rows from a pin key to a distribution, the
interpreter arguments and the timeouts, so this handler holds no setting. A build creates the
environment when it is absent or differs from the pin and installs every pinned library; a build that
finds every pinned version already in place installs nothing and answers ``ready``. Every pass ends by
asking the environment's own interpreter which versions it holds and comparing them with the pin: any
difference is a refusal, and a refusal removes the ``landing_runtime.json`` the controller would read,
so the controller cannot run on it. Every failure ends as a typed refusal; the handler raises nothing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)

from ..models import (
    ModelCommandOutcome,
    ModelLandingDecisionVenvOverlay,
    ModelLandingDecisionVenvRequest,
    ModelLandingDecisionVenvResult,
)

OVERLAY_ID = "node_landing_decision_venv_effect"
VERSION = re.compile(r"[0-9]+(\.[0-9]+)*")
COMMIT = re.compile(r"[0-9a-f]{40}")
# Interpreter-location variables of the caller; an environment's interpreter must resolve its own packages.
INTERPRETER_ENV = ("PYTHONPATH", "PYTHONHOME")
# Run by the environment's own interpreter, isolated, with the distribution names as one JSON argument.
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

Runner = Callable[[Sequence[str], float], ModelCommandOutcome]


def _run(argv: Sequence[str], timeout_seconds: float) -> ModelCommandOutcome:
    env = {k: v for k, v in os.environ.items() if k not in INTERPRETER_ENV}
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ModelCommandOutcome(returncode=-1, timed_out=True)
    except OSError as exc:
        return ModelCommandOutcome(returncode=-1, os_error=type(exc).__name__)
    return ModelCommandOutcome(
        returncode=done.returncode, stdout=done.stdout, stderr=done.stderr
    )


def _tail(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1] if lines else "no output"


def _failure(step: str, outcome: ModelCommandOutcome) -> str:
    """The typed reason a command ended badly, or an empty string when it succeeded."""
    if outcome.timed_out:
        return f"timeout:{step}"
    if outcome.os_error:
        return f"{step}-failed:{outcome.os_error}"
    if outcome.returncode != 0:
        return f"{step}-failed:rc={outcome.returncode}"
    return ""


def _refused(
    reason: str, *reasons: str, commit: str = "", python: str = ""
) -> ModelLandingDecisionVenvResult:
    return ModelLandingDecisionVenvResult(
        status="refused",
        pin_commit=commit,
        python=python,
        reason=reason,
        reasons=reasons or (reason,),
    )


def _read_pin(
    pin_file: Path, rows: Mapping[str, str]
) -> tuple[str, dict[str, str], str, list[str]]:
    """The pin's commit, its versions by distribution name, the typed reason it is refused, and the prose."""
    try:
        pin = json.loads(pin_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return (
            "",
            {},
            "pin-unreadable",
            [f"cannot read the decision pin {pin_file}: {exc}"],
        )
    if not isinstance(pin, dict):
        return (
            "",
            {},
            "pin-unreadable",
            [f"the decision pin {pin_file} is not a JSON object"],
        )
    problems: list[str] = []
    codes: list[str] = []
    commit = pin.get("commit")
    if not isinstance(commit, str) or COMMIT.fullmatch(commit) is None:
        problems.append("the pin carries no 40-hex commit")
        codes.append("pin-invalid:commit")
        commit = ""
    versions: dict[str, str] = {}
    missing = sorted(key for key in rows if key not in pin)
    invalid = sorted(
        key
        for key in rows
        if key in pin
        and not (isinstance(pin[key], str) and VERSION.fullmatch(pin[key]))
    )
    for key in rows:
        if key in missing or key in invalid:
            continue
        versions[rows[key]] = pin[key]
    if missing:
        problems.append(f"the pin names no version for {', '.join(missing)}")
        codes.append(f"pin-missing:{','.join(missing)}")
    if invalid:
        problems.append(f"the pin carries no exact version for {', '.join(invalid)}")
        codes.append(f"pin-invalid:{','.join(invalid)}")
    return commit, versions, ";".join(codes), problems


class HandlerLandingDecisionVenv:
    """Build (or only read) the decision environment, then check it against the pin."""

    def __init__(
        self,
        run: Runner = _run,
        *,
        overlay: ModelLandingDecisionVenvOverlay | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._run = run
        self._overlay = overlay
        self._environ = environ

    def handle(
        self, request: ModelLandingDecisionVenvRequest
    ) -> ModelLandingDecisionVenvResult:
        result = self._handle(request)
        if result.status == "refused" and request.runtime_file is not None:
            # the controller reads this file to find its interpreter: a refused environment must not be found
            request.runtime_file.unlink(missing_ok=True)
        return result

    def _overlay_or_reason(
        self,
    ) -> tuple[ModelLandingDecisionVenvOverlay | None, str]:
        if self._overlay is not None:
            return self._overlay, ""
        try:
            return (
                load_structured_overlay(
                    OVERLAY_ID, ModelLandingDecisionVenvOverlay, self._environ
                ),
                "",
            )
        except NodeOverlayNotFoundError:
            return None, "overlay-not-found"
        except NodeOverlayError:
            return None, "overlay-broken"

    def _handle(
        self, request: ModelLandingDecisionVenvRequest
    ) -> ModelLandingDecisionVenvResult:
        overlay, overlay_reason = self._overlay_or_reason()
        if overlay is None:
            return _refused(overlay_reason)
        commit, pinned, code, problems = _read_pin(
            request.pin_file, overlay.distributions
        )
        if problems:
            return _refused(code, *problems, commit=commit)
        if (request.runtime_file is None) != (request.node_src is None):
            return _refused(
                "request-invalid",
                "runtime_file and node_src are given together or not at all",
                commit=commit,
            )
        python = request.venv_dir / "bin" / "python"
        status: Literal["built", "ready", "verified"] = "verified"
        if request.operation == "build":
            if request.base_python is None:
                return _refused(
                    "request-invalid", "build needs base_python", commit=commit
                )
            status = "ready"
            if not self._holds_all(python, pinned, overlay):
                status = "built"
                failed = self._build(
                    request.base_python, request.venv_dir, pinned, overlay
                )
                if failed:
                    return _refused(
                        failed[0], failed[1], commit=commit, python=str(python)
                    )
        elif not python.is_file():
            return _refused(
                "no-environment",
                f"no environment at {request.venv_dir}",
                commit=commit,
            )
        held, code, reasons = self._read_back(python, pinned, overlay)
        if reasons:
            return _refused(code, *reasons, commit=commit, python=str(python))
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
            status=status,
            pin_commit=commit,
            python=str(python),
            installed=tuple(f"{name}=={held[name]}" for name in sorted(held)),
        )

    def _holds_all(
        self,
        python: Path,
        pinned: Mapping[str, str],
        overlay: ModelLandingDecisionVenvOverlay,
    ) -> bool:
        """True when an environment already stands at every pinned version, so a build has nothing to do."""
        if not python.is_file():
            return False
        _, _, reasons = self._read_back(python, pinned, overlay)
        return not reasons

    def _build(
        self,
        base_python: Path,
        venv_dir: Path,
        pinned: Mapping[str, str],
        overlay: ModelLandingDecisionVenvOverlay,
    ) -> tuple[str, str] | None:
        """The typed reason and the prose of a failed build, or ``None`` when it installed."""
        made = self._run(
            [str(base_python), *overlay.venv_args, str(venv_dir)],
            overlay.venv_timeout_seconds,
        )
        reason = _failure("venv-create", made)
        if reason:
            return reason, f"venv creation failed: {reason}: {_tail(made.stderr)}"
        python = venv_dir / "bin" / "python"
        installed = self._run(
            [
                str(python),
                *overlay.pip_args,
                *(f"{name}=={version}" for name, version in sorted(pinned.items())),
            ],
            overlay.pip_timeout_seconds,
        )
        reason = _failure("pip-install", installed)
        if reason:
            return (
                reason,
                f"installing the pinned libraries failed: {reason}: {_tail(installed.stderr)}",
            )
        return None

    def _read_back(
        self,
        python: Path,
        pinned: Mapping[str, str],
        overlay: ModelLandingDecisionVenvOverlay,
    ) -> tuple[dict[str, str], str, list[str]]:
        """Ask the environment's own interpreter what it holds; every difference is a reason."""
        probe = self._run(
            [str(python), "-I", "-c", PROBE, json.dumps(sorted(pinned))],
            overlay.probe_timeout_seconds,
        )
        reason = _failure("probe", probe)
        if reason:
            return (
                {},
                reason,
                [
                    f"the environment's interpreter failed: {reason}: {_tail(probe.stderr)}"
                ],
            )
        try:
            held = json.loads(probe.stdout)
        except ValueError:
            held = None
        if not isinstance(held, dict):
            return (
                {},
                "probe-failed:no-version-list",
                ["the environment's interpreter returned no version list"],
            )
        short = sorted(
            name for name, version in pinned.items() if held.get(name) != version
        )
        reasons = [
            f"{name} is {held.get(name) or 'not installed'} but the pin says {pinned[name]}"
            for name in short
        ]
        code = f"version-mismatch:{','.join(short)}" if short else ""
        return (
            {name: str(held[name]) for name in pinned if held.get(name)},
            code,
            reasons,
        )
