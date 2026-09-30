# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the public installation hint in an empty customer home."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

PROMPT = "say hello in one word"
MODEL = "canary-stub-model"


class CanaryError(Exception):
    """A named canary check failed."""


class Stage(TypedDict):
    stage: str
    status: str
    detail: str


def extract_install_hint(marketplace: dict[str, object]) -> str:
    plugins = marketplace.get("plugins")
    if isinstance(plugins, list):
        for plugin in plugins:
            if not isinstance(plugin, dict) or plugin.get("name") != "onex":
                continue
            requires = plugin.get("requires")
            cli = requires.get("onex_cli") if isinstance(requires, dict) else None
            hint = cli.get("install_hint") if isinstance(cli, dict) else None
            if isinstance(hint, str) and hint.strip():
                return hint
    raise CanaryError(
        "install_hint: missing plugins[name=onex].requires.onex_cli.install_hint"
    )


def plant_bad_pin(hint: str) -> str:
    planted, count = re.subn(
        r"(?<![\w.-])omnibase-core>=\d+\.\d+\.\d+(?![\w.+-])",
        "omnibase-core>=999.0.0",
        hint,
    )
    if count == 0 or planted == hint:
        raise CanaryError("plant_bad_pin: no replaceable omnibase-core version floor")
    return planted


def build_env(home: Path, passthrough: Sequence[str] = ()) -> dict[str, str]:
    # An allowlist also excludes future provider credentials and uv/XDG overrides.
    # `passthrough` names the few build variables a documented install path
    # needs (Intel macOS builds from source with OPENSSL_DIR); each must be set.
    env = {
        "HOME": str(home),
        "PATH": str(home / ".local/bin")
        + os.pathsep
        + os.environ.get("PATH", os.defpath),
    }
    for name in passthrough:
        value = os.environ.get(name)
        if not value:
            raise CanaryError(f"pass-env: {name} is not set in the environment")
        env[name] = value
    return env


def write_bifrost_overrides(home: Path, port: int) -> None:
    path = home / ".omninode/delegation/bifrost_overrides.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "backends:\n"
        + "".join(
            f"  - backend_id: {backend}\n"
            f'    endpoint_url: "http://127.0.0.1:{port}/v1/chat/completions"\n'
            f'    model_name: "{MODEL}"\n'
            for backend in ("local-coder", "local-heavy-reasoning")
        ),
        encoding="utf-8",
    )


def assert_run_artifacts(cwd: Path) -> None:
    """Validate the delegate run files under cwd."""
    runs = [path for path in (cwd / ".onex_state/runs").glob("*") if path.is_dir()]
    if len(runs) != 1:
        raise CanaryError(
            f"run count: expected exactly one directory, found {len(runs)}"
        )
    run = runs[0]
    for name in ("result.txt", "receipt.json", "run.json"):
        if not (run / name).is_file():
            raise CanaryError(f"{name}: missing artifact")
    try:
        result = (run / "result.txt").read_text(encoding="utf-8")
        receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
        request = json.loads((run / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CanaryError(f"artifact read/JSON check: {exc}") from exc
    if "canary-ok" not in result:
        raise CanaryError("result.txt: missing canary-ok")
    if not isinstance(receipt, dict) or receipt.get("status") != "success":
        raise CanaryError("receipt.json status: expected success")
    if receipt.get("model") != MODEL:
        raise CanaryError("receipt.json model: expected canary-stub-model")
    try:
        port = int((cwd.parent / "stub-port").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CanaryError(
            f"stub port: missing or invalid port evidence: {exc}"
        ) from exc
    if not 1 <= port <= 65535:
        raise CanaryError("stub port: outside TCP port range")
    if receipt.get("endpoint") != f"http://127.0.0.1:{port}/v1/chat/completions":
        raise CanaryError("receipt.json endpoint: does not match stub port and route")
    if not isinstance(request, dict) or request.get("prompt") != PROMPT:
        raise CanaryError("run.json prompt: missing expected prompt text")


def run_command(
    command: list[str], env: dict[str, str], cwd: Path, timeout: int
) -> str:
    try:
        result = subprocess.run(
            command,
            env=env,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CanaryError(f"command timed out after {timeout}s: {command[0]}") from exc
    output = result.stdout + result.stderr
    if result.returncode:
        raise CanaryError(f"command exited {result.returncode}:\n{output}")
    return output


def wait_for_stub(stub: subprocess.Popen[bytes], port_file: Path) -> int:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if stub.poll() is not None:
            raise CanaryError(
                f"stub_start: process exited {stub.returncode}; see stub.log"
            )
        if port_file.is_file():
            value = port_file.read_text(encoding="utf-8").strip()
            if value:
                port = int(value)
                if not 1 <= port <= 65535:
                    raise CanaryError("stub_start: invalid port")
                return port
        time.sleep(0.05)
    raise CanaryError("stub_start: port file was not written within 15s; see stub.log")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marketplace-json", required=True, type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--plant-bad-pin", action="store_true")
    parser.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="NAME",
        help="copy this variable into the install environment (repeatable)",
    )
    parser.add_argument("--summary-json", required=True, type=Path)
    args = parser.parse_args()
    stages: list[Stage] = []
    summary: dict[str, object] = {
        "hint": "",
        "plant_bad_pin": args.plant_bad_pin,
        "platform": f"{platform.system()} {platform.machine()}",
        "stages": stages,
        "failed_stage": None,
    }
    stage = "install"
    stub: subprocess.Popen[bytes] | None = None

    def passed(detail: str) -> None:
        stages.append({"stage": stage, "status": "passed", "detail": detail})

    try:
        work = (
            args.work_dir or Path(tempfile.mkdtemp(prefix="install-canary-"))
        ).resolve()
        work.mkdir(parents=True, exist_ok=True)
        home = Path(tempfile.mkdtemp(prefix="home-", dir=work))
        env = build_env(home, args.pass_env)
        marketplace = json.loads(args.marketplace_json.read_text(encoding="utf-8"))
        if not isinstance(marketplace, dict):
            raise CanaryError("marketplace: expected a JSON object")
        hint = extract_install_hint(marketplace)
        if args.plant_bad_pin:
            hint = plant_bad_pin(hint)
        summary.update(hint=hint, work_dir=str(work), home=str(home))
        if any(home.iterdir()):
            raise CanaryError("HOME: must be empty before install")
        output = run_command(["bash", "-c", hint], env, work, 900)
        if not (home / ".local/bin/onex").is_file():
            raise CanaryError("install: onex missing from fresh HOME/.local/bin")
        passed(output)

        stage = "version"
        passed(run_command(["onex", "--version"], env, work, 60))
        stage = "local_init"
        output = run_command(["onex", "local", "init"], env, work, 60)
        if "tenant_id" not in output:
            raise CanaryError("local_init: output missing tenant_id")
        passed(output)

        stage = "stub_start"
        port_file = work / "stub-port"
        port_file.unlink(missing_ok=True)
        with (work / "stub.log").open("wb") as log:
            stub = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("stub_openai_server.py")),
                    "--port",
                    "0",
                    "--port-file",
                    str(port_file),
                ],
                env=env,
                cwd=work,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        port = wait_for_stub(stub, port_file)
        passed(f"port={port}; request counts in {work / 'stub.log'}")
        stage = "overrides"
        write_bifrost_overrides(home, port)
        passed("configured local-coder and local-heavy-reasoning")

        stage = "delegate"
        run_dir = work / "run"
        run_dir.mkdir()  # Refuse reused run artifacts.
        passed(
            run_command(
                ["onex", "delegate", "--task-type", "research", PROMPT],
                env,
                run_dir,
                300,
            )
        )
        stage = "artifacts"
        assert_run_artifacts(run_dir)
        passed("result, receipt and run checks passed")
    except (CanaryError, OSError, ValueError) as exc:
        stages.append({"stage": stage, "status": "failed", "detail": str(exc)})
        summary["failed_stage"] = stage
    finally:
        if stub is not None:
            stub.terminate()
            try:
                stub.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stub.kill()
                stub.wait()
        rendered = json.dumps(summary, indent=2) + "\n"
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
    return 1 if summary["failed_stage"] is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
