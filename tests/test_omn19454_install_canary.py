# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Customer install canary checks, independent of installed script packages."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from threading import Thread
from types import ModuleType
from urllib.request import Request, urlopen

import pytest
import yaml

pytestmark = pytest.mark.unit
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/ci/install_canary"
HINT = (
    "uv tool install --with 'omnibase-infra>=0.38.4' "
    "--with 'omnimarket>=0.4.205' 'omnibase-core>=0.46.8'"
)


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


canary = load_script("run_install_canary")
stub = load_script("stub_openai_server")


@pytest.fixture
def marketplace() -> dict[str, object]:
    return {
        "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
        "name": "omninode-tools",
        "version": "2.1.0",
        "plugins": [
            {"name": "unrelated"},
            {
                "name": "onex",
                "source": "./plugins/onex-delegate",
                "requires": {
                    "onex_cli": {
                        "package": "omnibase-infra",
                        "min_version": "0.38.4",
                        "console_script_package": "omnibase-core",
                        "console_script_min_version": "0.46.8",
                        "node_package": "omnimarket",
                        "node_package_min_version": "0.4.205",
                        "install_hint": HINT,
                        "cwd_independent": True,
                        "installable_from_pypi": True,
                    }
                },
            },
        ],
    }


def test_extract_install_hint(marketplace: dict[str, object]) -> None:
    assert canary.extract_install_hint(marketplace) == HINT


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"plugins": {}},
        {"plugins": [{"name": "onex"}]},
        {"plugins": [{"name": "onex", "requires": {"onex_cli": {"install_hint": ""}}}]},
    ],
)
def test_missing_hint(value: dict[str, object]) -> None:
    with pytest.raises(canary.CanaryError, match="install_hint"):
        canary.extract_install_hint(value)


def test_plant_changes_only_core() -> None:
    assert canary.plant_bad_pin(HINT) == HINT.replace("core>=0.46.8", "core>=999.0.0")


@pytest.mark.parametrize(
    "hint",
    [
        "uv tool install omnibase-core",
        "uv tool install 'other-omnibase-core>=0.46.8'",
        "uv tool install 'omnibase-core>=999.0.0'",
    ],
)
def test_plant_cannot_silently_noop(hint: str) -> None:
    with pytest.raises(canary.CanaryError, match="plant_bad_pin"):
        canary.plant_bad_pin(hint)


def test_build_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    poisoned = [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GLM_API_KEY",
        "LLM_API_KEY",
        "LLM_OPENROUTER_API_KEY",
        "PYTHONPATH",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_INDEX_URL",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "BASH_ENV",
        "VIRTUAL_ENV",
    ]
    for key in poisoned:
        monkeypatch.setenv(key, "ambient-value")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = canary.build_env(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["PATH"] == str(tmp_path / ".local/bin") + os.pathsep + "/usr/bin:/bin"
    assert not set(poisoned).intersection(env)


def test_overrides_shape(tmp_path: Path) -> None:
    canary.write_bifrost_overrides(tmp_path, 12345)
    config = yaml.safe_load(
        (tmp_path / ".omninode/delegation/bifrost_overrides.yaml").read_text()
    )
    assert config == {
        "backends": [
            {
                "backend_id": backend,
                "endpoint_url": "http://127.0.0.1:12345/v1/chat/completions",
                "model_name": "canary-stub-model",
            }
            for backend in ("local-coder", "local-heavy-reasoning")
        ]
    }


@pytest.fixture
def stub_url() -> Iterator[str]:
    server = stub.serve(0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_stub_round_trip(stub_url: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with urlopen(stub_url + "/v1/models", timeout=5) as response:
        assert json.load(response) == {
            "object": "list",
            "data": [{"id": "canary-stub-model", "object": "model"}],
        }
    request = Request(
        stub_url + "/v1/chat/completions",
        data=json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=5) as response:
        result = json.load(response)
    assert result["choices"][0]["message"]["content"] == "canary-ok"
    assert result["model"] == "canary-stub-model"
    assert result["object"] == "chat.completion"
    assert all(
        type(result["usage"][key]) is int
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        )
    )
    assert "request_count=2" in caplog.text


def write_artifacts(cwd: Path, port: int = 12345) -> Path:
    run = cwd / ".onex_state/runs/test-run"
    run.mkdir(parents=True)
    (cwd.parent / "stub-port").write_text(str(port))
    (run / "result.txt").write_text("canary-ok\n")
    (run / "receipt.json").write_text(
        json.dumps(
            {
                "status": "success",
                "endpoint": f"http://127.0.0.1:{port}/v1/chat/completions",
                "model": "canary-stub-model",
            }
        )
    )
    (run / "run.json").write_text(json.dumps({"prompt": "say hello in one word"}))
    return run


def test_good_artifacts(tmp_path: Path) -> None:
    write_artifacts(tmp_path / "run")
    canary.assert_run_artifacts(tmp_path / "run")


@pytest.mark.parametrize(
    ("filename", "replacement", "check"),
    [
        ("receipt.json", None, "receipt.json: missing"),
        ("receipt.json", '{"status": "failed"}', "status"),
        ("receipt.json", '{"status": "success", "model": "wrong"}', "model"),
        (
            "receipt.json",
            '{"status": "success", "model": "canary-stub-model", "endpoint": "http://127.0.0.1:9999/v1/chat/completions"}',
            "endpoint",
        ),
        ("result.txt", "hello", "result.txt"),
        ("run.json", "{broken", "JSON check"),
        ("run.json", '{"prompt": "something else"}', "prompt"),
    ],
)
def test_reject_bad_artifacts(
    tmp_path: Path, filename: str, replacement: str | None, check: str
) -> None:
    run = write_artifacts(tmp_path / "run")
    path = run / filename
    if replacement is None:
        path.unlink()
    else:
        path.write_text(replacement)
    with pytest.raises(canary.CanaryError, match=check):
        canary.assert_run_artifacts(tmp_path / "run")


def test_reject_multiple_runs(tmp_path: Path) -> None:
    run = write_artifacts(tmp_path / "run")
    (run.parent / "extra").mkdir()
    with pytest.raises(canary.CanaryError, match="run count"):
        canary.assert_run_artifacts(tmp_path / "run")


@pytest.mark.parametrize("fail_install", [False, True])
def test_main_stages(
    tmp_path: Path,
    marketplace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    fail_install: bool,
) -> None:
    market = tmp_path / "marketplace.json"
    market.write_text(json.dumps(marketplace))
    summary_path = tmp_path / "summary.json"
    work = tmp_path / "work"
    commands: list[list[str]] = []

    def fake_command(
        command: list[str], env: dict[str, str], cwd: Path, timeout: int
    ) -> str:
        commands.append(command)
        if command[0] == "bash":
            assert command == [
                "bash",
                "-c",
                canary.plant_bad_pin(HINT) if fail_install else HINT,
            ]
            assert timeout == 900
            assert cwd == work
            home = Path(env["HOME"])
            assert list(home.iterdir()) == []
            if fail_install:
                raise canary.CanaryError("unsatisfiable planted pin")
            binary = home / ".local/bin/onex"
            binary.parent.mkdir(parents=True)
            binary.touch()
        elif command[1] == "delegate":
            assert command == [
                "onex",
                "delegate",
                "--task-type",
                "research",
                canary.PROMPT,
            ]
            assert timeout == 300
            assert cwd == work / "run"
            write_artifacts(cwd, int((work / "stub-port").read_text()))
        return "tenant_id: canary\n"

    monkeypatch.setattr(canary, "run_command", fake_command)
    argv = [
        "canary",
        "--marketplace-json",
        str(market),
        "--work-dir",
        str(work),
        "--summary-json",
        str(summary_path),
    ]
    if fail_install:
        argv.append("--plant-bad-pin")
    monkeypatch.setattr(sys, "argv", argv)
    assert canary.main() == int(fail_install)
    summary = json.loads(summary_path.read_text())
    assert summary["plant_bad_pin"] is fail_install
    assert summary["hint"] == (canary.plant_bad_pin(HINT) if fail_install else HINT)
    assert summary["failed_stage"] == ("install" if fail_install else None)
    expected = (
        ["install"]
        if fail_install
        else [
            "install",
            "version",
            "local_init",
            "stub_start",
            "overrides",
            "delegate",
            "artifacts",
        ]
    )
    assert [entry["stage"] for entry in summary["stages"]] == expected
    assert len(commands) == (1 if fail_install else 4)
    assert summary["stages"][-1]["status"] == ("failed" if fail_install else "passed")


def test_build_env_passes_only_named_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSSL_DIR", "/opt/openssl")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-value")
    env = canary.build_env(tmp_path, passthrough=["OPENSSL_DIR"])
    assert env["OPENSSL_DIR"] == "/opt/openssl"
    assert "ANTHROPIC_API_KEY" not in env


def test_build_env_named_variable_must_be_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENSSL_DIR", raising=False)
    with pytest.raises(canary.CanaryError, match="OPENSSL_DIR"):
        canary.build_env(tmp_path, passthrough=["OPENSSL_DIR"])


WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/install-canary-nightly.yml"
)


def test_workflow_covers_intel_macos_from_source() -> None:
    """Intel macOS installs from source; the canary must follow that recipe."""
    text = WORKFLOW.read_text()
    workflow = yaml.safe_load(text)
    matrix = workflow["jobs"]["canary"]["strategy"]["matrix"]
    assert "macos-15-intel" in matrix["os"]
    assert "ubuntu-latest" in matrix["os"]
    assert "macos-latest" in matrix["os"]
    assert "brew install openssl@3 rust" in text
    assert "OPENSSL_DIR" in text
    assert "--pass-env OPENSSL_DIR" in text
    # The recipe steps run on the Intel leg only.
    steps = workflow["jobs"]["canary"]["steps"]
    recipe = [s for s in steps if "brew install" in s.get("run", "")]
    assert len(recipe) == 1
    assert "macos-15-intel" in recipe[0]["if"]
