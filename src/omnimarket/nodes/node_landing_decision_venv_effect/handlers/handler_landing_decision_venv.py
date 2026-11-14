# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install every dependency the landing decision pin names into the decision venv (OMN-24355).

The landing controller's decision environment is a venv built from the landing decision pin. The pin names each
dependency by key and version; the overlay holds the rows from a pin key to a distribution, the interpreter arguments
and the timeouts, so this handler holds no setting. A run creates the venv when it is absent, reads which of the
distributions already stand at their pinned version, installs when any does not, and reads again. A second run on a
venv that already holds every pinned version installs nothing and answers ``ready``. Every failure ends as a typed
refusal; the handler raises nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.nodes.node_landing_decision_venv_effect.handlers.adapter_subprocess_runner import (
    AdapterSubprocessRunner,
)
from omnimarket.nodes.node_landing_decision_venv_effect.models.model_landing_decision_venv import (
    ModelCommandOutcome,
    ModelLandingDecisionVenvOverlay,
    ModelLandingDecisionVenvRequest,
    ModelLandingDecisionVenvResult,
)
from omnimarket.nodes.node_landing_decision_venv_effect.protocols import (
    ProtocolCommandRunner,
)

OVERLAY_ID = "node_landing_decision_venv_effect"
VENV_PYTHON = Path("bin") / "python"
# Run by the venv's own interpreter with the distribution names as arguments: the installed version of each, or null.
PROBE = (
    "import importlib.metadata as m, json, sys\n"
    "found = {}\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        found[name] = m.version(name)\n"
    "    except m.PackageNotFoundError:\n"
    "        found[name] = None\n"
    "print(json.dumps(found))\n"
)


class HandlerLandingDecisionVenv:
    """``handle(ModelLandingDecisionVenvRequest) -> ModelLandingDecisionVenvResult``."""

    def __init__(
        self,
        *,
        overlay: ModelLandingDecisionVenvOverlay | None = None,
        runner: ProtocolCommandRunner | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._runner: ProtocolCommandRunner = runner or AdapterSubprocessRunner()
        self._environ = environ

    def handle(
        self, request: ModelLandingDecisionVenvRequest
    ) -> ModelLandingDecisionVenvResult:
        try:
            overlay = self._overlay or load_structured_overlay(
                OVERLAY_ID, ModelLandingDecisionVenvOverlay, self._environ
            )
        except NodeOverlayNotFoundError:
            return _refused(request, "overlay-not-found")
        except NodeOverlayError:
            return _refused(request, "overlay-broken")
        pin = _read_pin(Path(request.pin_path))
        if pin is None:
            return _refused(request, "pin-unreadable")
        wanted: dict[str, str] = {}
        missing: list[str] = []
        for key, dist in overlay.distributions.items():
            version = pin.get(key)
            if isinstance(version, str) and version:
                wanted[dist] = version
            else:
                missing.append(key)
        if missing:
            return _refused(request, f"pin-missing:{','.join(sorted(missing))}")

        python = Path(request.venv_dir) / VENV_PYTHON
        if not python.exists():
            created = self._runner.run(
                [
                    request.base_python,
                    *overlay.venv_args,
                    request.venv_dir,
                ],
                overlay.venv_timeout_seconds,
            )
            reason = _failure("venv-create", created)
            if reason:
                return _refused(request, reason)

        held = self._probe(python, wanted, overlay)
        if held is None:
            return _refused(request, "timeout:probe")
        if held == wanted:
            return ModelLandingDecisionVenvResult(
                correlation_id=request.correlation_id,
                outcome="ready",
                versions=held,
            )
        installed = self._runner.run(
            [
                str(python),
                *overlay.pip_args,
                *(f"{dist}=={version}" for dist, version in wanted.items()),
            ],
            overlay.pip_timeout_seconds,
        )
        reason = _failure("pip-install", installed)
        if reason:
            return _refused(request, reason)
        held = self._probe(python, wanted, overlay)
        if held is None:
            return _refused(request, "timeout:probe")
        short = sorted(
            dist for dist, version in wanted.items() if held.get(dist) != version
        )
        if short:
            return _refused(request, f"verify-failed:{','.join(short)}")
        return ModelLandingDecisionVenvResult(
            correlation_id=request.correlation_id,
            outcome="installed",
            versions=held,
        )

    def _probe(
        self,
        python: Path,
        wanted: Mapping[str, str],
        overlay: ModelLandingDecisionVenvOverlay,
    ) -> dict[str, str] | None:
        """The pinned distributions the venv holds, at their versions; ``None`` when the probe timed out."""
        outcome = self._runner.run(
            [str(python), "-c", PROBE, *wanted],
            overlay.probe_timeout_seconds,
        )
        if outcome.timed_out:
            return None
        try:
            found = json.loads(outcome.stdout) if outcome.returncode == 0 else {}
        except ValueError:
            found = {}
        if not isinstance(found, dict):
            return {}
        return {
            dist: version for dist, version in found.items() if isinstance(version, str)
        }


def _read_pin(path: Path) -> dict[str, object] | None:
    try:
        pin = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return pin if isinstance(pin, dict) else None


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
    request: ModelLandingDecisionVenvRequest, reason: str
) -> ModelLandingDecisionVenvResult:
    return ModelLandingDecisionVenvResult(
        correlation_id=request.correlation_id, outcome="refused", reason=reason
    )


def answer_command(
    payload: bytes,
    handler: HandlerLandingDecisionVenv,
    topics: Mapping[str, str],
) -> list[tuple[str, bytes]]:
    """Answer one command: the run-completed event of an install or a ready read, or one run-failed event."""
    try:
        request = ModelLandingDecisionVenvRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        )
        body: dict[str, object] = {
            "correlation_id": "unknown",
            "failure": f"bad-payload:{','.join(fields)}",
        }
        return [
            (
                topics["landing_decision_venv_run_failed"],
                json.dumps(body, sort_keys=True).encode("utf-8"),
            )
        ]
    result = handler.handle(request)
    if result.outcome == "refused":
        failed = {"correlation_id": result.correlation_id, "failure": result.reason}
        return [
            (
                topics["landing_decision_venv_run_failed"],
                json.dumps(failed, sort_keys=True).encode("utf-8"),
            )
        ]
    completed = {
        "correlation_id": result.correlation_id,
        "outcome": result.outcome,
        "versions": result.versions,
        "ok": True,
    }
    return [
        (
            topics["landing_decision_venv_run_completed"],
            json.dumps(completed, sort_keys=True).encode("utf-8"),
        )
    ]


__all__ = ["HandlerLandingDecisionVenv", "answer_command"]
