# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's decision environment is built and checked by machine (OMN-24408).

The controller decides with a pinned decision node, run by an interpreter that must hold exactly the
pinned ``pydantic``, ``pydantic_core`` and ``pyyaml``. Before this node someone built that environment
by hand on the Mac; a drifted copy would change decisions silently. The tests run the handler three
ways: against the real interpreter of this test run (``bin/python`` is a wrapper that execs it, so the
versions it reports are real), against a fake runner for the build commands, and through the CLI.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_landing_decision_venv_effect import (
    runtime_landing_decision_venv_cli as cli,
)
from omnimarket.nodes.node_landing_decision_venv_effect.handlers import (
    HandlerLandingDecisionVenv,
)
from omnimarket.nodes.node_landing_decision_venv_effect.models import (
    ModelLandingDecisionVenvRequest,
    ModelLandingDecisionVenvResult,
)

pytestmark = pytest.mark.unit

COMMIT = "50ad28a50753e3c7e5cc1b63abd2d0768c16c364"
NODE_DIR = Path(cli.__file__).resolve().parent


def _pin(path: Path, **overrides: str) -> Path:
    """A decision pin holding the versions this interpreter really has, unless overridden."""
    body = {
        "commit": COMMIT,
        "pydantic": version("pydantic"),
        "pydantic_core": version("pydantic-core"),
        "pyyaml": version("pyyaml"),
    }
    body.update(overrides)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _real_env(root: Path) -> Path:
    """An environment directory whose interpreter is this run's own, so the versions read back are real."""
    python = root / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    return root


def _verify(
    tmp_path: Path, pin: Path, **extra: Path
) -> tuple[ModelLandingDecisionVenvRequest, ModelLandingDecisionVenvResult]:
    env = tmp_path / "venv"
    if not env.exists():
        _real_env(env)
    request = ModelLandingDecisionVenvRequest(
        operation="verify", pin_file=pin, venv_dir=env, **extra
    )
    return request, HandlerLandingDecisionVenv().handle(request)


def test_verify_accepts_an_environment_that_matches_the_pin(tmp_path: Path) -> None:
    _, result = _verify(tmp_path, _pin(tmp_path / "pin.json"))
    assert result.status == "verified", result.reasons
    assert result.pin_commit == COMMIT
    assert f"pydantic=={version('pydantic')}" in result.installed


@pytest.mark.parametrize("key", ["pydantic", "pydantic_core", "pyyaml"])
def test_verify_refuses_one_pinned_version_changed(tmp_path: Path, key: str) -> None:
    """Failing control: the same environment, one pinned version moved, is refused and names the library."""
    _, result = _verify(tmp_path, _pin(tmp_path / "pin.json", **{key: "0.0.1"}))
    assert result.status == "refused"
    assert len(result.reasons) == 1
    assert "but the pin says 0.0.1" in result.reasons[0]


def test_verify_refuses_a_missing_environment(tmp_path: Path) -> None:
    request = ModelLandingDecisionVenvRequest(
        operation="verify",
        pin_file=_pin(tmp_path / "pin.json"),
        venv_dir=tmp_path / "absent",
    )
    result = HandlerLandingDecisionVenv().handle(request)
    assert result.status == "refused"
    assert "no environment at" in result.reasons[0]


@pytest.mark.parametrize(
    "broken",
    [
        {"pydantic": "2.x"},
        {"pyyaml": "6.0.3; rm -rf /"},
        {"commit": "abc"},
    ],
)
def test_a_pin_without_exact_versions_is_refused(
    tmp_path: Path, broken: dict[str, str]
) -> None:
    _, result = _verify(tmp_path, _pin(tmp_path / "pin.json", **broken))
    assert result.status == "refused"
    assert result.reasons


def test_an_unreadable_pin_is_refused(tmp_path: Path) -> None:
    _, result = _verify(tmp_path, tmp_path / "no-such-pin.json")
    assert result.status == "refused"
    assert "cannot read the decision pin" in result.reasons[0]


def test_runtime_file_is_written_after_a_pass_and_removed_by_a_refusal(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "state" / "landing_runtime.json"
    src = tmp_path / "node-src"
    _, ok = _verify(
        tmp_path, _pin(tmp_path / "pin.json"), runtime_file=runtime, node_src=src
    )
    assert ok.status == "verified"
    written = json.loads(runtime.read_text(encoding="utf-8"))
    assert written == {
        "python": str(tmp_path / "venv" / "bin" / "python"),
        "node_src": str(src),
        "commit": COMMIT,
    }
    _, bad = _verify(
        tmp_path,
        _pin(tmp_path / "pin.json", pydantic="0.0.1"),
        runtime_file=runtime,
        node_src=src,
    )
    assert bad.status == "refused"
    assert not runtime.exists(), (
        "a refused environment must not stay findable by the controller"
    )


def test_runtime_file_without_node_src_is_refused(tmp_path: Path) -> None:
    _, result = _verify(
        tmp_path, _pin(tmp_path / "pin.json"), runtime_file=tmp_path / "rt.json"
    )
    assert result.status == "refused"


class _Recorder:
    """A runner that records each command and answers the build steps like a working interpreter."""

    def __init__(self, held: dict[str, str | None], fail: str = "") -> None:
        self.calls: list[list[str]] = []
        self._held = held
        self._fail = fail

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if self._fail and self._fail in argv:
            return subprocess.CompletedProcess(argv, 1, "", "boom: the step failed")
        if argv[1:3] == ["-m", "venv"]:
            if not Path(argv[-1]).exists():
                _real_env(Path(argv[-1]))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self._held), "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _held(pin_path: Path) -> dict[str, str | None]:
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    return {
        "pydantic": pin["pydantic"],
        "pydantic-core": pin["pydantic_core"],
        "pyyaml": pin["pyyaml"],
    }


def test_build_installs_exactly_the_pinned_versions_then_checks(tmp_path: Path) -> None:
    pin = _pin(
        tmp_path / "pin.json", pydantic="2.13.5", pydantic_core="2.46.5", pyyaml="6.0.3"
    )
    recorder = _Recorder(_held(pin))
    result = HandlerLandingDecisionVenv(recorder).handle(
        ModelLandingDecisionVenvRequest(
            operation="build",
            pin_file=pin,
            venv_dir=tmp_path / "decision-venv",
            base_python=Path("/opt/brew/bin/python3.13"),
        )
    )
    assert result.status == "built", result.reasons
    assert recorder.calls[0] == [
        "/opt/brew/bin/python3.13",
        "-m",
        "venv",
        "--clear",
        str(tmp_path / "decision-venv"),
    ]
    install = recorder.calls[1]
    assert install[1:6] == ["-m", "pip", "install", "--quiet", "--no-input"]
    assert install[6:] == ["pydantic==2.13.5", "pydantic-core==2.46.5", "pyyaml==6.0.3"]
    assert recorder.calls[2][1] == "-I", "the read-back runs isolated"


def test_build_refuses_when_the_installed_versions_drift_from_the_pin(
    tmp_path: Path,
) -> None:
    """A resolver that lands another version (or none) is caught by the read-back, not trusted."""
    pin = _pin(tmp_path / "pin.json")
    held = _held(pin)
    held["pyyaml"] = None
    held["pydantic"] = "0.0.1"
    result = HandlerLandingDecisionVenv(_Recorder(held)).handle(
        ModelLandingDecisionVenvRequest(
            operation="build",
            pin_file=pin,
            venv_dir=tmp_path / "decision-venv",
            base_python=Path("python3"),
        )
    )
    assert result.status == "refused"
    assert any("pyyaml is not installed" in reason for reason in result.reasons)
    assert any("pydantic is 0.0.1" in reason for reason in result.reasons)


@pytest.mark.parametrize("failing", ["venv", "pip"])
def test_build_failure_is_a_refusal_with_the_reason(
    tmp_path: Path, failing: str
) -> None:
    pin = _pin(tmp_path / "pin.json")
    result = HandlerLandingDecisionVenv(_Recorder(_held(pin), fail=failing)).handle(
        ModelLandingDecisionVenvRequest(
            operation="build",
            pin_file=pin,
            venv_dir=tmp_path / "decision-venv",
            base_python=Path("python3"),
        )
    )
    assert result.status == "refused"
    assert "boom: the step failed" in result.reasons[0]


def test_build_without_base_python_is_refused(tmp_path: Path) -> None:
    result = HandlerLandingDecisionVenv(_Recorder({})).handle(
        ModelLandingDecisionVenvRequest(
            operation="build",
            pin_file=_pin(tmp_path / "pin.json"),
            venv_dir=tmp_path / "decision-venv",
        )
    )
    assert result.status == "refused"


def test_cli_exit_code_follows_the_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _real_env(tmp_path / "venv")
    good = _pin(tmp_path / "good.json")
    assert cli.main(["verify", "--pin-file", str(good), "--venv-dir", str(env)]) == 0
    assert "landing_decision_venv=verified" in capsys.readouterr().out
    bad = _pin(tmp_path / "bad.json", pyyaml="0.0.1")
    assert cli.main(["verify", "--pin-file", str(bad), "--venv-dir", str(env)]) == 1
    out = capsys.readouterr().out
    assert "landing_decision_venv=refused" in out
    assert "pyyaml is" in out


def test_registered_node_declares_the_completion_topic() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["terminal_event"] in contract["externally_consumed_topics"]
    assert contract["metadata"]["ticket"] == "OMN-24408"
    pyproject = (NODE_DIR.parents[3] / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        'node_landing_decision_venv_effect = "omnimarket.nodes.node_landing_decision_venv_effect"'
        in pyproject
    )
