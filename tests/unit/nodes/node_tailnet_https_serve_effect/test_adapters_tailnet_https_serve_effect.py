# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real seams against a fake ``tailscale`` binary and a loopback http server, and the overlay's own refusals.

The fake tailscale keeps its listener table in a json file, answers ``serve status --json`` the way the real one does
(``Web`` keyed ``host:port``) and serves only on ``--https=`` (a ``--http=`` flag is an error). That makes the
failing control real: the same install, with the https flag broken to ``--http=``, goes red.
"""

from __future__ import annotations

import http.server
import json
import stat
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_tailnet_https_serve_effect.adapters import adapters_real
from omnimarket.nodes.node_tailnet_https_serve_effect.adapters.adapters_real import (
    AppendReporter,
    HttpUpstreamProbe,
    TailscaleServeBackend,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.handlers.handler_tailnet_https_serve import (
    HandlerTailnetHttpsServe,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.models import (
    EnumServeMode,
    EnumServeOutcome,
    ModelServeListener,
    ModelServeOverlay,
    ModelServeReport,
    ModelServeResult,
    ModelTailscaleBackend,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.protocols import ServeStepError
from tests.unit.nodes.node_tailnet_https_serve_effect.support import (
    FOREIGN,
    LISTENER,
    OTHER_PORT,
    PORT,
    request,
)

pytestmark = pytest.mark.unit

FAKE_TAILSCALE = r"""#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_TAILSCALE_STATE"]
cfg = json.load(open(state)) if os.path.exists(state) else {"Web": {}}
args = sys.argv[1:]
if args[:2] == ["serve", "status"]:
    print(json.dumps(cfg))
elif args[:1] == ["serve"]:
    flags = [a for a in args if a.startswith("--https=")]
    if not flags:
        sys.exit("fake tailscale: serve needs --https=<port> here: " + " ".join(args))
    key = "host.example.invalid:" + flags[0].split("=")[1]
    if "off" in args:
        cfg["Web"].pop(key, None)
    else:
        cfg["Web"][key] = {"Handlers": {"/": {"Proxy": args[-1]}}}
    json.dump(cfg, open(state, "w"))
else:
    sys.exit("fake tailscale: unhandled " + " ".join(args))
"""


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_: object) -> None:
        return


@pytest.fixture
def upstream() -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def fake_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "tailscale"
    binary.write_text(FAKE_TAILSCALE, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_TAILSCALE_STATE", str(tmp_path / "ts.json"))
    return binary


def _served(tmp_path: Path) -> dict[str, str]:
    state = tmp_path / "ts.json"
    if not state.exists():
        return {}
    web = json.loads(state.read_text(encoding="utf-8"))["Web"]
    return {k: v["Handlers"]["/"]["Proxy"] for k, v in web.items()}


def _handler(
    tmp_path: Path, binary: Path, upstream_url: str, reported: list[ModelServeResult]
) -> tuple[HandlerTailnetHttpsServe, TailscaleServeBackend]:
    backend = TailscaleServeBackend(
        ModelTailscaleBackend(binary=str(binary), command_prefix=(), timeout_seconds=30)
    )

    class Reporter:
        def report(self, result: ModelServeResult) -> None:
            reported.append(result)

    listener = LISTENER.model_copy(
        update={
            "upstream_url": upstream_url,
            "env_file": str(tmp_path / "etc" / "serve.env"),
        }
    )
    return (
        HandlerTailnetHttpsServe(
            listener=listener,
            probe=HttpUpstreamProbe(),
            backend=backend,
            reporter=Reporter(),
        ),
        backend,
    )


def test_install_status_and_uninstall_through_the_real_adapters(
    tmp_path: Path, fake_host: Path, upstream: str
) -> None:
    reported: list[ModelServeResult] = []
    handler, _ = _handler(tmp_path, fake_host, upstream, reported)
    result = handler.handle(request(EnumServeMode.INSTALL))
    assert result.outcome is EnumServeOutcome.INSTALLED, result.detail
    assert _served(tmp_path) == {f"host.example.invalid:{PORT}": upstream}
    env = (tmp_path / "etc" / "serve.env").read_text(encoding="utf-8")
    assert f"EXAMPLE_HTTPS_PORT={PORT}\n" in env
    assert f"EXAMPLE_UPSTREAM={upstream}\n" in env
    assert (
        handler.handle(request(EnumServeMode.STATUS)).outcome
        is EnumServeOutcome.SERVING
    )
    assert (
        handler.handle(request(EnumServeMode.UNINSTALL)).outcome
        is EnumServeOutcome.UNINSTALLED
    )
    assert _served(tmp_path) == {}
    assert not (tmp_path / "etc" / "serve.env").exists()
    assert len(reported) == 3


def test_a_dead_upstream_through_the_http_probe_is_refused(
    tmp_path: Path, fake_host: Path
) -> None:
    handler, _ = _handler(tmp_path, fake_host, "http://127.0.0.1:9", [])
    result = handler.handle(request(EnumServeMode.INSTALL))
    assert result.outcome is EnumServeOutcome.REFUSED_DEAD_UPSTREAM
    assert _served(tmp_path) == {}
    assert not (tmp_path / "etc" / "serve.env").exists()


def test_a_foreign_listener_on_the_port_survives_install_and_uninstall(
    tmp_path: Path, fake_host: Path, upstream: str
) -> None:
    handler, backend = _handler(tmp_path, fake_host, upstream, [])
    backend.serve(PORT, FOREIGN)
    backend.serve(OTHER_PORT, FOREIGN)
    assert (
        handler.handle(request(EnumServeMode.INSTALL)).outcome
        is EnumServeOutcome.REFUSED_FOREIGN_LISTENER
    )
    assert (
        handler.handle(request(EnumServeMode.UNINSTALL)).outcome
        is EnumServeOutcome.NOT_INSTALLED
    )
    assert _served(tmp_path) == {
        f"host.example.invalid:{PORT}": FOREIGN,
        f"host.example.invalid:{OTHER_PORT}": FOREIGN,
    }


def test_the_other_listener_stays_after_our_uninstall(
    tmp_path: Path, fake_host: Path, upstream: str
) -> None:
    handler, backend = _handler(tmp_path, fake_host, upstream, [])
    backend.serve(OTHER_PORT, FOREIGN)
    handler.handle(request(EnumServeMode.INSTALL))
    handler.handle(request(EnumServeMode.UNINSTALL))
    assert _served(tmp_path) == {f"host.example.invalid:{OTHER_PORT}": FOREIGN}


def test_failing_control_the_http_flag_instead_of_the_https_flag_goes_red(
    tmp_path: Path,
    fake_host: Path,
    upstream: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The control the 8 ported installer tests carried: break `--https=` to `--http=` and the install must not pass.
    monkeypatch.setattr(adapters_real, "SERVE_PORT_FLAG", "--http=")
    handler, _ = _handler(tmp_path, fake_host, upstream, [])
    result = handler.handle(request(EnumServeMode.INSTALL))
    assert result.outcome is EnumServeOutcome.BACKEND_FAILED
    assert "needs --https" in result.detail or "serve exited" in result.detail
    assert _served(tmp_path) == {}


def test_a_missing_binary_is_a_backend_failure_naming_the_step(
    tmp_path: Path,
) -> None:
    backend = TailscaleServeBackend(
        ModelTailscaleBackend(
            binary=str(tmp_path / "absent"), command_prefix=(), timeout_seconds=5
        )
    )
    with pytest.raises(ServeStepError) as caught:
        backend.served_target(PORT)
    assert caught.value.step == "serve"


def test_env_placement_and_removal_use_the_command_prefix(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def run(argv: object, timeout: int) -> object:
        import subprocess

        seen.append(list(argv))  # type: ignore[call-overload]
        return subprocess.CompletedProcess(argv, 0, "", "")  # type: ignore[arg-type]

    backend = TailscaleServeBackend(
        ModelTailscaleBackend(
            binary="tailscale", command_prefix=("sudo", "-n"), timeout_seconds=5
        ),
        run=run,  # type: ignore[arg-type]
    )
    backend.place_env("/etc/example/serve.env", "A=1\n")
    backend.remove_env("/etc/example/serve.env")
    assert seen[0] == ["sudo", "-n", "install", "-d", "-m", "0755", "/etc/example"]
    assert seen[1][:5] == ["sudo", "-n", "install", "-m", "0644"]
    assert seen[1][-1] == "/etc/example/serve.env"
    assert seen[2] == ["sudo", "-n", "rm", "-f", "/etc/example/serve.env"]


def test_the_ledger_row_carries_the_outcome_and_no_pipe_in_the_detail() -> None:
    reporter = AppendReporter(
        ModelServeReport(
            append_argv=("onex-ledger", "/srv/ledger.md", "--append"),
            lane="tailnet-https-serve",
            actor="node:node_tailnet_https_serve_effect",
            timeout_seconds=5,
        )
    )
    result = ModelServeResult(
        run_id="20261106T160000Z",
        mode=EnumServeMode.INSTALL,
        outcome=EnumServeOutcome.REFUSED_DEAD_UPSTREAM,
        detail="a | b",
        https_port=PORT,
        upstream_url="http://127.0.0.1:1",
    )
    row = reporter.row(result, datetime(2026, 11, 6, 16, 0, 0, tzinfo=UTC))
    assert row.startswith("2026-11-06T16:00:00Z | STATUS | lane=tailnet-https-serve")
    assert "outcome=refused-dead-upstream" in row
    assert row.endswith("a b")


def test_the_overlay_refuses_a_non_loopback_upstream() -> None:
    with pytest.raises(ValidationError):
        ModelServeListener.model_validate(
            {**LISTENER.model_dump(), "upstream_url": "http://203.0.113.5:3080"}
        )


def test_the_overlay_needs_the_block_that_matches_its_backend() -> None:
    base = {
        "listener": LISTENER.model_dump(),
        "report": {
            "append_argv": ["onex-ledger", "--append"],
            "lane": "x",
            "actor": "y",
            "timeout_seconds": 5,
        },
    }
    with pytest.raises(ValidationError):
        ModelServeOverlay.model_validate({**base, "backend": "tailscale"})
    with pytest.raises(ValidationError):
        ModelServeOverlay.model_validate({**base, "backend": "in-memory"})
    tailscale = {"binary": "tailscale", "command_prefix": [], "timeout_seconds": 5}
    assert ModelServeOverlay.model_validate(
        {**base, "backend": "tailscale", "tailscale": tailscale}
    )


def test_the_http_probe_answers_true_for_a_live_path_and_false_for_a_missing_one(
    upstream: str,
) -> None:
    probe = HttpUpstreamProbe()
    assert probe.answers(upstream, "/health", 5) is True
    assert probe.answers(upstream, "/missing", 5) is False
