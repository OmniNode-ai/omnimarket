# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A pin file, an overlay and a runner that models a venv, so the chains run without a network.

The overlay here is the test's own copy of the document the overlays repository holds. Its timeouts are distinct values
so a test can show that the handler passes the overlay's value to the runner and holds none of its own.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_landing_decision_venv_effect.models.model_landing_decision_venv import (
    ModelCommandOutcome,
    ModelLandingDecisionVenvOverlay,
    ModelLandingDecisionVenvRequest,
)

PIN = {
    "commit": "50ad28a50753e3c7e5cc1b63abd2d0768c16c364",
    "node": "src/omnimarket/nodes/node_pr_landing_decision_compute",
    "pydantic": "2.13.5",
    "pydantic_core": "2.46.5",
    "pyyaml": "6.0.3",
}
VENV_TIMEOUT = 11.0
PIP_TIMEOUT = 22.0
PROBE_TIMEOUT = 33.0
DIST_ROWS = {
    "pydantic": "pydantic",
    "pydantic_core": "pydantic-core",
    "pyyaml": "pyyaml",
}
CORRELATION = "chain-1"


def make_overlay(
    rows: dict[str, str] | None = None,
) -> ModelLandingDecisionVenvOverlay:
    return ModelLandingDecisionVenvOverlay(
        distributions=DIST_ROWS if rows is None else rows,
        venv_args=["-m", "venv"],
        pip_args=["-m", "pip", "install", "--quiet"],
        venv_timeout_seconds=VENV_TIMEOUT,
        pip_timeout_seconds=PIP_TIMEOUT,
        probe_timeout_seconds=PROBE_TIMEOUT,
    )


class FakeVenvRunner:
    """Models one venv: creating makes its interpreter, installing records ``name==version``, the probe reads them.

    ``fail`` names the step that answers a non-zero exit (``venv``, ``pip``), ``hang`` the step that times out
    (``venv``, ``pip``, ``probe``), and ``refuse_pip_for`` leaves one distribution out of an install that exits zero.
    """

    def __init__(
        self,
        *,
        fail: str = "",
        hang: str = "",
        os_error: str = "",
        refuse_pip_for: str = "",
    ) -> None:
        self.fail = fail
        self.hang = hang
        self.os_error = os_error
        self.refuse_pip_for = refuse_pip_for
        self.installed: dict[str, str] = {}
        self.calls: list[tuple[str, float]] = []

    def run(self, argv: Sequence[str], timeout_seconds: float) -> ModelCommandOutcome:
        step = _step(argv)
        self.calls.append((step, timeout_seconds))
        if self.os_error and step == "venv":
            return ModelCommandOutcome(returncode=-1, os_error=self.os_error)
        if self.hang == step:
            return ModelCommandOutcome(returncode=-1, timed_out=True)
        if self.fail == step:
            return ModelCommandOutcome(returncode=1)
        if step == "venv":
            interpreter = Path(argv[-1]) / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("", encoding="utf-8")
        elif step == "pip":
            for spec in argv:
                if "==" in spec:
                    name, version = spec.split("==")
                    if name != self.refuse_pip_for:
                        self.installed[name] = version
        else:
            names = argv[3:]
            return ModelCommandOutcome(
                returncode=0,
                stdout=json.dumps({n: self.installed.get(n) for n in names}),
            )
        return ModelCommandOutcome(returncode=0)

    def steps(self) -> list[str]:
        return [step for step, _ in self.calls]


def _step(argv: Sequence[str]) -> str:
    if argv[1:3] == ["-m", "venv"]:
        return "venv"
    if argv[1:3] == ["-m", "pip"]:
        return "pip"
    return "probe"


@pytest.fixture
def pin_file(tmp_path: Path) -> Path:
    path = tmp_path / "landing_decision.pin.json"
    path.write_text(json.dumps(PIN), encoding="utf-8")
    return path


@pytest.fixture
def make_request(
    tmp_path: Path, pin_file: Path
) -> Callable[..., ModelLandingDecisionVenvRequest]:
    def build(pin: Path | None = None) -> ModelLandingDecisionVenvRequest:
        return ModelLandingDecisionVenvRequest(
            correlation_id=CORRELATION,
            pin_path=str(pin or pin_file),
            venv_dir=str(tmp_path / "decision-venv"),
            base_python="/base/python3",
        )

    return build
