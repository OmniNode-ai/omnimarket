# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise an unknown package's actual commands and bounded lifecycle children.

Failures caught: unavailable dynamic command, missing dependency enable, disabling
a dependency still in use, duplicate callback effects, discarded user edits,
callback failure/timeout recorded as success, and corrupt configuration ignored.
The final artifact captures every CLI result and the preserved customer settings.
"""

import json
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def test_unknown_plugin_lifecycle_uses_owned_callbacks(tmp_path: Path) -> None:
    package = tmp_path / "packages"
    package.mkdir()
    metadata = package / "customer_plugin-1.0.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: customer-plugin\nVersion: 1.0.0\n"
    )
    (metadata / "entry_points.txt").write_text(
        "[onex.setup]\nbase = customer_plugin:BASE\nfeature = customer_plugin:FEATURE\n"
    )
    (package / "customer_plugin.py").write_text("""
import json
import time
from pathlib import Path
import click
from pydantic import BaseModel
BASE = {"id":"customer-base","api_version":1,"version":"1.0.0","enabled_by_default":False,"components":["customer-base"],"steps":[{"name":"customer-base","factory":"customer_plugin:Effect"}]}
FEATURE = {"id":"customer-feature","api_version":1,"version":"1.0.0","enabled_by_default":False,"recommended":True,"requires":["customer-base"],"components":["customer-feature"],"commands":{"hello":"customer_plugin:hello"},"lifecycle":{"enable":"customer_plugin:enable","disable":"customer_plugin:disable","timeout_seconds":1},"steps":[{"name":"customer-feature","factory":"customer_plugin:Effect"}]}
@click.command()
def hello(): click.echo("customer command")
def enable(onex_home):
    home = Path(onex_home)
    if (home / "fail").exists(): raise ValueError("private-error-SENTINEL")
    if (home / "slow").exists(): time.sleep(10)
    p = home / "customer.json"
    data = json.loads(p.read_text()) if p.exists() else {}
    data["owned"] = "plugin-value"
    p.write_text(json.dumps(data))
    count = home / "enable-count"
    count.write_text(str(int(count.read_text()) + 1) if count.exists() else "1")
def disable(onex_home):
    p = Path(onex_home) / "customer.json"
    if p.exists():
        data = json.loads(p.read_text())
        if data.get("owned") == "plugin-value": data.pop("owned")
        p.write_text(json.dumps(data))
class Effect: pass
class Settings(BaseModel):
    threshold: int = 1
FEATURE["configuration_schema"] = "customer_plugin:Settings"
""")
    home = tmp_path / "home"
    home.mkdir()
    config_home = home / ".onex"
    config_home.mkdir()
    config = config_home / "config.yaml"
    config.write_text("version: 1\nuser_added: keep-me\n")
    environment = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(package),
    }
    evidence: list[dict[str, object]] = []

    def invoke(
        *arguments: str, success: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_onex_setup_effect.cli_plugins",
                *arguments,
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        evidence.append(
            {
                "arguments": arguments,
                "exit": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        assert (result.returncode == 0) is success, result.stdout + result.stderr
        assert "private-error-SENTINEL" not in result.stdout + result.stderr
        return result

    listed = json.loads(invoke("list", "--json").stdout)
    assert any(row["id"] == "customer-feature" and not row["enabled"] for row in listed)
    invoke("run", "customer-feature", "hello", success=False)
    invoke("enable", "customer-feature", "--json", success=False)
    invoke("enable", "customer-base", "--json")
    invoke("enable", "customer-feature", "--json")
    assert (
        invoke("run", "customer-feature", "hello").stdout.strip() == "customer command"
    )
    before = (config.read_bytes(), config.stat().st_mtime_ns)
    invoke("enable", "customer-feature", "--json")
    assert before == (config.read_bytes(), config.stat().st_mtime_ns)
    assert (config_home / "enable-count").read_text() == "1"
    invoke("disable", "customer-base", success=False)
    owned = config_home / "customer.json"
    owned.write_text(json.dumps({"owned": "later-user-edit", "unrelated": "keep"}))
    invoke("disable", "customer-feature", "--json")
    assert json.loads(owned.read_text()) == {
        "owned": "later-user-edit",
        "unrelated": "keep",
    }
    invoke("run", "customer-feature", "hello", success=False)
    for failure in ("fail", "slow"):
        flag = config_home / failure
        flag.touch()
        before = config.read_bytes()
        invoke("enable", "customer-feature", "--json", success=False)
        assert config.read_bytes() == before
        flag.unlink()
    assert "keep-me" in config.read_text()
    config.write_text(
        "setup_plugins: {configuration: {customer-feature: {threshold: 3}}}\n"
    )
    invoke("list", "--json")
    config.write_text(
        "setup_plugins: {configuration: {customer-feature: {threshold: private-error-SENTINEL}}}\n"
    )
    invoke("list", "--json", success=False)
    config.write_text("setup_plugins: {enabled: bad}\n")
    invoke("list", "--json", success=False)
    (tmp_path / "plugin-lifecycle-evidence.json").write_text(
        json.dumps(evidence, indent=2)
    )


def test_plugin_package_owner_is_verified_after_real_install(tmp_path: Path) -> None:
    """A successful pip install without an ONEX manifest must never say validated.

    Exercise real wheel install/removal in an isolated venv, retaining the CLI's
    installed dependencies through PYTHONPATH; no package touches the caller env.
    """
    environment_path = tmp_path / "env"
    created = subprocess.run(
        [sys.executable, "-m", "venv", str(environment_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    python = environment_path / "bin" / "python"
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).resolve().parents[2] / "src"
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(source) + ":" + sysconfig.get_paths()["purelib"],
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }

    def wheel(name: str, manifest: bool, version: str = "1.0.0") -> Path:
        path = tmp_path / f"{name}-{version}-py3-none-any.whl"
        info = f"{name}-{version}.dist-info"
        files = {
            f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{name}.py": f'PLUGIN = {{"id":"wheel-fixture","api_version":1,"version":"{version}","components":["wheel-fixture"],"lifecycle":{{"disable":"{name}:disable"}},"steps":[{{"name":"wheel-fixture","factory":"{name}:Effect"}}]}}\nclass Effect: pass\n'
            + """import json
from pathlib import Path
def disable(onex_home):
    if (onex_home / "refuse-remove").exists(): raise ValueError("disposal refused")
    path = onex_home / "customer.json"
    if not path.exists(): return
    data = json.loads(path.read_text())
    if data.get("owned") == "plugin-original":
        data.pop("owned")
        path.write_text(json.dumps(data))
""",
        }
        if manifest:
            files[f"{info}/entry_points.txt"] = (
                f"[onex.setup]\nfixture = {name}:PLUGIN\n"
            )
        files[f"{info}/RECORD"] = (
            "".join(f"{entry},,\n" for entry in files) + f"{info}/RECORD,,\n"
        )
        with zipfile.ZipFile(path, "w") as archive:
            for entry, content in files.items():
                archive.writestr(entry, content)
        return path

    def invoke(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(python),
                "-m",
                "omnimarket.nodes.node_onex_setup_effect.cli_plugins",
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    valid = invoke("install", str(wheel("customer_fixture", True)), "--allow-unsigned")
    assert valid.returncode == 0, valid.stdout + valid.stderr
    listed = invoke("list", "--json")
    assert any(row["id"] == "wheel-fixture" for row in json.loads(listed.stdout))
    configuration = home / ".onex"
    configuration.mkdir(exist_ok=True)
    config = configuration / "config.yaml"
    config.write_text(
        "customer_owned: keep\nsetup_plugins: {enabled: {wheel-fixture: false}}\n"
    )
    customer = configuration / "customer.json"
    customer.write_text('{"owned":"later-user-edit","unrelated":"keep"}\n')
    before = (config.read_bytes(), customer.read_bytes(), customer.stat().st_mtime_ns)
    updated = invoke(
        "update", str(wheel("customer_fixture", True, "2.0.0")), "--allow-unsigned"
    )
    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert before == (
        config.read_bytes(),
        customer.read_bytes(),
        customer.stat().st_mtime_ns,
    )
    upgraded = next(
        row
        for row in json.loads(invoke("list", "--json").stdout)
        if row["id"] == "wheel-fixture"
    )
    assert upgraded["version"] == "2.0.0"
    assert upgraded["enabled"] is False
    assert invoke("enable", "wheel-fixture", "--json").returncode == 0
    refusal = configuration / "refuse-remove"
    refusal.touch()
    before_refusal = config.read_bytes()
    failed_removal = invoke("uninstall", "wheel-fixture", "--yes")
    assert failed_removal.returncode != 0
    assert config.read_bytes() == before_refusal
    assert any(
        row["id"] == "wheel-fixture"
        for row in json.loads(invoke("list", "--json").stdout)
    )
    refusal.unlink()
    invalid = invoke("install", str(wheel("plain_package", False)), "--allow-unsigned")
    assert invalid.returncode != 0, "A package with no capability manifest was accepted"
    removed = invoke("uninstall", "wheel-fixture", "--yes")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert all(
        row["id"] != "wheel-fixture"
        for row in json.loads(invoke("list", "--json").stdout)
    )
    assert customer.read_bytes() == before[1]
    assert customer.stat().st_mtime_ns == before[2]
    assert "customer_owned: keep" in config.read_text()
    (tmp_path / "plugin-package-evidence.json").write_text(
        json.dumps(
            {
                "install": valid.returncode,
                "missing_manifest": invalid.returncode,
                "uninstall": removed.returncode,
                "update": updated.returncode,
                "refused_disposal_kept_package": failed_removal.returncode != 0,
                "later_user_state_preserved": True,
            }
        )
    )
