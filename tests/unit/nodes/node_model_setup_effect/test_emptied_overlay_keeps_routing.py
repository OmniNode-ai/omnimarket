# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Removing the last local model leaves every other model routable (OMN-23413).

Found 2026-11-13 from the operator's /config: Reflection AI's key was accepted
and its model added, but its test failed with "Bifrost delegation config schema
validation failed: backends Tuple should have at least 1 item ... not 0". The
routes file the TUI's home held was the single line `backends:`, written when
its last local model was removed. YAML reads that as null, and the loader's
deep merge put the null over the contract's backend list, so no backend at all
was left and every delegation test failed.

Failure modes:
  E1  removing the last local model writes a routes file that empties routing.
  E2  a routes file already holding a bare `backends:` empties routing.
  E3  delegate's own backend resolution fails on that file ("'NoneType' object
      is not iterable").
  E4  the incident replayed through the installed onex (add one local model, remove it, delegate, isolated HOME) crashes on null backends instead of refusing the removed backend (OMN-25069).
  E5  removing the last local model changes other keys of the routes file (OMN-25069 AC1).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    HandlerModelSetup,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
)

pytestmark = pytest.mark.unit


def test_removing_the_last_local_model_writes_no_null_backends() -> None:
    """E1: what the remove path writes, read back as YAML."""
    one = handler_model_setup._with_local_urls(
        "",
        [
            {
                "backend_id": handler_model_setup.local_url_backend_id(1),
                "endpoint_url": "http://models.test:8000/v1/chat/completions",
                "model_name": "m",
                **handler_model_setup._LOCAL_URL_DEFAULTS,
            }
        ],
    )
    emptied = handler_model_setup._with_local_urls(
        handler_model_setup._without_local_url(one), []
    )
    data = yaml.safe_load(emptied) or {}
    assert data.get("backends", []) is not None


def test_a_bare_backends_line_keeps_the_contracts_backends(tmp_path: Path) -> None:
    """E2: the file the operator's home already holds."""
    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text("backends:\n")
    config = load_bifrost_delegation_config(overlay_path=overlay)
    assert len(config.backends) > 0


def test_delegate_routing_reads_a_bare_backends_line(tmp_path: Path) -> None:
    """E3: `onex delegate` itself, not only the bifrost loader, reads the file.

    Seen 2026-11-13 in the operator's TUI: with the same `backends:` file every
    typed task failed "delegation failed: 'NoneType' object is not iterable"
    (run 618e3dc2), from `list(overlay.get("backends", []))`: the default
    applies to a missing key, not to a null one.
    """
    from omnimarket.handlers.routing.delegation_backend_resolution import (
        load_bifrost_backends,
    )

    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text("backends:\n")
    assert len(load_bifrost_backends(overlay_path=overlay)) > 0


@pytest.fixture
def overlay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        handler_model_setup,
        "default_evidence_db_path",
        lambda: tmp_path / "delegation.sqlite",
    )
    return tmp_path / "bifrost_overrides.yaml"


@pytest.mark.parametrize("keep_other", [False, True])
def test_remove_last_local_model_preserves_other_overlay_keys(
    overlay: Path, keep_other: bool
) -> None:
    """E5: the other keys and backends survive removing the last local model."""
    other = [{"backend_id": "byok-openai", "max_tokens": 1024}] if keep_other else []
    overlay.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    *other,
                    {
                        "backend_id": "local-url",
                        "endpoint_url": "http://127.0.0.1:9999/v1/chat/completions",
                        "model_name": "incident-model",
                    },
                ],
                "shadow_mode": {"enabled": False},
                "operator_note": "keep this",
            },
            sort_keys=False,
        )
    )
    HandlerModelSetup().handle(ModelModelSetupRequest(operation="remove_local"))
    written = yaml.safe_load(overlay.read_text())
    assert written.get("backends", []) == other
    assert written["shadow_mode"] == {"enabled": False}
    assert written["operator_note"] == "keep this"


class _LocalModel(BaseHTTPRequestHandler):
    def _send(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send({"object": "list", "data": [{"id": "incident-model"}]})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(
            {
                "id": "incident-response",
                "object": "chat.completion",
                "model": "incident-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "### ANSWER\nhello",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )

    def log_message(self, *_args: object) -> None:
        pass


@pytest.mark.integration
def test_remove_last_local_then_delegate(tmp_path: Path) -> None:
    """E4: A removed model gets a routing refusal, never a null-backends crash."""
    isolated = tmp_path / "home"
    isolated.mkdir()
    onex = Path(sys.executable).with_name("onex")
    env = {
        "HOME": str(isolated),
        "PATH": f"{onex.parent}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "ONEX_CLI_SHOW_ALL_COMMANDS": "1",
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def run(label: str, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(onex), *args],
            env=env,
            cwd=isolated,
            capture_output=True,
            text=True,
            timeout=100,
            check=False,
        )
        (tmp_path / f"{label}.log").write_text(result.stdout + result.stderr)
        return result

    try:
        added = run(
            "add",
            "models",
            "add",
            "local",
            "--url",
            f"http://127.0.0.1:{server.server_port}",
            "--model",
            "incident-model",
        )
        assert added.returncode == 0, added.stdout + added.stderr
        removed = run("remove", "models", "remove", "local")
        assert removed.returncode == 0, removed.stdout + removed.stderr
        overlay = isolated / ".omninode/delegation/bifrost_overrides.yaml"
        assert yaml.safe_load(overlay.read_text()).get("backends", []) == []
        delegated = run(
            "delegate",
            "delegate",
            "--bus",
            "inmemory",
            "--locus",
            "in-process",
            "--backend-id",
            "local-url",
            "--result-only",
            "hello",
        )
        output = delegated.stdout + delegated.stderr
        assert delegated.returncode != 0
        assert (
            "No delegation backend 'local-url' with a populated endpoint_url" in output
        ), output
        assert "schema validation failed" not in output
        receipt_path = Path(output.split("(receipt: ", 1)[1].strip().removesuffix(")"))
        assert receipt_path.is_relative_to(tmp_path / "state")
        receipt = json.loads(receipt_path.read_text())
        # The console shortens a long message in the middle, so the overlay's path is read from the receipt,
        # which keeps the full error: the refusal still names the file the user has to fix.
        assert str(overlay) in json.dumps(receipt), receipt
    finally:
        server.shutdown()
        server.server_close()
