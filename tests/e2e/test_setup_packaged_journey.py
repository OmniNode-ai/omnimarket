# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise customer commands from an explicitly installed wheel, outside source.

Failure controls: editable import, wrong model/endpoint receipt, missing served
run, unauthenticated dashboard, provisioning on replay, hidden child tools, and
stale tenant readiness. The HTTP model is a protocol fixture, not real inference.
Set ONEX_TEST_CLI and ONEX_TEST_WHEEL to opt in; the resulting artifact survives
both success and failure, and never contains credential values.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

pytestmark = pytest.mark.e2e


def test_dashboard_lifecycle_cli_preserves_profile_and_other_process(
    tmp_path: Path,
) -> None:
    cli = str(Path(sys.executable).parent / "onex")
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "PATH": str(Path(cli).parent) + ":/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
    }
    commands: list[dict[str, Any]] = []

    def invoke(*args: str) -> subprocess.CompletedProcess[str]:
        result = _run([cli, *args], environment, home, timeout=210)
        commands.append(
            {
                "argv": args,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        return result

    empty = invoke("dashboard", "status", "--json")
    assert empty.returncode != 0
    assert json.loads(empty.stdout)["status"] == "unavailable"
    assert not list(home.iterdir()), "Status on an empty home created files"
    missing = invoke("dashboard", "start", "--json")
    assert missing.returncode != 0
    assert "onex local init" in missing.stderr
    assert invoke("local", "init", "--json").returncode == 0
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(90)"], env=environment
    )
    try:
        first = invoke("dashboard", "start", "--json")
        assert first.returncode == 0, first.stdout + first.stderr
        process = json.loads(first.stdout)["evidence"]
        database = home / ".omninode" / "delegation" / "delegation.sqlite"
        metadata = home / ".onex" / "dashboard.json"
        snapshot = (
            database.stat().st_mtime_ns,
            database.read_bytes(),
            metadata.read_bytes(),
        )
        status = invoke("dashboard", "status", "--json")
        assert status.returncode == 0, status.stdout + status.stderr
        assert snapshot == (
            database.stat().st_mtime_ns,
            database.read_bytes(),
            metadata.read_bytes(),
        ), "Status mutated credential metadata"
        repeated = invoke("dashboard", "start", "--json")
        assert repeated.returncode == 0
        assert json.loads(repeated.stdout)["evidence"]["pid"] == process["pid"]
        assert metadata.read_bytes() == snapshot[2]
        stopped = invoke("dashboard", "stop", "--json")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert unrelated.poll() is None
        assert invoke("dashboard", "status", "--json").returncode != 0
    finally:
        # Direct owner cleanup also works while the CLI under test is red.
        _run(
            [
                str(Path(cli).parent / "python"),
                "-c",
                "from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard import HandlerSetupDashboard; HandlerSetupDashboard().stop()",
            ],
            environment,
            home,
            timeout=20,
        )
        unrelated.terminate()
        unrelated.wait(timeout=5)
        (tmp_path / "dashboard-cli.json").write_text(json.dumps(commands, indent=2))


def _run(
    command: list[str], environment: dict[str, str], cwd: Path, timeout: int = 90
) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(
        command,
        env=environment,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
    return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)


@pytest.mark.release_proof
@pytest.mark.parametrize("existing_profile", [False, True])
def test_built_package_setup_delegation_dashboard_journey(
    tmp_path: Path, existing_profile: bool
) -> None:
    executable = os.environ.get("ONEX_TEST_CLI")
    artifact_wheel = os.environ.get("ONEX_TEST_WHEEL")
    if not executable or not artifact_wheel:
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail("Packaged journey requires ONEX_TEST_CLI and ONEX_TEST_WHEEL")
        pytest.skip("Packaged journey requires ONEX_TEST_CLI and ONEX_TEST_WHEEL")
    cli, wheel = Path(executable).resolve(), Path(artifact_wheel).resolve()
    assert cli.is_file()
    assert wheel.is_file()
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    preserved = home / ".claude" / "settings.json"
    if existing_profile:
        preserved.parent.mkdir()
        preserved.write_text(
            json.dumps({"permissions": {"allow": ["Read"]}, "customer_owned": "keep"})
        )
    preserved_bytes = preserved.read_bytes() if existing_profile else None
    tools = tmp_path / "tool-sentinels"
    tools.mkdir()
    trace = tmp_path / "tool-processes.txt"
    for name in ("git", "ollama"):
        sentinel = tools / name
        sentinel.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$ONEX_TEST_TOOL_TRACE"\n'
            'if [ "$1" = "--onex-test-trace-control" ]; then exit 0; fi\nexit 127\n'
        )
        sentinel.chmod(0o700)
    environment = {
        "HOME": str(home),
        "PATH": str(tools) + ":" + str(cli.parent) + ":/usr/bin:/bin",
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "ONEX_TEST_TOOL_TRACE": str(trace),
        "LANG": "en_US.UTF-8",
    }
    control = _run(
        [str(tools / "git"), "--onex-test-trace-control"], environment, workspace
    )
    assert control.returncode == 0
    assert "--onex-test-trace-control" in trace.read_text()
    trace.write_text("")
    artifact = tmp_path / "packaged-journey.json"
    artifact.write_text(
        json.dumps(
            {
                "outcome": "failed",
                "phase": "installed-wheel-verification",
                "model_evidence": "protocol fixture only",
            },
            indent=2,
        )
    )
    packages: dict[str, Any] = {}
    wheels = {"omnimarket": wheel}
    for distribution, variable in (
        ("omnibase_core", "ONEX_TEST_CORE_WHEEL"),
        ("omniclaude", "ONEX_TEST_CLAUDE_WHEEL"),
    ):
        value = os.environ.get(variable)
        assert value, f"Packaged customer journey requires {variable}"
        wheels[distribution] = Path(value).resolve()
    for distribution, package_wheel in wheels.items():
        assert package_wheel.is_file(), f"Missing wheel for {distribution}"
        installed = _run(
            [
                str(cli.parent / "python"),
                "-c",
                "import json,pathlib,importlib.metadata as m,importlib,sys,zipfile; "
                "name=sys.argv[2];d=m.distribution('omninode-claude' if name=='omniclaude' else name);module=importlib.import_module(name); "
                "z=zipfile.ZipFile(sys.argv[1]);names=[n for n in z.namelist() if n.startswith(name+'/') and not n.endswith('/')]; "
                "matches=all(d.locate_file(n).read_bytes()==z.read(n) for n in names); "
                "print(json.dumps({'version':d.version,'source':str(pathlib.Path(module.__file__).resolve()),'direct_url':d.read_text('direct_url.json'),'wheel_matches':matches,'files_checked':len(names)}))",
                str(package_wheel),
                distribution,
            ],
            environment,
            workspace,
        )
        assert installed.returncode == 0, installed.stderr
        package = json.loads(installed.stdout)
        assert "/site-packages/" in package["source"], package
        direct = json.loads(package["direct_url"] or "{}")
        assert not direct.get("dir_info", {}).get("editable"), package
        assert package["files_checked"] > 0
        assert package["wheel_matches"], (
            f"Installed {distribution} differs from supplied wheel"
        )
        package["wheel_sha256"] = hashlib.sha256(package_wheel.read_bytes()).hexdigest()
        packages[distribution] = package
    model = "customer-model-not-in-any-registry:fixture"
    requests: list[dict[str, Any]] = []

    class ModelServer(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def respond(self, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/v1/models":
                self.respond({"object": "list", "data": [{"id": model}]})
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append({"path": self.path, "model": body.get("model")})
            if self.path != "/v1/chat/completions" or body.get("model") != model:
                self.send_error(400)
                return
            self.respond(
                {
                    "id": "fixture-completion",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 17,
                        "completion_tokens": 1,
                        "total_tokens": 18,
                    },
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelServer)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    evidence: dict[str, Any] = {
        "packages": packages,
        "existing_profile": existing_profile,
        "tool_trace_scope": "PATH-resolved git and ollama children; absolute executable paths are outside this sentinel trace",
        "model_evidence": "HTTP protocol fixture, not actual model proof",
        "commands": [],
        "outcome": "failed",
    }
    artifact = tmp_path / "packaged-journey.json"

    def invoke(*args: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        result = _run([str(cli), *args], environment, workspace, timeout)
        evidence["commands"].append(
            {
                "argv": list(args),
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        artifact.write_text(json.dumps(evidence, indent=2))
        return result

    try:
        assets = _run(
            [
                str(cli.parent / "python"),
                "-c",
                "import importlib.metadata as m,json,pathlib; e=tuple(m.entry_points(group='onex.claude',name='customer_assets')); assert len(e)==1; a=e[0].load()();p=pathlib.Path(a['marketplace']);print(json.dumps({'marketplace':str(p),'manifest_present':(p/'.claude-plugin'/'marketplace.json').is_file()}))",
            ],
            environment,
            workspace,
        )
        assert assets.returncode == 0, assets.stderr
        bundle = json.loads(assets.stdout)
        assert bundle["manifest_present"]
        assert "/site-packages/" in bundle["marketplace"]
        evidence["claude_local_bundle"] = bundle
        discovered = invoke("models", "discover", "--url", endpoint, "--json")
        assert discovered.returncode == 0, discovered.stderr
        assert json.loads(discovered.stdout)["models"] == [model]
        setup_args = (
            "setup",
            "--no-interactive",
            "--yes",
            "--json",
            "--workspace",
            str(workspace),
            "--component",
            "delegation",
            "--component",
            "dashboard",
            "--connection",
            "fixture-server",
            "--provider",
            "ollama",
            "--model",
            model,
            "--url",
            endpoint,
        )
        first = invoke(*setup_args, timeout=240)
        assert first.returncode == 0, first.stdout + first.stderr
        journal = json.loads(first.stdout)
        observations = {item["step"]: item for item in journal["observations"]}
        assert observations["models"]["status"] == "success"
        assert observations["dashboard"]["status"] == "success"
        receipt_paths = list((tmp_path / "state" / "runs").glob("*/receipt.json"))
        assert receipt_paths, "Setup produced no canonical delegation receipt"
        receipts = [json.loads(path.read_text()) for path in receipt_paths]
        accepted = [
            receipt for receipt in receipts if receipt.get("status") == "success"
        ]
        assert accepted, receipts
        receipt = accepted[-1]
        result = receipt["receipt"]["result"]
        assert result["model_name"] == model
        assert receipt["endpoint"].startswith(endpoint)
        assert requests
        assert all(row["model"] == model for row in requests)
        correlation = receipt["correlation_id"]
        dashboard = observations["dashboard"]["evidence"]
        # Read the credential through the canonical store in the installed process.
        # It is only sent to the loopback server, never included in the artifact.
        readback = _run(
            [
                str(cli.parent / "python"),
                "-c",
                "import asyncio; from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore; "
                "print(asyncio.run(LocalByokCredentialStore().get_secret('setup.dashboard.session')) or '')",
            ],
            environment,
            workspace,
        )
        assert readback.returncode == 0
        assert readback.stdout.strip()
        token = readback.stdout.strip()
        with httpx.Client(trust_env=False, timeout=15) as client:
            assert client.get(dashboard["url"] + "/projections").status_code == 401
            headers = {"authorization": "Bearer " + token}
            listing = client.get(dashboard["url"] + "/projections", headers=headers)
            assert listing.status_code == 200
            assert listing.json()["tenant"] == result["tenant_id"]
            topic = "onex.snapshot.projection.delegation.decisions.v1"
            served = client.get(
                dashboard["url"] + "/projection/" + quote(topic, safe=""),
                params={"correlation_id": correlation},
                headers=headers,
            )
            assert served.status_code == 200, served.text
            assert any(
                row.get("correlation_id") == correlation
                for row in served.json()["rows"]
            ), served.text
            evidence["served_projection"] = served.json()
        before = (home / ".onex" / "setup.json").read_bytes()
        before_requests = len(requests)
        repeated = invoke(*setup_args, timeout=90)
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert (home / ".onex" / "setup.json").read_bytes() == before
        assert len(requests) == before_requests, "No-op setup repeated model inference"
        assert all(
            not row["changed"] for row in json.loads(repeated.stdout)["observations"]
        )
        profile_source = tmp_path / "saved-plan.json"
        verify_plan = dict(journal["plan"])
        verify_plan["action"] = "verify"
        profile_source.write_text(json.dumps(verify_plan))
        imported = invoke(
            "config", "import", str(profile_source), "--name", "customer", "--json"
        )
        assert imported.returncode == 0, imported.stdout + imported.stderr
        exported = invoke("config", "export", "--profile", "customer")
        assert exported.returncode == 0, exported.stdout + exported.stderr
        exported_file = tmp_path / "exported-profile.json"
        exported_file.write_text(exported.stdout)
        checked = invoke(
            "setup", "--plan", str(exported_file), "--json", "--no-interactive"
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert all(
            not row["changed"] for row in json.loads(checked.stdout)["observations"]
        )
        assert len(requests) == before_requests, (
            "Profile verification made an inference request"
        )
        if preserved_bytes is not None:
            assert preserved.read_bytes() == preserved_bytes
        assert token not in artifact.read_text()
        assert token in artifact.read_text() + token, "Secret detector positive control"

        tool_calls = trace.read_text().splitlines()
        assert not any(
            "ollama " in call
            or any(
                "git " + operation in call
                for operation in ("clone", "fetch", "checkout")
            )
            for call in tool_calls
        ), tool_calls
        evidence["path_tool_calls"] = tool_calls
        evidence["path_tool_trace_control"] = (
            "git sentinel recorded the deliberately invoked control"
        )
        evidence.update(
            outcome="passed", run_id=receipt["run_id"], correlation_id=correlation
        )
    finally:
        # The installed lifecycle verifies the recorded PID before stopping it.
        stopped = _run(
            [
                str(cli.parent / "python"),
                "-c",
                "from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard import HandlerSetupDashboard; HandlerSetupDashboard().stop()",
            ],
            environment,
            workspace,
            timeout=20,
        )
        evidence["dashboard_stop_exit"] = stopped.returncode
        evidence["model_requests"] = requests
        artifact.write_text(json.dumps(evidence, indent=2))
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("changed_tenant", [True, False])
def test_dashboard_observation_rejects_changed_current_tenant(
    tmp_path: Path, changed_tenant: bool
) -> None:
    """A still-live server for the previous identity is not current readiness."""
    from omnimarket.inference.local_byok_credential_adapter import (
        LocalByokCredentialStore,
    )
    from omnimarket.local_deployment.tenant_identity import mint_local_tenant_identity
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard import (
        HandlerSetupDashboard,
        _stamp,
    )
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_dashboard_process import (
        ModelSetupDashboardProcess,
    )
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
        ModelSetupPlan,
    )

    store = LocalByokCredentialStore(db_path=tmp_path / "current.sqlite")
    current = mint_local_tenant_identity(db_path=store.db_path)
    old = mint_local_tenant_identity(db_path=tmp_path / "old.sqlite")
    assert current.tenant_uuid != old.tenant_uuid
    served_tenant = old if changed_tenant else current
    asyncio.run(store.set_secret("test.dashboard.token", "test-secret"))

    class PreviousDashboard(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def do_GET(self) -> None:
            body = json.dumps({"tenant": str(served_tenant.tenant_uuid)}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), PreviousDashboard)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        handler = HandlerSetupDashboard(onex_home=tmp_path, store=store)
        stamp = _stamp(os.getpid())
        assert stamp
        handler._write(
            ModelSetupDashboardProcess(
                pid=os.getpid(),
                process_stamp=stamp,
                url=f"http://127.0.0.1:{server.server_port}",
                credential_ref="test.dashboard.token",
                tenant_id=str(served_tenant.tenant_uuid),
            )
        )
        snapshot = (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns)
        observation = handler.observe(
            ModelSetupPlan(workspace=tmp_path, components=("dashboard",))
        )
        if changed_tenant:
            assert observation.status != "success", (
                "Previous tenant still passed readiness"
            )
        else:
            assert observation.status == "success", observation
        assert snapshot == (
            store.db_path.read_bytes(),
            store.db_path.stat().st_mtime_ns,
        ), "Observation mutated the credential store"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
