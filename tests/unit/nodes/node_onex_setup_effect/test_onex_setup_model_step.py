# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23207: ``onex setup`` ends in a proven delegation and runs unattended.

AC1  a key on stdin, on a fresh HOME against a stub key server, ends with a
     receipt whose provider and model are the ones named.
AC2  each missing input is refused before any step runs, naming its flag.
AC3  a wrong key fails the model step with exit 1, the step and the retry
     command named, the earlier steps kept; the retry with the right key ends.
AC4  ``local`` and optional ``ollama`` use the supplied endpoint without
     installing, starting or downloading a runtime or models.
AC5  the key is in no argv, stdout, stderr, log, state file or receipt.

The servers are real HTTP servers on 127.0.0.1. The one thing stubbed is the
process boundary: ``subprocess.run`` records the argv of every command the walk
starts and answers ``onex delegate`` with the receipt it would have written.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from omnibase_core.cli.cli_commands import cli
from pydantic import SecretStr

from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    HandlerOnexSetup,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
    HandlerSetupPreflight,
)
from omnimarket.nodes.node_onex_setup_effect.models import ModelOnexSetupRequest
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from tests.helpers.setup_shell_environment import isolate_setup_shell

pytestmark = pytest.mark.unit

MODEL = "stub-model-7b"
GOOD_KEY = f"sk-stub-good-{uuid.uuid4().hex}"
WRONG_KEY = f"sk-stub-wrong-{uuid.uuid4().hex}"


class _KeyServer(BaseHTTPRequestHandler):
    """An OpenAI-compatible server: it lists its model only for ``GOOD_KEY``."""

    def do_GET(self) -> None:
        if self.path != "/v1/models":
            self.send_error(404)
            return
        if self.headers.get("Authorization") not in (None, f"Bearer {GOOD_KEY}"):
            self._json(401, {"error": {"message": "invalid api key"}})
            return
        self._json(200, {"object": "list", "data": [{"id": MODEL}]})

    def do_POST(self) -> None:
        # The setup walk's one-token availability check (OMN-23413); delegation
        # itself goes through the recorded subprocess, never this server.
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        if self.headers.get("Authorization") not in (None, f"Bearer {GOOD_KEY}"):
            self._json(401, {"error": {"message": "invalid api key"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        model = json.loads(self.rfile.read(length) or b"{}").get("model")
        if model != MODEL:
            self._json(404, {"error": {"message": "unknown model"}})
            return
        self._json(
            200,
            {
                "object": "chat.completion",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    def _json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _KeyServer)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@dataclass
class Recorder:
    """Every subprocess the walk started, and the receipts the stub delegate wrote."""

    receipts_dir: Path
    answer_model: str = MODEL
    endpoint: str = "http://127.0.0.1/v1/chat/completions"
    argv: list[list[str]] = field(default_factory=list)
    receipts: list[Path] = field(default_factory=list)

    def __call__(self, command: Any, *args: Any, **kwargs: Any) -> Any:
        argv = [str(part) for part in command]
        self.argv.append(argv)
        if "delegate" not in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        backend = argv[argv.index("--backend-id") + 1]
        receipt = self.receipts_dir / f"run-{len(self.receipts)}" / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": backend,
                    "model": self.answer_model,
                    "endpoint": self.endpoint,
                }
            ),
            encoding="utf-8",
        )
        self.receipts.append(receipt)
        return subprocess.CompletedProcess(
            argv, 0, '{"status": "success"}\n', f"delegate artifacts: {receipt}"
        )

    def delegations(self) -> list[list[str]]:
        return [argv for argv in self.argv if "delegate" in argv]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh HOME; the credential store and delegation state live beside it."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("OMNIBASE_PATH", raising=False)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    isolate_setup_shell(monkeypatch)
    # These isolate the model/store/receipt boundary. Native machine probes have
    # their own real-Mac E2E and refusal controls; the subprocess recorder must
    # not impersonate sysctl or disk evidence.
    monkeypatch.setattr(
        HandlerSetupPreflight,
        "observe",
        lambda _self, _plan: ModelSetupObservation(step="preflight", status="success"),
    )
    store = tmp_path / "delegation" / "delegation.sqlite"
    store.parent.mkdir()
    for module in (
        "omnimarket.inference.local_byok_credential_adapter",
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup",
    ):
        monkeypatch.setattr(f"{module}.default_evidence_db_path", lambda: store)
    return fake_home


@pytest.fixture
def recorder(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: str
) -> Recorder:
    recording = Recorder(
        receipts_dir=tmp_path / "runs", endpoint=server + "/v1/chat/completions"
    )
    monkeypatch.setattr(subprocess, "run", recording)
    return recording


def _setup(args: list[str], stdin: str | None = None, **env: str) -> Result:
    merged: dict[str, str | None] = {"OMNIBASE_PATH": None, "OMNI_HOME": None}
    merged.update(env)
    return CliRunner().invoke(
        cli,
        ["setup", "--yes", "--component", "delegation", *args],
        input=stdin,
        env=merged,
    )


def _unattended(workspace: Path, *flags: str) -> list[str]:
    return ["--no-interactive", "--workspace", str(workspace), *flags]


def _receipt_line(result: Result) -> dict[str, str]:
    line = next(
        (ln for ln in result.output.splitlines() if ln.startswith("Receipt: ")), None
    )
    assert line is not None, result.output
    return dict(part.split("=", 1) for part in line.removeprefix("Receipt: ").split())


def test_model_step_proven_delegation(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    workspace = tmp_path / "ws"
    result = _setup(
        _unattended(
            workspace,
            "--provider",
            "openai_compatible",
            "--model",
            MODEL,
            "--url",
            server,
        ),
        stdin=f"{GOOD_KEY}\n",
    )

    assert result.exit_code == 0, result.output
    receipt = _receipt_line(result)
    assert receipt["provider"] == "openai_compatible"
    assert receipt["model"] == MODEL
    steps = re.findall(r"^\[success\] ([\w-]+)", result.output, re.MULTILINE)
    assert steps == [
        "preflight",
        "shell-safety",
        "workspace",
        "user_config",
        "runtime_config",
        "identity",
        "models",
    ]
    delegations = recorder.delegations()
    assert len(delegations) == 1, "the walk must run exactly one delegation"
    assert (
        delegations[0][delegations[0].index("--backend-id") + 1] == receipt["backend"]
    )


def test_model_step_refuses_a_delegation_that_answered_on_another_model(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    """Failing control for AC1: the receipt, not the request, decides the pass."""
    recorder.answer_model = "some-other-model"
    result = _setup(
        _unattended(
            tmp_path / "ws",
            "--provider",
            "openai_compatible",
            "--model",
            MODEL,
            "--url",
            server,
        ),
        stdin=f"{GOOD_KEY}\n",
    )
    assert result.exit_code == 1, result.output
    assert "[failure] models" in result.output
    assert "Receipt: " not in result.output


def _assert_missing_refused(
    home: Path,
    tmp_path: Path,
    recorder: Recorder,
    server: str,
    *,
    flags: list[str],
    with_workspace: bool,
    named: str,
) -> None:
    workspace = tmp_path / "ws"
    args = ["--no-interactive", *flags]
    if with_workspace:
        args += ["--workspace", str(workspace)]
    result = _setup(args, stdin="")

    assert result.exit_code == 1, result.output
    assert f"{named} is missing" in result.output
    assert "[done]" not in result.output
    assert "[unchanged]" not in result.output
    assert not (home / ".onex").exists(), "a step wrote before the refusal"
    assert not workspace.exists()
    assert recorder.argv == []


def test_unattended_missing_workspace(
    home: Path, tmp_path: Path, recorder: Recorder, server: str
) -> None:
    _assert_missing_refused(
        home,
        tmp_path,
        recorder,
        server,
        flags=["--provider", "local", "--url", server, "--model", MODEL],
        with_workspace=False,
        named="--workspace",
    )


def test_unattended_missing_provider(
    home: Path, tmp_path: Path, recorder: Recorder, server: str
) -> None:
    _assert_missing_refused(
        home,
        tmp_path,
        recorder,
        server,
        flags=["--url", server, "--model", MODEL],
        with_workspace=True,
        named="--provider",
    )


def test_unattended_missing_model(
    home: Path, tmp_path: Path, recorder: Recorder, server: str
) -> None:
    _assert_missing_refused(
        home,
        tmp_path,
        recorder,
        server,
        flags=["--provider", "local", "--url", server],
        with_workspace=True,
        named="--model",
    )


@pytest.mark.parametrize("provider", ["local", "openai_compatible"])
def test_local_missing_url_refused_before_any_step(
    home: Path, tmp_path: Path, recorder: Recorder, provider: str
) -> None:
    result = _setup(
        _unattended(tmp_path / "ws", "--provider", provider, "--model", MODEL),
        stdin=f"{GOOD_KEY}\n",
    )
    assert result.exit_code == 1, result.output
    assert "--url is missing" in result.output
    assert f"--provider {provider}" in result.output
    assert not (home / ".onex").exists()
    assert recorder.argv == []


def test_wrong_key_resumes(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    workspace = tmp_path / "ws"
    args = _unattended(
        workspace, "--provider", "openai_compatible", "--model", MODEL, "--url", server
    )
    failed = _setup(args, stdin=f"{WRONG_KEY}\n")

    assert failed.exit_code == 1, failed.output
    assert "[failure] models" in failed.output
    retry = re.search(r"^Retry: (onex setup.*)$", failed.output, re.MULTILINE)
    assert retry, failed.output
    assert "stdin" in failed.output, "the retry must say the key goes on stdin"
    assert WRONG_KEY not in failed.output
    # The earlier steps are kept.
    assert workspace.is_dir()
    assert (workspace / "config/onex/runtime/runtime_config.yaml").is_file()
    assert (home / ".onex/config.yaml").is_file()
    assert (home / ".onex/setup.json").is_file()
    assert recorder.delegations() == [], "a refused key must not reach a delegation"

    resumed = _setup(shlex.split(retry.group(1))[2:], stdin=f"{GOOD_KEY}\n")
    assert resumed.exit_code == 0, resumed.output
    assert _receipt_line(resumed)["model"] == MODEL
    assert len(recorder.delegations()) == 1


def test_local_url_unattended(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    result = _setup(
        _unattended(
            tmp_path / "ws", "--provider", "local", "--url", server, "--model", MODEL
        ),
        stdin="",
    )
    assert result.exit_code == 0, result.output
    receipt = _receipt_line(result)
    assert receipt["provider"] == "local"
    assert receipt["model"] == MODEL
    assert len(recorder.delegations()) == 1


def test_interactive_walk_asks_for_provider_url_and_model(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    result = _setup(
        ["--interactive", "--workspace", str(tmp_path / "ws")],
        stdin=f"local\n{server}\ny\n{MODEL}\n",
    )
    assert result.exit_code == 0, result.output
    assert "Provider" in result.output
    assert _receipt_line(result)["provider"] == "local"
    assert len(recorder.delegations()) == 1


def test_ollama_requires_an_explicit_endpoint_before_installation(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    result = _setup(
        _unattended(tmp_path / "ws", "--provider", "ollama", "--model", MODEL),
        stdin="",
    )
    assert result.exit_code != 0, result.output
    assert "--url" in result.output
    assert "[done]" not in result.output
    assert not (home / ".onex").exists()
    assert recorder.argv == []


@pytest.mark.parametrize("authenticated", [False, True])
def test_ollama_uses_supplied_endpoint_without_managing_runtime(
    home: Path, tmp_path: Path, server: str, recorder: Recorder, authenticated: bool
) -> None:
    """Catch model allowlists, implicit localhost, and install/start/pull effects."""
    args = _unattended(
        tmp_path / "ws", "--provider", "ollama", "--model", MODEL, "--url", server
    )
    if authenticated:
        args += ["--endpoint-auth", "api-key"]
    result = _setup(args, stdin=f"{GOOD_KEY}\n" if authenticated else "")
    assert result.exit_code == 0, result.output
    assert _receipt_line(result)["provider"] == "ollama"
    assert len(recorder.delegations()) == 1
    assert all("delegate" in argv for argv in recorder.argv), recorder.argv
    assert GOOD_KEY not in result.output
    state = home / ".onex" / "setup.json"
    assert GOOD_KEY not in state.read_text()
    before = state.read_bytes()
    database = tmp_path / "delegation" / "delegation.sqlite"
    database_before = (database.read_bytes(), database.stat().st_mtime_ns)
    again = _setup([*args, "--action", "verify"], stdin="")
    assert again.exit_code == 0, again.output
    assert len(recorder.delegations()) == 1
    assert state.read_bytes() == before
    assert (database.read_bytes(), database.stat().st_mtime_ns) == database_before


def test_ollama_preserves_an_existing_local_endpoint(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
        ModelModelSetupRequest,
    )

    owner = handler_model_setup.HandlerModelSetup()
    owner.handle(
        ModelModelSetupRequest(operation="add_local", url=server, models=(MODEL,))
    )
    original = owner.overrides_path().read_text()
    second = server + "/separate-server"
    recorder.endpoint = second + "/v1/chat/completions"
    result = _setup(
        _unattended(
            tmp_path / "ws", "--provider", "ollama", "--model", MODEL, "--url", second
        ),
        stdin="",
    )
    assert result.exit_code == 0, result.output
    rows = handler_model_setup.local_url_entries(owner.overrides_path())
    assert len(rows) == 2
    assert rows[0]["endpoint_url"] == server + "/v1/chat/completions"
    assert rows[1]["endpoint_url"] == recorder.endpoint
    assert original != owner.overrides_path().read_text()
    assert _receipt_line(result)["backend"] == rows[1]["backend_id"]


def test_ollama_authentication_cannot_share_a_credential_slot_in_one_plan(
    tmp_path: Path,
) -> None:
    from pydantic import ValidationError

    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
        ModelSetupPlan,
    )

    with pytest.raises(ValidationError, match="credential"):
        ModelSetupPlan(
            workspace=tmp_path,
            providers=(
                {
                    "provider": "ollama",
                    "models": [MODEL],
                    "url": "http://localhost:11434",
                    "api_key_required": True,
                },
                {
                    "provider": "openai_compatible",
                    "models": [MODEL],
                    "url": "https://example.com/v1",
                },
            ),
        )


def test_unattended_keyed_provider_without_a_key_is_refused(
    home: Path, tmp_path: Path, server: str, recorder: Recorder
) -> None:
    result = _setup(
        _unattended(
            tmp_path / "ws",
            "--provider",
            "openai_compatible",
            "--model",
            MODEL,
            "--url",
            server,
        ),
        stdin="",
    )
    assert result.exit_code == 1, result.output
    assert "credential" in result.output.lower()
    assert not (home / ".onex").exists()


# --- AC5: the key is nowhere it must not be --------------------------------


def _where_the_key_is(
    key: str,
    *,
    recorder: Recorder,
    outputs: list[str],
    roots: list[Path],
    keep_out: tuple[str, ...] = ("delegation.sqlite",),
) -> list[str]:
    """Every place ``key`` appears among argv, outputs, receipts and state files."""
    found: list[str] = []
    for argv in recorder.argv:
        if any(key in part for part in argv):
            found.append(f"argv: {argv[:3]}")
    for index, text in enumerate(outputs):
        if key in text:
            found.append(f"output #{index}")
    for receipt in recorder.receipts:
        if key in receipt.read_text(encoding="utf-8"):
            found.append(f"delegation receipt {receipt}")
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.name.startswith(keep_out):
                continue
            if key.encode() in path.read_bytes():
                found.append(f"file {path}")
    return found


def _run_for_the_sentinel(
    home: Path, tmp_path: Path, server: str, key: str, recorder: Recorder
) -> tuple[Result, Path]:
    recorder.endpoint = server + "/v1/chat/completions"
    workspace = tmp_path / "ws"
    result = _setup(
        _unattended(
            workspace,
            "--provider",
            "openai_compatible",
            "--model",
            MODEL,
            "--url",
            server,
        ),
        stdin=f"{key}\n",
    )
    return result, workspace


class _KeyAnyServer(_KeyServer):
    """Accepts whichever key the sentinel run presents."""

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": MODEL}]})
        else:
            self.send_error(404)


@pytest.fixture
def any_key_server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _KeyAnyServer)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_sentinel_key_never_leaks(
    home: Path,
    tmp_path: Path,
    any_key_server: str,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = f"sk-sentinel-{uuid.uuid4().hex}"
    caplog.set_level("DEBUG")
    result, workspace = _run_for_the_sentinel(
        home, tmp_path, any_key_server, sentinel, recorder
    )
    assert result.exit_code == 0, result.output
    assert recorder.argv, "the recorder saw no subprocess: nothing was checked"
    assert recorder.delegations(), "the walk started no delegation: nothing was proven"
    state = home / ".onex" / "setup.json"
    before_compatibility_call = state.read_bytes()
    assert json.loads(before_compatibility_call)["schema_version"] == 2

    handler_result = HandlerOnexSetup().handle(
        ModelOnexSetupRequest(
            onex_home=home / ".onex",
            workspace=workspace,
            provider="openai_compatible",
            model=MODEL,
            url=any_key_server,
            key=SecretStr(sentinel),
            unattended=True,
        )
    )
    assert handler_result.receipt is not None
    assert state.read_bytes() == before_compatibility_call, (
        "the legacy contract handler replaced the versioned setup authority"
    )

    found = _where_the_key_is(
        sentinel,
        recorder=recorder,
        outputs=[
            result.stdout,
            result.stderr,
            result.output,
            caplog.text,
            handler_result.model_dump_json(),
            repr(ModelOnexSetupRequest(onex_home=home, key=SecretStr(sentinel))),
        ],
        roots=[home, workspace, tmp_path / "delegation"],
    )
    assert found == []


def test_sentinel_scan_catches_a_key_put_in_argv(
    home: Path,
    tmp_path: Path,
    any_key_server: str,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing control for AC5: a named break, the key in the delegation's prompt."""
    sentinel = f"sk-sentinel-{uuid.uuid4().hex}"
    monkeypatch.setattr(handler_model_setup, "_TEST_PROMPT", f"say ok {sentinel}")
    result, workspace = _run_for_the_sentinel(
        home, tmp_path, any_key_server, sentinel, recorder
    )
    assert result.exit_code == 0, result.output

    found = _where_the_key_is(
        sentinel,
        recorder=recorder,
        outputs=[result.stdout, result.stderr],
        roots=[home, workspace],
    )
    assert any(item.startswith("argv") for item in found), found
