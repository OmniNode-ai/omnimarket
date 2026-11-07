# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real probe and actuator of the host bring-up run, over ProtocolHostShell (OMN-19130).

A host's declared steps (the overlay ``overlays/host_shell_steps.yaml``) bind a
target to one shell primitive. The primitive renders a probe that exits 0 when
the target is satisfied and 1 when it is not, and, where the primitive has one,
an apply. The probe of a target with no declared step reads ``unknown``, and so
does any probe whose transport failed or whose exit is neither 0 nor 1: unknown
is never satisfied. The one exception is ``transport_answers``, whose check is
the transport itself: a transport that refuses reads ``not_ok``.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path

import yaml

from omnimarket.handlers.handler_host_shell_scripted import STEP_HEADER
from omnimarket.models.host_bringup.model_host_shell import (
    EnumShellPrimitive,
    ModelDeclaredShellStep,
    ModelHostShellOverlay,
    ModelHostShellSteps,
)
from omnimarket.nodes.node_host_bringup_orchestrator.models.model_host_bringup_run import (
    ModelActionResult,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan import (
    EnumObservedState,
    ModelBringupGraph,
    ModelBringupTarget,
)
from omnimarket.protocols.protocol_host_shell import ProtocolHostShell

OVERLAY_PATH = Path(__file__).resolve().parent.parent / "overlays/host_shell_steps.yaml"

# sudo -n answering is the privilege check itself; enabling a unit needs root.
_PROBE_PRIVILEGED = frozenset({EnumShellPrimitive.PRIVILEGE_ANSWERS})
_APPLY_PRIVILEGED = frozenset({EnumShellPrimitive.SERVICE_ACTIVE})


def load_host_shell_overlay(path: Path = OVERLAY_PATH) -> ModelHostShellOverlay:
    return ModelHostShellOverlay.model_validate(yaml.safe_load(path.read_text()))


def _q(value: str | None) -> str:
    assert value is not None
    return shlex.quote(value)


def _header(step: ModelDeclaredShellStep, kind: str) -> str:
    return f"{STEP_HEADER}{step.target} {kind}\n"


def render_probe(step: ModelDeclaredShellStep) -> str:
    """The read-only check: exit 0 satisfied, exit 1 not, anything else unknown."""
    p = step.primitive
    if p in (
        EnumShellPrimitive.TRANSPORT_ANSWERS,
        EnumShellPrimitive.PRIVILEGE_ANSWERS,
    ):
        body = "exit 0\n"
    elif p is EnumShellPrimitive.HOSTNAME_IS:
        body = f'[ "$(hostname)" = {_q(step.expected)} ] && exit 0\nexit 1\n'
    elif p is EnumShellPrimitive.DIRECTORY:
        body = f"[ -d {_q(step.path)} ] && exit 0\nexit 1\n"
    elif p is EnumShellPrimitive.LINE_IN_FILE:
        body = (
            f"[ -f {_q(step.path)} ] && grep -qxF -- {_q(step.line)} {_q(step.path)} "
            "&& exit 0\nexit 1\n"
        )
    elif p is EnumShellPrimitive.SERVICE_ACTIVE:
        body = f"systemctl is-active --quiet {_q(step.unit)} && exit 0\nexit 1\n"
    elif p is EnumShellPrimitive.MOUNT_SOURCE:
        # The topmost mount at the path is the one a reader of the path sees.
        body = (
            f"top=$(findmnt -n -o FSTYPE,SOURCE --mountpoint {_q(step.path)} | tail -n 1)\n"
            '[ -n "$top" ] || exit 1\n'
            "set -- $top\n"
            f'[ "$1" = {_q(step.fstype)} ] || exit 1\n'
            f'case "$2" in {_q(step.source)}*) exit 0 ;; esac\n'
            "exit 1\n"
        )
    elif p is EnumShellPrimitive.GLOB_ABSENT:
        assert step.pattern is not None
        body = (
            f"for f in {_q(step.path)}/{step.pattern}; do\n"
            '  [ -e "$f" ] && exit 1\n'
            "done\nexit 0\n"
        )
    else:  # pragma: no cover - the enum is closed
        raise ValueError(f"no probe for {p.value}")
    return _header(step, "probe") + body


def render_apply(step: ModelDeclaredShellStep) -> str | None:
    """The apply, or None for a primitive that only reads."""
    p = step.primitive
    if p is EnumShellPrimitive.DIRECTORY:
        body = f"install -d -m {_q(step.mode)} {_q(step.path)}\n"
    elif p is EnumShellPrimitive.LINE_IN_FILE:
        body = (
            "set -e\n"
            f"[ -f {_q(step.path)} ] && grep -qxF -- {_q(step.line)} {_q(step.path)} && exit 0\n"
            f"printf '%s\\n' {_q(step.line)} >> {_q(step.path)}\n"
        )
    elif p is EnumShellPrimitive.SERVICE_ACTIVE:
        body = f"systemctl enable --now {_q(step.unit)}\n"
    else:
        return None
    return _header(step, "act") + body


class _HostSteps:
    def __init__(self, overlay: ModelHostShellOverlay) -> None:
        self.overlay = overlay
        self._hosts: dict[str, ModelHostShellSteps] = {h.host: h for h in overlay.hosts}

    def find(
        self, host: str, target: str
    ) -> tuple[ModelHostShellSteps, ModelDeclaredShellStep] | None:
        entry = self._hosts.get(host)
        if entry is None:
            return None
        for step in entry.steps:
            if step.target == target:
                return entry, step
        return None

    def targets(self, host: str) -> tuple[str, ...]:
        entry = self._hosts.get(host)
        return tuple(s.target for s in entry.steps) if entry else ()


def _probe_privileged(step: ModelDeclaredShellStep) -> bool:
    return step.privileged or step.primitive in _PROBE_PRIVILEGED


def _apply_privileged(step: ModelDeclaredShellStep) -> bool:
    return step.privileged or step.primitive in _APPLY_PRIVILEGED


class HandlerShellTargetProbe:
    """ProtocolTargetProbe over ProtocolHostShell and the declared steps."""

    def __init__(
        self, *, shell: ProtocolHostShell, overlay: ModelHostShellOverlay
    ) -> None:
        self._shell = shell
        self._steps = _HostSteps(overlay)
        self.readings: list[tuple[str, str, EnumObservedState, str]] = []

    def read(self, host: str, target: ModelBringupTarget) -> EnumObservedState:
        found = self._steps.find(host, target.name)
        if found is None:
            self.readings.append(
                (host, target.name, EnumObservedState.UNKNOWN, "no declared step")
            )
            return EnumObservedState.UNKNOWN
        entry, step = found
        result = self._shell.run(
            entry.transport,
            render_probe(step),
            privileged=_probe_privileged(step),
            timeout_s=self._steps.overlay.probe_timeout_s,
        )
        if result.transport_failed:
            state = (
                EnumObservedState.NOT_OK
                if step.primitive is EnumShellPrimitive.TRANSPORT_ANSWERS
                else EnumObservedState.UNKNOWN
            )
        elif result.exit_code == 0:
            state = EnumObservedState.OK
        elif result.exit_code == 1:
            state = EnumObservedState.NOT_OK
        else:
            state = EnumObservedState.UNKNOWN
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
        self.readings.append(
            (host, target.name, state, f"exit {result.exit_code} {detail}".strip())
        )
        return state


class HandlerShellTargetActuator:
    """ProtocolTargetActuator over ProtocolHostShell and the declared steps."""

    def __init__(
        self, *, shell: ProtocolHostShell, overlay: ModelHostShellOverlay
    ) -> None:
        self._shell = shell
        self._steps = _HostSteps(overlay)

    def act(
        self, host: str, target: ModelBringupTarget, fence: int
    ) -> ModelActionResult:
        found = self._steps.find(host, target.name)
        if found is None:
            return ModelActionResult(
                ok=False,
                detail=f"{host}: {target.name} has no declared step to act with",
            )
        entry, step = found
        script = render_apply(step)
        if script is None:
            return ModelActionResult(
                ok=False,
                detail=f"{host}: {target.name} primitive {step.primitive.value} only reads",
            )
        result = self._shell.run(
            entry.transport,
            script,
            privileged=_apply_privileged(step),
            timeout_s=self._steps.overlay.action_timeout_s,
        )
        if result.transport_failed or result.exit_code != 0:
            tail = (
                result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
            )
            return ModelActionResult(
                ok=False,
                detail=(
                    f"{host}: {target.name} apply expected exit 0, observed "
                    f"{result.exit_code}{' (transport)' if result.transport_failed else ''}"
                    f"{': ' + tail if tail else ''}"
                ),
            )
        return ModelActionResult(ok=True)


def declared_subgraph(
    graph: ModelBringupGraph, targets: Iterable[str], top: str
) -> ModelBringupGraph:
    """The rows of ``targets`` with needs cut to those rows, under one aggregate top.

    Runs only what a host has declared steps for; every other row of the graph is
    left out, never read as satisfied.
    """
    by_name: Mapping[str, ModelBringupTarget] = {t.name: t for t in graph.targets}
    keep = [n for n in targets if n in by_name]
    missing = sorted(set(targets) - set(by_name))
    if missing:
        raise ValueError(
            f"declared steps name no target of the graph: {', '.join(missing)}"
        )
    if top in by_name:
        raise ValueError(f"top {top} is already a target of the graph")
    rows = [
        by_name[n].model_copy(
            update={
                "top": False,
                "needs": tuple(x for x in by_name[n].needs if x in keep),
            }
        )
        for n in keep
    ]
    stage = max(r.stage for r in rows)
    aggregate = ModelBringupTarget.model_validate(
        {
            "name": top,
            "stage": stage,
            "top": True,
            "needs": tuple(keep),
            "check": {"kind": "aggregate", "subject": top.replace("-", "_")},
            "falsifier": "any declared step reading unsatisfied keeps this unsatisfied",
            "action": "no_action",
        }
    )
    return ModelBringupGraph(targets=(*rows, aggregate))


__all__ = [
    "OVERLAY_PATH",
    "HandlerShellTargetActuator",
    "HandlerShellTargetProbe",
    "declared_subgraph",
    "load_host_shell_overlay",
    "render_apply",
    "render_probe",
]
