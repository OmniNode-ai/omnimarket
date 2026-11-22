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
import shutil
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
            # The readiness probe asks for hello; the normal task needs an
            # answer to its own question rather than that probe response.
            answer = "hello"
            if any(
                message.get("role") == "user"
                and "named connections" in str(message.get("content", "")).lower()
                for message in body.get("messages", [])
            ):
                answer = (
                    "### ANSWER\nNamed connections bind each endpoint to its own credential "
                    "and selected models. A key is resolved only for its configured "
                    "server. Separate identities prevent one endpoint from "
                    "overwriting another server's configuration. The default "
                    "selection chooses the exact named route while retaining "
                    "unrelated registrations."
                )
            self.respond(
                {
                    "id": "fixture-completion",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": answer,
                            },
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
        restricted = invoke("models", "only", "local")
        assert restricted.returncode == 0, restricted.stdout + restricted.stderr
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
            "local",
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
        tui_status = _run(
            [
                str(cli.parent / "python"),
                "-c",
                "import asyncio; from omnimarket.nodes.node_onex_setup_effect.cli_tui import load_status; "
                "print(asyncio.run(load_status()).model_dump_json())",
            ],
            environment,
            workspace,
        )
        assert tui_status.returncode == 0, tui_status.stderr
        evidence["tui_status"] = json.loads(tui_status.stdout)
        assert evidence["tui_status"]["models"] == 1, (
            "The TUI footer lost the named model setup just configured"
        )
        delegated = invoke(
            "delegate",
            "Explain why named connections keep model endpoints independent.",
            "--bus",
            "inmemory",
            "--locus",
            "in-process",
            "--criteria",
            "response_non_empty",
            "--criteria",
            "concise",
            "--criteria",
            "final_artifact_only",
            "--criteria-mode",
            "replace-task-class",
            "--json",
        )
        assert delegated.returncode == 0, delegated.stdout + delegated.stderr
        terminal = json.loads(delegated.stdout)
        receipt_paths = list((tmp_path / "state" / "runs").glob("*/receipt.json"))
        assert receipt_paths, "Setup produced no canonical delegation receipt"
        receipts = [json.loads(path.read_text()) for path in receipt_paths]
        accepted = [
            receipt for receipt in receipts if receipt.get("status") == "success"
        ]
        assert accepted, receipts
        receipt = next(row for row in accepted if row["run_id"] == terminal["run_id"])
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


@pytest.mark.release_proof
def test_customer_launchers_only(tmp_path: Path) -> None:
    """AC2: `uv tool install omninode-onex` puts exactly onex and omni-agent on PATH.

    Opt in with ONEX_TEST_ONEX_WHEEL (the built omninode-onex wheel); the
    distributions it pins resolve from PyPI. The control installs omnibase-core
    the same way and must expose more than the customer launchers, which shows
    the bin-directory read can see an extra launcher.
    """
    supplied = os.environ.get("ONEX_TEST_ONEX_WHEEL")
    if not supplied:
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail("Customer launcher check requires ONEX_TEST_ONEX_WHEEL")
        pytest.skip("Customer launcher check requires ONEX_TEST_ONEX_WHEEL")
    wheel = Path(supplied).resolve()
    assert wheel.is_file()
    uv = os.environ.get("ONEX_TEST_UV") or "uv"

    def install(target: str, name: str) -> tuple[set[str], Path]:
        root = tmp_path / name
        home, tools, bin_dir = root / "home", root / "tools", root / "bin"
        for directory in (home, tools, bin_dir):
            directory.mkdir(parents=True)
        environment = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "UV_TOOL_DIR": str(tools),
            "UV_TOOL_BIN_DIR": str(bin_dir),
        }
        installed = _run(
            [
                uv,
                "tool",
                "install",
                "--python",
                "3.12",
                "--no-build",
                "--find-links",
                str(wheel.parent),
                target,
            ],
            environment,
            root,
            timeout=900,
        )
        assert installed.returncode == 0, installed.stderr
        return {entry.name for entry in bin_dir.iterdir()}, bin_dir

    version = wheel.name.split("-")[1]
    launchers, bin_dir = install(f"omninode-onex=={version}", "customer")
    assert launchers == {"onex", "omni-agent"}, launchers
    ran = _run(
        [str(bin_dir / "onex"), "--version"],
        {
            "HOME": str(tmp_path / "customer" / "home"),
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
        },
        tmp_path,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.startswith("onex version "), ran.stdout
    # --help loads every command's entry point; --version alone does not.
    listed = _run(
        [str(bin_dir / "onex"), "--help"],
        {
            "HOME": str(tmp_path / "customer" / "home"),
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
        },
        tmp_path,
    )
    assert listed.returncode == 0, listed.stdout + listed.stderr

    control, _ = install("omnibase-core", "control")
    assert control > {"onex", "omni-agent"}, control


GUIDE_INSTALL_LINE: tuple[str, ...] = (
    "uv",
    "tool",
    "install",
    "--python",
    "3.12",
    "--no-build",
    "omninode-onex",
)


def _wheel_requiring(links: Path, name: str, requires: str) -> None:
    import base64
    import hashlib
    import zipfile

    dist = name.replace("-", "_")
    metadata = (
        f"Metadata-Version: 2.1\n"
        f"Name: {name}\n"
        f"Version: 1.0\n"
        f"Requires-Dist: {requires}\n"
    )
    wheel = (
        "Wheel-Version: 1.0\n"
        "Generator: test\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )

    def _digest(content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    files = {
        f"{dist}-1.0.dist-info/METADATA": metadata,
        f"{dist}-1.0.dist-info/WHEEL": wheel,
    }
    record_lines = [
        f"{path},{_digest(content)},{len(content.encode('utf-8'))}"
        for path, content in files.items()
    ]
    record = "\n".join(record_lines) + f"\n{dist}-1.0.dist-info/RECORD,,\n"
    files[f"{dist}-1.0.dist-info/RECORD"] = record

    wheel_path = links / f"{dist}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)


def _source_only(links: Path, name: str, version: str, marker: Path) -> None:
    import tarfile

    build_root = links.parent / f"{name}-{version}"
    build_root.mkdir(parents=True, exist_ok=True)
    (build_root / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "stub"\nbackend-path = ["backend"]\n\n'
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    (build_root / "PKG-INFO").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    )
    backend_dir = build_root / "backend"
    backend_dir.mkdir()
    (backend_dir / "stub.py").write_text(
        f"def _invoke(*args: object, **kwargs: object) -> None:\n"
        f"    with open({str(marker)!r}, 'w', encoding='utf-8') as f:\n"
        f"        f.write('invoked')\n"
        f"    raise RuntimeError('build backend stub invoked')\n\n"
        "build_wheel = _invoke\n"
        "build_sdist = _invoke\n"
        "get_requires_for_build_wheel = _invoke\n"
        "prepare_metadata_for_build_wheel = _invoke\n"
        "build_editable = _invoke\n"
    )
    archive = links / f"{name}-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(build_root, arcname=f"{name}-{version}")


def _git_stub(directory: Path, log: Path) -> None:
    import stat

    directory.mkdir(parents=True, exist_ok=True)
    git_path = directory / "git"
    git_path.write_text(f'#!/bin/sh\necho "git $*" >> {str(log)!r}\nexit 97\n')
    git_path.chmod(
        stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )


@pytest.mark.release_proof
def test_no_git_install(tmp_path: Path) -> None:
    """AC1: the guide's install line, run as printed with its --no-build into an empty HOME, never calls git, creates no .git directory, builds nothing and gives an onex that runs; the same line without --no-build builds the source-only fixture and fails."""
    import shutil
    import subprocess

    installer = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "omninode-onex"
        / "installer.json"
    )
    rendered = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_onex_installer_render_compute.runtime_onex_installer_render",
        ],
        input=installer.read_text(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    tool_install_lines = [
        line for line in rendered.stdout.splitlines() if "tool install" in line
    ]
    assert len(tool_install_lines) >= 1
    for flag in GUIDE_INSTALL_LINE:
        if flag.startswith("--"):
            assert any(flag in line for line in tool_install_lines)
    config = json.loads(installer.read_text())
    assert config["python"] == GUIDE_INSTALL_LINE[4], config
    assert config["package"] == GUIDE_INSTALL_LINE[-1], config

    wheel_env = os.environ.get("ONEX_TEST_ONEX_WHEEL")
    if wheel_env is None:
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail("No-git install check requires ONEX_TEST_ONEX_WHEEL")
        pytest.skip("No-git install check requires ONEX_TEST_ONEX_WHEEL")
    wheel = Path(wheel_env)
    assert wheel.is_file()
    version = wheel.name.split("-")[1]
    uv = shutil.which(os.environ.get("ONEX_TEST_UV") or "uv")
    assert uv

    links = tmp_path / "links"
    links.mkdir()
    shutil.copy2(wheel, links / wheel.name)
    marker = tmp_path / "backend-invoked"
    _source_only(links, "certifi", "2099.1.1", marker)

    stub_dir = tmp_path / "stub"
    git_log = tmp_path / "git-called.log"
    _git_stub(stub_dir, git_log)

    def line(no_build: bool) -> list[str]:
        command = list(GUIDE_INSTALL_LINE)
        command[0] = uv
        if not no_build:
            command.remove("--no-build")
        command[-1] = f"omninode-onex=={version}"
        command.extend(["--find-links", str(links)])
        return command

    def environment(home: Path) -> dict[str, str]:
        env: dict[str, str] = {
            "HOME": str(home),
            "PATH": f"{stub_dir}{os.pathsep}{home / '.local' / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "LANG": "en_US.UTF-8",
        }
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmp_dir)
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        ):
            if key in os.environ:
                env[key] = os.environ[key]
        assert not any(k.startswith("UV_") for k in env)
        return env

    home = tmp_path / "home"
    home.mkdir()
    resolved = shutil.which("git", path=environment(home)["PATH"])
    assert resolved == str(stub_dir / "git")
    probe = _run([resolved, "probe"], environment(home), tmp_path)
    assert probe.returncode == 97
    assert git_log.read_text().count("\n") == 1
    git_log.unlink()

    installed = _run(line(True), environment(home), tmp_path, timeout=1200)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert not git_log.exists(), git_log.read_text() if git_log.exists() else ""
    dot_git = sorted(str(p) for p in tmp_path.rglob(".git") if p.is_dir())
    assert dot_git == [], dot_git
    assert not marker.exists(), "the build backend ran under --no-build"

    dist_infos = list(
        (home / ".local" / "share" / "uv" / "tools" / "omninode-onex").glob(
            "lib/python3.12/site-packages/certifi-*.dist-info"
        )
    )
    assert len(dist_infos) == 1
    assert not dist_infos[0].name.startswith("certifi-2099.1.1")

    for args in (["--version"], ["--help"]):
        ran = _run(
            [str(home / ".local" / "bin" / "onex"), *args],
            environment(home),
            tmp_path,
            timeout=120,
        )
        assert ran.returncode == 0, ran.stdout + ran.stderr
        if args == ["--version"]:
            assert version in ran.stdout

    marker_child = tmp_path / "child-backend-invoked"
    _source_only(links, "onex-fixture-child", "1.0", marker_child)
    _wheel_requiring(links, "onex-fixture-parent", "onex-fixture-child")
    naming_home = tmp_path / "naming-home"
    naming_home.mkdir()
    naming = _run(
        [*line(True), "--with", "onex-fixture-parent==1.0"],
        environment(naming_home),
        tmp_path,
        timeout=1200,
    )
    assert naming.returncode != 0
    assert "onex-fixture-child" in naming.stdout + naming.stderr
    assert not marker_child.exists(), "the build backend ran under --no-build"

    control_home = tmp_path / "control-home"
    control_home.mkdir()
    control = _run(line(False), environment(control_home), tmp_path, timeout=1200)
    assert marker.exists(), (
        "the control did not build the source-only fixture, so the check above proves nothing"
    )
    assert control.returncode != 0, control.stdout


CUSTOMER_PACKAGE = "omninode-onex"
CUSTOMER_LAUNCHERS = frozenset({"onex", "omni-agent"})
LAUNCHER_TARGET = "omnibase_core.cli.cli_commands:cli"


def _site_packages(venv: Path) -> Path:
    found = sorted(venv.glob("lib/python*/site-packages"))
    assert len(found) == 1, f"expected one site-packages under {venv}, found {found}"
    return found[0]


def _venv_python(venv: Path) -> str:
    for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in {"version", "version_info"}:
            return value.strip()
    raise AssertionError(f"no version in {venv / 'pyvenv.cfg'}")


def _launcher_owners(site: Path) -> dict[str, list[tuple[str, str]]]:
    """Every installed distribution that declares onex or omni-agent, with its target."""
    from importlib import metadata as importlib_metadata

    owners: dict[str, list[tuple[str, str]]] = {name: [] for name in CUSTOMER_LAUNCHERS}
    from packaging.utils import canonicalize_name

    for dist in importlib_metadata.distributions(path=[str(site)]):
        name = str(canonicalize_name(dist.metadata["Name"]))
        for entry in dist.entry_points:
            if (
                entry.group in {"console_scripts", "gui_scripts"}
                and entry.name in owners
            ):
                owners[entry.name].append((name, entry.value))
        # A launcher a distribution ships as a file (its RECORD lists bin/onex) is a
        # declaration too, unless it is the script its own entry point generated.
        for file in dist.files or []:
            path = Path(str(file))
            declared = {owner for owner, _ in owners.get(path.name, [])}
            if (
                path.parent.name == "bin"
                and path.name in owners
                and name not in declared
            ):
                owners[path.name].append((name, "installed file"))
    return owners


def _assert_one_owner(owners: dict[str, list[tuple[str, str]]]) -> None:
    for name in sorted(CUSTOMER_LAUNCHERS):
        assert owners.get(name) == [(CUSTOMER_PACKAGE, LAUNCHER_TARGET)], (
            f"{name} is declared by {owners.get(name)}; AC3's one-owner rule wants exactly "
            f"{CUSTOMER_PACKAGE} with {LAUNCHER_TARGET}"
        )


def _customer_wheel(wheel: Path) -> tuple[list[str], list[str]]:
    """The customer wheel's RECORD paths and its Requires-Dist lines."""
    import zipfile

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        info = next(n.split("/")[0] for n in names if n.endswith(".dist-info/METADATA"))
        record = archive.read(f"{info}/RECORD").decode("utf-8")
        metadata = archive.read(f"{info}/METADATA").decode("utf-8")
    paths = [line.split(",")[0] for line in record.splitlines() if line.strip()]
    requires = [
        line.split(":", 1)[1].strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:")
    ]
    return paths, requires


def _assert_installed_matches_wheel(site: Path, wheel: Path) -> None:
    """Every hashed file of the customer wheel's RECORD is installed with that sha256."""
    import base64
    import zipfile

    with zipfile.ZipFile(wheel) as archive:
        info = next(
            n.split("/")[0]
            for n in archive.namelist()
            if n.endswith(".dist-info/RECORD")
        )
        record = archive.read(f"{info}/RECORD").decode("utf-8")
    checked = 0
    for line in record.splitlines():
        path, _, rest = line.partition(",")
        algorithm, _, expected = rest.split(",")[0].partition("=")
        if algorithm != "sha256":
            continue
        installed = site / path
        assert installed.is_file(), f"{path} from the wheel is not installed"
        digest = hashlib.sha256(installed.read_bytes()).digest()
        actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert actual == expected, f"installed {path} differs from the wheel's RECORD"
        checked += 1
    assert checked, f"{wheel.name} lists no hashed file"


def _assert_carries_no_code(paths: list[str]) -> None:
    code = [path for path in paths if not path.split("/")[0].endswith(".dist-info")]
    assert code == [], (
        f"{CUSTOMER_PACKAGE} must carry no code of its own; its wheel holds {code}"
    )


def _assert_pins_match_installed(requires: list[str], site: Path) -> None:
    from importlib import metadata as importlib_metadata

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    installed = {
        canonicalize_name(dist.metadata["Name"]): dist.version
        for dist in importlib_metadata.distributions(path=[str(site)])
    }
    assert requires, f"{CUSTOMER_PACKAGE} pins nothing"
    pinned = {canonicalize_name(Requirement(line).name) for line in requires}
    omninode = {
        name
        for name in installed
        if name.startswith(("omnibase-", "omnimarket", "omninode-"))
        and name != CUSTOMER_PACKAGE
    }
    assert omninode <= pinned, (
        f"installed but not pinned by {CUSTOMER_PACKAGE}: {sorted(omninode - pinned)}"
    )
    for line in requires:
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        message = (
            f"{CUSTOMER_PACKAGE} must pin {requirement.name} with ==, not {line!r}"
        )
        assert len(specifiers) == 1, message
        assert specifiers[0].operator == "==", message
        name = canonicalize_name(requirement.name)
        assert installed.get(name) == specifiers[0].version, (
            f"{requirement.name}: pinned {specifiers[0].version}, installed {installed.get(name)}"
        )


def _check_customer_install(venv: Path, bin_dir: Path, wheel: Path, home: Path) -> None:
    """Everything AC3 asks of one installed customer environment."""
    launchers = {entry.name for entry in bin_dir.iterdir()}
    assert launchers == set(CUSTOMER_LAUNCHERS), f"on PATH: {sorted(launchers)}"
    assert _venv_python(venv).startswith("3.12"), _venv_python(venv)
    site = _site_packages(venv)
    _assert_one_owner(_launcher_owners(site))
    paths, requires = _customer_wheel(wheel)
    _assert_carries_no_code(paths)
    _assert_pins_match_installed(requires, site)
    _assert_installed_matches_wheel(site, wheel)
    # Positive control for "builds nothing": certifi is in the closure, so the resolver
    # saw AC1's source-only certifi 2099.1.1 in the links folder and had to pass it over.
    from importlib import metadata as importlib_metadata

    versions = {
        dist.metadata["Name"].lower(): dist.version
        for dist in importlib_metadata.distributions(path=[str(site)])
    }
    assert "certifi" in versions, (
        "certifi is not in the closure, so the build check proved nothing"
    )
    assert versions["certifi"] != "2099.1.1", (
        "the source-only certifi fixture was installed"
    )
    ran = _run(
        [str(bin_dir / "onex"), "--version"],
        {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
        home,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr


def _interpreter(version: str) -> str:
    supplied = os.environ.get(f"ONEX_TEST_PYTHON{version.replace('.', '')}")
    if supplied:
        return supplied
    found = subprocess.run(
        [os.environ.get("ONEX_TEST_UV") or "uv", "python", "find", version],
        capture_output=True,
        text=True,
        check=False,
    )
    assert found.returncode == 0, f"no Python {version}: {found.stderr}"
    return found.stdout.strip()


def _customer_installs(
    tmp_path: Path, wheel: Path, pipx_python_flag: bool = True
) -> dict[str, tuple[Path, Path, Path, Path]]:
    """Install the candidate with uv and with pipx into empty homes whose default Python is 3.11.

    Returns installer -> (venv, bin dir, home, build-backend marker); links holds the customer
    wheel and AC1's source-only fixture, so any build would write the marker.
    """
    py311, py312 = _interpreter("3.11"), _interpreter("3.12")
    version = wheel.name.split("-")[1]
    installs: dict[str, tuple[Path, Path, Path, Path]] = {}
    for installer in ("uv", "pipx"):
        root = tmp_path / installer
        home, bin_dir, links = root / "home", root / "bin", root / "links"
        for directory in (home, bin_dir, links):
            directory.mkdir(parents=True)
        (links / wheel.name).write_bytes(wheel.read_bytes())
        # A candidate not yet on PyPI: its other wheels (for example omnibase-core) are
        # served from the same links folder, still as wheels only.
        extra = os.environ.get("ONEX_TEST_CANDIDATE_WHEELS")
        for candidate in sorted(Path(extra).glob("*.whl")) if extra else []:
            (links / candidate.name).write_bytes(candidate.read_bytes())
        marker = root / "backend-invoked"
        _source_only(links, "certifi", "2099.1.1", marker)
        # Only the two installers and Python 3.12 are added to a minimal PATH; with uv in
        # reach pipx installs through uv, which keeps pipx's default interpreter.
        tools_path = root / "path"
        tools_path.mkdir()
        for tool, source in (
            ("uv", shutil.which(os.environ.get("ONEX_TEST_UV") or "uv")),
            ("pipx", shutil.which(os.environ.get("ONEX_TEST_PIPX") or "pipx")),
            ("python3.12", py312),
        ):
            assert source, f"{tool} is not available"
            (tools_path / tool).symlink_to(source)
        environment = {
            "HOME": str(home),
            "PATH": f"{tools_path}:/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "UV_PYTHON_DOWNLOADS": "never",
        }
        if installer == "uv":
            tools = root / "tools"
            environment |= {
                "UV_TOOL_DIR": str(tools),
                "UV_TOOL_BIN_DIR": str(bin_dir),
                "UV_PYTHON": py311,
            }
            command = ["uv", "tool", "install"]
            command += ["--python", "3.12"]
            command += [
                "--no-build",
                "--find-links",
                str(links),
                f"{CUSTOMER_PACKAGE}=={version}",
            ]
            venv = tools / CUSTOMER_PACKAGE
        else:
            pipx_home = root / "pipx"
            environment |= {
                "PIPX_HOME": str(pipx_home),
                "PIPX_BIN_DIR": str(bin_dir),
                "PIPX_DEFAULT_PYTHON": py311,
                # pipx installs through uv here, as it does by default when uv is on PATH;
                # uv keeps PIPX_DEFAULT_PYTHON, so a line without --python 3.12 fails.
                "PIPX_DEFAULT_BACKEND": "uv",
            }
            command = ["pipx", "install"]
            command += ["--python", "3.12"] if pipx_python_flag else []
            command += [
                f"--pip-args=--only-binary=:all: --find-links {links}",
                f"{CUSTOMER_PACKAGE}=={version}",
            ]
            venv = pipx_home / "venvs" / CUSTOMER_PACKAGE
        installed = _run(command, environment, root, timeout=900)
        assert installed.returncode == 0, (
            f"{installer}: {installed.stdout}{installed.stderr}"
        )
        assert not marker.exists(), f"{installer} built AC1's source-only fixture"
        installs[installer] = (venv, bin_dir, home, marker)
    return installs


@pytest.mark.release_proof
def test_one_package_uv_and_pipx(tmp_path: Path) -> None:
    """AC3: uv and pipx each install only the customer package's launchers, under Python 3.12,
    with each launcher declared once by omninode-onex, exact pins and no code of its own."""
    supplied = os.environ.get("ONEX_TEST_ONEX_WHEEL")
    if not supplied:
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail("AC3 check requires ONEX_TEST_ONEX_WHEEL")
        pytest.skip("AC3 check requires ONEX_TEST_ONEX_WHEEL")
    wheel = Path(supplied).resolve()
    for installer, (venv, bin_dir, home, _) in _customer_installs(
        tmp_path, wheel
    ).items():
        try:
            _check_customer_install(venv, bin_dir, wheel, home)
        except AssertionError as error:
            raise AssertionError(f"{installer}: {error}") from error


@pytest.mark.release_proof
def test_one_package_uv_and_pipx_refuses_pipx_without_python_312(
    tmp_path: Path,
) -> None:
    """AC3 control: a pipx line without --python 3.12 runs on the 3.11 default and fails."""
    supplied = os.environ.get("ONEX_TEST_ONEX_WHEEL")
    if not supplied:
        pytest.skip("AC3 control requires ONEX_TEST_ONEX_WHEEL")
    with pytest.raises(
        AssertionError, match=r"(?s)pipx: .*(3\.12|[Rr]equires.[Pp]ython)"
    ):
        _customer_installs(tmp_path, Path(supplied).resolve(), pipx_python_flag=False)


def _fake_dist(site: Path, name: str, scripts: dict[str, str]) -> None:
    info = site / f"{name.replace('-', '_')}-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
    )
    lines = "".join(f"{key} = {value}\n" for key, value in scripts.items())
    (info / "entry_points.txt").write_text(f"[console_scripts]\n{lines}")


@pytest.mark.parametrize(
    ("declarers", "fails"),
    [
        ({CUSTOMER_PACKAGE: True}, False),
        ({"omnibase-core": True}, True),
        ({CUSTOMER_PACKAGE: True, "omnibase-core": True}, True),
        ({}, True),
    ],
    ids=["customer-only", "core-only", "both", "none"],
)
def test_one_package_uv_and_pipx_one_owner_rule(
    tmp_path: Path, declarers: dict[str, bool], fails: bool
) -> None:
    """AC3 controls: launchers declared only by omnibase_core, by both, or by nobody fail."""
    site = tmp_path / "site-packages"
    site.mkdir()
    for name in declarers:
        _fake_dist(site, name, dict.fromkeys(CUSTOMER_LAUNCHERS, LAUNCHER_TARGET))
    if fails:
        with pytest.raises(AssertionError, match="one-owner rule"):
            _assert_one_owner(_launcher_owners(site))
    else:
        _assert_one_owner(_launcher_owners(site))


def test_one_package_uv_and_pipx_no_code_and_exact_pins(tmp_path: Path) -> None:
    """AC3 controls: a module in the customer wheel, or a pin that is not ==, fails."""
    _assert_carries_no_code(["omninode_onex-1.0.dist-info/METADATA"])
    with pytest.raises(AssertionError, match="no code of its own"):
        _assert_carries_no_code(
            ["omninode_onex-1.0.dist-info/METADATA", "omninode_onex/__init__.py"]
        )
    site = tmp_path / "site-packages"
    site.mkdir()
    _fake_dist(site, "omnibase-core", {})
    _assert_pins_match_installed(["omnibase-core==1.0"], site)
    with pytest.raises(AssertionError, match="with =="):
        _assert_pins_match_installed(["omnibase-core>=1.0"], site)
    with pytest.raises(AssertionError, match=r"installed 1\.0"):
        _assert_pins_match_installed(["omnibase-core==2.0"], site)


def test_one_package_uv_and_pipx_pins_name_every_omninode_distribution(
    tmp_path: Path,
) -> None:
    """AC3 controls: no pins at all, or an installed OmniNode distribution left unpinned, fails."""
    site = tmp_path / "site-packages"
    site.mkdir()
    _fake_dist(site, "omnibase-core", {})
    _fake_dist(site, "omnimarket", {})
    _assert_pins_match_installed(["omnibase-core==1.0", "omnimarket==1.0"], site)
    with pytest.raises(AssertionError, match="pins nothing"):
        _assert_pins_match_installed([], site)
    with pytest.raises(AssertionError, match="not pinned"):
        _assert_pins_match_installed(["omnibase-core==1.0"], site)


def test_one_package_uv_and_pipx_counts_every_kind_of_declaration(
    tmp_path: Path,
) -> None:
    """AC3 controls: a wrong target, a gui_scripts entry, or a launcher file another
    distribution installs (its RECORD lists bin/onex) all break the one-owner rule."""
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    _fake_dist(
        wrong, CUSTOMER_PACKAGE, dict.fromkeys(CUSTOMER_LAUNCHERS, "other.module:cli")
    )
    with pytest.raises(AssertionError, match="one-owner rule"):
        _assert_one_owner(_launcher_owners(wrong))
    gui = tmp_path / "gui"
    gui.mkdir()
    _fake_dist(
        gui, CUSTOMER_PACKAGE, dict.fromkeys(CUSTOMER_LAUNCHERS, LAUNCHER_TARGET)
    )
    info = gui / "omnibase_core-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: omnibase-core\nVersion: 1.0\n"
    )
    (info / "entry_points.txt").write_text(f"[gui_scripts]\nonex = {LAUNCHER_TARGET}\n")
    with pytest.raises(AssertionError, match="one-owner rule"):
        _assert_one_owner(_launcher_owners(gui))
    # importlib only lists RECORD files that exist, as in a real install.
    files = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    files.mkdir(parents=True)
    (tmp_path / "venv" / "bin").mkdir()
    (tmp_path / "venv" / "bin" / "onex").write_text("#!/bin/sh\n")
    _fake_dist(
        files, CUSTOMER_PACKAGE, dict.fromkeys(CUSTOMER_LAUNCHERS, LAUNCHER_TARGET)
    )
    other = files / "omnibase_core-1.0.dist-info"
    other.mkdir()
    (other / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: omnibase-core\nVersion: 1.0\n"
    )
    (other / "RECORD").write_text("../../../bin/onex,,\n")
    with pytest.raises(AssertionError, match="one-owner rule"):
        _assert_one_owner(_launcher_owners(files))


def test_one_package_uv_and_pipx_ties_the_install_to_the_wheel(tmp_path: Path) -> None:
    """AC3 control: an installed customer file whose sha256 differs from the wheel's RECORD fails."""
    import base64
    import zipfile

    content = "Metadata-Version: 2.1\nName: omninode-onex\nVersion: 1.0\n"
    digest = base64.urlsafe_b64encode(hashlib.sha256(content.encode()).digest()).rstrip(
        b"="
    )
    wheel = tmp_path / "omninode_onex-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("omninode_onex-1.0.dist-info/METADATA", content)
        archive.writestr(
            "omninode_onex-1.0.dist-info/RECORD",
            f"omninode_onex-1.0.dist-info/METADATA,sha256={digest.decode()},{len(content)}\n"
            "omninode_onex-1.0.dist-info/RECORD,,\n",
        )
    site = tmp_path / "site-packages"
    (site / "omninode_onex-1.0.dist-info").mkdir(parents=True)
    installed = site / "omninode_onex-1.0.dist-info" / "METADATA"
    installed.write_text(content)
    _assert_installed_matches_wheel(site, wheel)
    installed.write_text(content + "Summary: changed\n")
    with pytest.raises(AssertionError, match="differs from the wheel"):
        _assert_installed_matches_wheel(site, wheel)


_CI_INSTALLER = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "omninode-onex"
    / "installer.json"
)
_CI_TOOLS = (
    "bash",
    "curl",
    "tar",
    "mktemp",
    "awk",
    "sed",
    "grep",
    "uname",
    "basename",
    "cat",
    "cp",
    "mv",
    "mkdir",
    "rm",
    "chmod",
    "dirname",
    "env",
    "gzip",
    "sha256sum",
    "shasum",
    "perl",
    "ldd",
    "sysctl",
    "ln",
    "ls",
    "head",
    "tr",
    "readlink",
    "id",
)


def _ci_config() -> dict[str, Any]:
    """Load the installer manifest from the repository."""
    config: dict[str, Any] = json.loads(_CI_INSTALLER.read_text(encoding="utf-8"))
    return config


def _ci_host_target() -> str:
    """Map the host platform to the uv release target triple."""
    import platform

    system = platform.system()
    machine = platform.machine()
    if system == "Linux" and machine == "x86_64":
        return "x86_64-unknown-linux-gnu"
    if system == "Linux" and machine in {"aarch64", "arm64"}:
        return "aarch64-unknown-linux-gnu"
    if system == "Darwin" and machine == "arm64":
        return "aarch64-apple-darwin"
    pytest.skip(f"curl installer journey does not run on {system} {machine}")


def _ci_uv_tarball(target: str, cache: Path) -> Path:
    """Return the pinned uv tarball, downloading it once if needed."""
    env_dir = os.environ.get("ONEX_TEST_UV_TARBALL_DIR")
    if env_dir:
        path = Path(env_dir) / f"uv-{target}.tar.gz"
        assert path.is_file(), f"missing uv tarball: {path}"
        return path
    version = _ci_config()["uv_version"]
    url = (
        f"https://github.com/astral-sh/uv/releases/download/"
        f"{version}/uv-{target}.tar.gz"
    )
    response = httpx.get(url, follow_redirects=True, timeout=120)
    response.raise_for_status()
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"uv-{target}.tar.gz"
    path.write_bytes(response.content)
    return path


def _ci_render(version: str) -> str:
    """Render the installer script for the given release version."""
    from omnimarket.nodes.node_onex_installer_render_compute.handlers.handler_onex_installer_render import (
        HandlerOnexInstallerRender,
    )
    from omnimarket.nodes.node_onex_installer_render_compute.models.model_onex_installer import (
        ModelOnexInstallerRequest,
    )

    config = _ci_config()
    config["version"] = version
    request = ModelOnexInstallerRequest.model_validate(config)
    return HandlerOnexInstallerRender().handle(request).script


def _ci_serve(root: Path) -> tuple[ThreadingHTTPServer, str]:
    """Serve root over loopback HTTP on an ephemeral port."""

    import functools
    import http.server

    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            """Suppress per-request logging."""

    handler = functools.partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _ci_toolbox(directory: Path, uname_stub: str | None = None) -> Path:
    """Populate directory with symlinks to the required host tools."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in _CI_TOOLS:
        if name == "uname" and uname_stub is not None:
            continue
        found = shutil.which(name)
        if found is not None:
            os.symlink(found, directory / name)
    if uname_stub is not None:
        stub = directory / "uname"
        stub.write_text(f"#!/bin/sh\n{uname_stub}\n", encoding="utf-8")
        stub.chmod(0o755)
    return directory


def _ci_fixture(
    tmp_path: Path,
    script: str,
    tarball: Path,
    target: str,
    uname_stub: str | None = None,
) -> tuple[dict[str, str], Path, Path, ThreadingHTTPServer, str]:
    """Build the isolated HOME, site, stub and tools for the installer."""
    home = tmp_path / "home"
    home.mkdir()
    site = tmp_path / "site"
    site.mkdir()
    (site / "install.sh").write_text(script, encoding="utf-8")
    version = _ci_config()["uv_version"]
    uv_dir = site / "uv" / version
    uv_dir.mkdir(parents=True)
    shutil.copy2(tarball, uv_dir / f"uv-{target}.tar.gz")
    stub = tmp_path / "stub"
    stub.mkdir()
    git_log = tmp_path / "git-called.log"
    _git_stub(stub, git_log)
    toolbox = _ci_toolbox(tmp_path / "tools", uname_stub)
    server, base = _ci_serve(site)
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    env: dict[str, str] = {
        "HOME": str(home),
        "PATH": f"{stub}:{toolbox}",
        "LANG": "C.UTF-8",
        "ONEX_INSTALL_UV_BASE_URL": f"{base}/uv",
        "TMPDIR": str(tmp_dir),
    }
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    return env, home, git_log, server, base


def _ci_curl_bash(
    env: dict[str, str],
    base: str,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    """Run the installer script through curl piped into bash."""
    return subprocess.run(
        ["/bin/bash", "-c", f"curl -fsSL {base}/install.sh | bash"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


@pytest.mark.release_proof
def test_curl_installer_same_release(tmp_path: Path) -> None:
    """AC4: the candidate's script, fetched over HTTP from a local stand-in for our domain and piped into bash in an empty HOME with no uv on PATH and a git stub, installs the pinned uv after its sha256 matches, runs the guide's install line with --no-build, never calls git and leaves exactly onex and omni-agent in uv's bin directory. Until omninode-onex is published, the candidate wheel comes from ONEX_TEST_ONEX_WHEEL through the installer's find-links variable while every other distribution comes from PyPI. Not covered here: installed content checked against the release manifest, which exists only at release."""
    wheel_env = os.environ.get("ONEX_TEST_ONEX_WHEEL")
    if wheel_env is None:
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail("curl installer journey requires ONEX_TEST_ONEX_WHEEL")
        pytest.skip("curl installer journey requires ONEX_TEST_ONEX_WHEEL")
    wheel = Path(wheel_env)
    assert wheel.is_file()
    version = wheel.name.split("-")[1]
    target = _ci_host_target()
    cache = tmp_path / "cache"
    cache.mkdir()
    tarball = _ci_uv_tarball(target, cache)
    assert (
        hashlib.sha256(tarball.read_bytes()).hexdigest()
        == (_ci_config()["uv_sha256"][target])
    )
    links = tmp_path / "links"
    links.mkdir()
    shutil.copy2(wheel, links / wheel.name)
    script = _ci_render(version)
    env, home, git_log, server, base = _ci_fixture(
        tmp_path,
        script,
        tarball,
        target,
    )
    env["ONEX_INSTALL_FIND_LINKS"] = str(links)
    try:
        result = _ci_curl_bash(env, base)
    finally:
        server.shutdown()
    assert result.returncode == 0, (
        f"installer failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert not git_log.exists(), (
        f"git was called: {git_log.read_text(encoding='utf-8')!r}"
    )
    assert [p for p in tmp_path.rglob(".git") if p.is_dir()] == []
    private = home / ".local" / "share" / "omninode" / "uv" / "uv"
    assert private.is_file()
    assert "checksum verified" in result.stderr
    uv_version_out = subprocess.run(
        [str(private), "--version"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert _ci_config()["uv_version"] in uv_version_out
    bin_dir = home / ".local" / "bin"
    assert {p.name for p in bin_dir.iterdir()} == set(CUSTOMER_LAUNCHERS)
    onex = subprocess.run(
        [str(bin_dir / "onex"), "--version"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert onex.returncode == 0
    assert version in onex.stdout + onex.stderr


@pytest.mark.release_proof
def test_curl_installer_same_release_refuses_a_changed_uv(tmp_path: Path) -> None:
    """A changed uv tarball must be refused and leave no partial install."""
    target = _ci_host_target()
    tarball = _ci_uv_tarball(target, tmp_path / "cache")
    changed = tmp_path / f"changed-uv-{target}.tar.gz"
    changed.write_bytes(tarball.read_bytes() + b"0")
    script = _ci_render(_ci_config()["version"])
    env, home, git_log, server, base = _ci_fixture(tmp_path, script, changed, target)
    try:
        result = _ci_curl_bash(env, base, timeout=300)
    finally:
        server.shutdown()
    assert result.returncode != 0
    assert "does not match its published sha256" in result.stderr
    assert not (home / ".local" / "share" / "omninode" / "uv" / "uv").exists()
    assert not (home / ".local" / "bin" / "onex").exists()
    assert not git_log.exists()


@pytest.mark.release_proof
def test_curl_installer_same_release_leaves_a_foreign_onex_alone(
    tmp_path: Path,
) -> None:
    """A foreign onex launcher must be left untouched on a refused install."""
    wheel_env = os.environ.get("ONEX_TEST_ONEX_WHEEL")
    if wheel_env is None:
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail("foreign-launcher journey requires ONEX_TEST_ONEX_WHEEL")
        pytest.skip("foreign-launcher journey requires ONEX_TEST_ONEX_WHEEL")
    wheel = Path(wheel_env)
    version = wheel.name.split("-")[1]
    links = tmp_path / "links"
    links.mkdir()
    shutil.copy2(wheel, links / wheel.name)
    target = _ci_host_target()
    tarball = _ci_uv_tarball(target, tmp_path / "cache")
    env, home, git_log, server, base = _ci_fixture(
        tmp_path, _ci_render(version), tarball, target
    )
    env["ONEX_INSTALL_FIND_LINKS"] = str(links)
    foreign = home / ".local" / "bin" / "onex"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign_text = "#!/bin/sh\n# installed by pipx for another package\necho foreign\n"
    foreign.write_text(foreign_text)
    os.chmod(foreign, 0o755)
    before = foreign.read_bytes()
    try:
        result = _ci_curl_bash(env, base)
    finally:
        server.shutdown()
    assert result.returncode != 0
    assert str(foreign) in result.stderr
    assert "pipx uninstall" in result.stderr
    assert foreign.read_bytes() == before
    private = home / ".local" / "share" / "omninode" / "uv" / "uv"
    assert private.is_file()
    line = subprocess.run(
        [
            str(private),
            "tool",
            "install",
            "--python",
            "3.12",
            "--no-build",
            f"omninode-onex=={version}",
            "--find-links",
            str(links),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert line.returncode != 0
    assert foreign.read_bytes() == before
    assert not git_log.exists()


@pytest.mark.release_proof
def test_curl_installer_same_release_refuses_windows_naming_wsl(tmp_path: Path) -> None:
    """A Windows uname stub must be refused as unsupported WSL."""
    target = _ci_host_target()
    tarball = _ci_uv_tarball(target, tmp_path / "cache")
    uname_stub = 'case "$1" in -m) echo x86_64 ;; *) echo MINGW64_NT-10.0-22631 ;; esac'
    env, home, git_log, server, base = _ci_fixture(
        tmp_path,
        _ci_render(_ci_config()["version"]),
        tarball,
        target,
        uname_stub=uname_stub,
    )
    try:
        result = _ci_curl_bash(env, base, timeout=120)
    finally:
        server.shutdown()
    assert result.returncode != 0
    assert "WSL" in result.stderr
    assert "not supported yet" in result.stderr
    assert not (home / ".local" / "share" / "omninode" / "uv").exists()
    assert not git_log.exists()
