# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The customer installer, rendered by node_onex_installer_render_compute (OMN-25070, AC4).

The script is the node's output for packages/omninode-onex/installer.json, or the
file named by ONEX_TEST_INSTALL_SCRIPT when a workflow rendered it already.

Every case pipes the script into bash, as `curl -fsSL .../install.sh | bash`
does, so nothing may depend on the script having a file path.

Failure modes each test catches: a syntax construct the macOS Bash 3.2 cannot
parse; the script pinning a package version the package does not have, or a
uv target without a checksum; a platform mapped to the wrong uv build, or an
unsupported one accepted; a missing tool reported late or not at all; a uv
download that is run although its sha256 differs; a launcher another tool owns
being overwritten; and (opt-in, real network) a clean install, reinstall or
upgrade that leaves anything but onex on PATH.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "packages" / "omninode-onex"
INSTALLER_REQUEST = PACKAGE_DIR / "installer.json"


def _render() -> str:
    supplied = os.environ.get("ONEX_TEST_INSTALL_SCRIPT")
    if supplied:
        return Path(supplied).read_text(encoding="utf-8")
    from omnimarket.nodes.node_onex_installer_render_compute.handlers.handler_onex_installer_render import (
        HandlerOnexInstallerRender,
    )
    from omnimarket.nodes.node_onex_installer_render_compute.models.model_onex_installer import (
        ModelOnexInstallerRequest,
    )

    request = ModelOnexInstallerRequest.model_validate_json(
        INSTALLER_REQUEST.read_text(encoding="utf-8")
    )
    return HandlerOnexInstallerRender().handle(request).script


SCRIPT_TEXT = _render()
TARGETS = (
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
)
BASE_TOOLS = (
    "bash", "curl", "tar", "mktemp", "awk", "sed", "grep", "uname", "basename",
    "cat", "cp", "mv", "mkdir", "rm", "chmod", "dirname", "env", "gzip",
    "sha256sum", "shasum", "perl", "ldd", "sysctl",
)  # fmt: skip


def _toolbox(directory: Path, exclude: tuple[str, ...] = ()) -> Path:
    """A PATH directory holding only the named real tools (never uv)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in BASE_TOOLS:
        if name in exclude:
            continue
        found = shutil.which(name)
        if found:
            (directory / name).symlink_to(found)
    return directory


def _stub(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def _run(
    env: dict[str, str], script: str | None = None, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash"],
        input=script if script is not None else SCRIPT_TEXT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _script_value(name: str) -> str:
    match = re.search(rf'^{name}="([^"]*)"$', SCRIPT_TEXT, re.MULTILINE)
    assert match, name
    return match.group(1)


@pytest.mark.unit
def test_script_parses_under_the_system_bash() -> None:
    assert SCRIPT_TEXT.startswith("#!/usr/bin/env bash\n")
    parsed = subprocess.run(
        ["/bin/bash", "-n"],
        input=SCRIPT_TEXT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stderr


@pytest.mark.unit
def test_shellcheck_is_clean() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    checked = subprocess.run(
        [shellcheck, "-s", "bash", "-"],
        input=SCRIPT_TEXT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout


@pytest.mark.unit
def test_pins_match_the_package_and_every_target_has_a_checksum() -> None:
    with (PACKAGE_DIR / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    assert _script_value("ONEX_VERSION") == version
    assert _script_value("ONEX_PACKAGE") == "omninode-onex"
    text = SCRIPT_TEXT
    for target in TARGETS:
        match = re.search(rf'{re.escape(target)}\) echo "([0-9a-f]*)"', text)
        assert match, target
        assert len(match.group(1)) == 64, target


@pytest.mark.unit
def test_the_rendered_installer_expects_only_onex() -> None:
    """The customer install exposes only onex (OMN-25070 ruling 2026-11-25).

    The launcher list the script verifies, and every word it prints, name onex
    alone: a script that still expects or mentions omni-agent fails here.
    """
    assert _script_value("ONEX_LAUNCHERS") == "onex"
    assert "omni-agent" not in SCRIPT_TEXT


@pytest.mark.unit
@pytest.mark.parametrize(
    ("system", "machine", "translated", "expected"),
    [
        ("Darwin", "arm64", "0", "target=aarch64-apple-darwin"),
        ("Darwin", "x86_64", "1", "target=aarch64-apple-darwin"),
        ("Darwin", "x86_64", "0", "Intel Macs are not supported yet"),
        ("Linux", "x86_64", "0", "target=x86_64-unknown-linux-gnu"),
        ("Linux", "aarch64", "0", "target=aarch64-unknown-linux-gnu"),
        ("Linux", "arm64", "0", "target=aarch64-unknown-linux-gnu"),
        ("Linux", "riscv64", "0", "unsupported Linux CPU 'riscv64'"),
        ("FreeBSD", "amd64", "0", "unsupported operating system 'FreeBSD'"),
        ("MINGW64_NT-10.0", "x86_64", "0", "Windows is not supported yet"),
    ],
)
def test_platform_detection(
    tmp_path: Path, system: str, machine: str, translated: str, expected: str
) -> None:
    stubs = tmp_path / "stubs"
    _stub(stubs, "uname", f'case "$1" in -s) echo {system};; -m) echo {machine};; esac')
    _stub(stubs, "sysctl", f"echo {translated}")
    _stub(stubs, "ldd", 'echo "ldd (GNU libc) 2.39"')
    tools = _toolbox(tmp_path / "tools", exclude=("uname", "sysctl", "ldd"))
    result = _run(
        {
            "HOME": str(tmp_path),
            "PATH": f"{stubs}:{tools}",
            "ONEX_INSTALL_DRY_RUN": "1",
        }
    )
    combined = result.stdout + result.stderr
    assert expected in combined, combined
    assert (result.returncode == 0) == expected.startswith("target="), combined


@pytest.mark.unit
def test_musl_linux_is_refused(tmp_path: Path) -> None:
    stubs = tmp_path / "stubs"
    _stub(stubs, "uname", 'case "$1" in -s) echo Linux;; -m) echo x86_64;; esac')
    # Real musl `ldd --version` prints to stderr and exits 1.
    _stub(stubs, "ldd", 'echo "musl libc (x86_64)" >&2; exit 1')
    tools = _toolbox(tmp_path / "tools", exclude=("uname", "ldd"))
    result = _run(
        {"HOME": str(tmp_path), "PATH": f"{stubs}:{tools}", "ONEX_INSTALL_DRY_RUN": "1"}
    )
    assert result.returncode == 1
    assert "musl" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("build_tools", [False, True])
def test_intel_mac_is_refused_even_with_build_tools(
    tmp_path: Path, build_tools: bool
) -> None:
    """Intel Macs are not supported yet, so the installer never compiles anything."""
    stubs = tmp_path / "stubs"
    _stub(stubs, "uname", 'case "$1" in -s) echo Darwin;; -m) echo x86_64;; esac')
    _stub(stubs, "sysctl", "echo 0")
    if build_tools:
        _stub(stubs, "brew", f'echo "{tmp_path}/openssl@3"')
        _stub(stubs, "cargo", "exit 0")
    tools = _toolbox(tmp_path / "tools", exclude=("uname", "sysctl"))
    result = _run(
        {"HOME": str(tmp_path), "PATH": f"{stubs}:{tools}", "ONEX_INSTALL_DRY_RUN": "1"}
    )
    assert result.returncode == 1, result.stdout
    assert "Intel Macs are not supported yet" in result.stderr
    assert "target=" not in result.stdout


@pytest.mark.unit
def test_dry_run_installs_without_building(tmp_path: Path) -> None:
    stubs = tmp_path / "stubs"
    _stub(stubs, "uname", 'case "$1" in -s) echo Darwin;; -m) echo arm64;; esac')
    tools = _toolbox(tmp_path / "tools", exclude=("uname",))
    result = _run(
        {"HOME": str(tmp_path), "PATH": f"{stubs}:{tools}", "ONEX_INSTALL_DRY_RUN": "1"}
    )
    assert result.returncode == 0, result.stderr
    assert "build=--no-build" in result.stdout


@pytest.mark.unit
def test_help_works_when_piped(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/bash", "-s", "--", "--help"],
        input=SCRIPT_TEXT,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "curl -fsSL https://omninode.ai/install.sh | bash" in result.stdout
    assert f"omninode-onex {_script_value('ONEX_VERSION')}" in result.stdout


@pytest.mark.unit
def test_missing_curl_is_named(tmp_path: Path) -> None:
    tools = _toolbox(tmp_path / "tools", exclude=("curl",))
    result = _run(
        {"HOME": str(tmp_path), "PATH": str(tools), "ONEX_INSTALL_DRY_RUN": "1"}
    )
    assert result.returncode == 1
    assert "needs 'curl'" in result.stderr


def _native_target() -> str:
    system, machine = os.uname().sysname, os.uname().machine
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    return f"{arch}-apple-darwin" if system == "Darwin" else f"{arch}-unknown-linux-gnu"


@pytest.mark.unit
def test_uv_with_a_wrong_checksum_is_never_installed(tmp_path: Path) -> None:
    target = _native_target()
    if target == "x86_64-apple-darwin":
        pytest.skip(
            "Intel Macs are not supported yet; the installer refuses before this step"
        )
    release = tmp_path / "release" / _script_value("UV_VERSION")
    payload = tmp_path / "payload" / f"uv-{target}"
    payload.mkdir(parents=True)
    fake_uv = payload / "uv"
    fake_uv.write_text('#!/bin/sh\necho ran >> "$HOME/fake-uv-ran"\n')
    fake_uv.chmod(0o755)
    release.mkdir(parents=True)
    with tarfile.open(release / f"uv-{target}.tar.gz", "w:gz") as archive:
        archive.add(payload, arcname=f"uv-{target}")
    home = tmp_path / "home"
    home.mkdir()
    tools = _toolbox(tmp_path / "tools")
    result = _run(
        {
            "HOME": str(home),
            "PATH": str(tools),
            "ONEX_INSTALL_UV_BASE_URL": (tmp_path / "release").as_uri(),
        }
    )
    assert result.returncode == 1, result.stderr
    assert "does not match its published sha256" in result.stderr
    assert not (home / ".local" / "share" / "omninode" / "uv" / "uv").exists()
    assert not (home / "fake-uv-ran").exists()


@pytest.mark.unit
def test_a_launcher_another_tool_owns_is_left_alone(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    if _native_target() == "x86_64-apple-darwin":
        pytest.skip(
            "Intel Macs are not supported yet; the installer refuses before this step"
        )
    tools = _toolbox(tmp_path / "tools")
    (tools / "uv").symlink_to(uv)
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    foreign = bin_dir / "onex"
    foreign.write_text("#!/bin/sh\necho foreign onex\n")
    foreign.chmod(0o755)
    before = foreign.read_bytes()
    result = _run(
        {
            "HOME": str(home),
            "PATH": str(tools),
            "UV_TOOL_DIR": str(tmp_path / "tool-envs"),
            "UV_TOOL_BIN_DIR": str(bin_dir),
        }
    )
    assert result.returncode == 1, result.stderr
    assert str(foreign) in result.stderr
    assert "was not installed by uv" in result.stderr
    assert foreign.read_bytes() == before
    assert not (bin_dir / "omni-agent").exists()


@pytest.mark.e2e
@pytest.mark.release_proof
def test_install_reinstall_and_upgrade_for_real(tmp_path: Path) -> None:
    """Real network: pinned uv from GitHub, packages from PyPI.

    Opt in with ONEX_TEST_ONEX_WHEEL, the built omninode-onex wheel at the
    script's pinned version; until that version is on PyPI the installer finds
    it through ONEX_INSTALL_FIND_LINKS. No uv is on PATH, so the installer
    downloads and verifies its own.
    """
    supplied = os.environ.get("ONEX_TEST_ONEX_WHEEL")
    if not supplied:
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail("Installer e2e requires ONEX_TEST_ONEX_WHEEL")
        pytest.skip("Installer e2e requires ONEX_TEST_ONEX_WHEEL")
    wheel = Path(supplied).resolve()
    home = tmp_path / "home"
    home.mkdir()
    # A uv that cannot report a version comes first on the caller's PATH, so the
    # installer must fetch and verify its own.
    shadow = tmp_path / "shadow"
    _stub(shadow, "uv", "exit 127")
    env = {
        "HOME": str(home),
        "PATH": f"{shadow}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "SHELL": "/bin/bash",
        "ONEX_INSTALL_FIND_LINKS": str(wheel.parent),
    }
    bin_dir = home / ".local" / "bin"

    first = _run(env, timeout=1800)
    assert first.returncode == 0, first.stderr
    assert "checksum verified" in first.stderr
    assert {p.name for p in bin_dir.iterdir()} == {"onex"}
    assert "is not on your PATH yet" in first.stderr

    again = _run(env, timeout=1800)
    assert again.returncode == 0, again.stderr
    assert {p.name for p in bin_dir.iterdir()} == {"onex"}

    uv_bin = shutil.which("uv")
    if uv_bin is None:
        pytest.skip("building the upgrade wheel needs uv on PATH")
    current = _script_value("ONEX_VERSION")
    # Bump the trailing number: 0.0.2 -> 0.0.3, 0.0.1.post1 -> 0.0.1.post2.
    trailing = re.search(r"(\d+)$", current)
    assert trailing, current
    newer = current[: trailing.start()] + str(int(trailing.group(1)) + 1)
    package = tmp_path / "newer-package"
    package.mkdir()
    (package / "pyproject.toml").write_text(
        (PACKAGE_DIR / "pyproject.toml")
        .read_text()
        .replace(f'version = "{current}"', f'version = "{newer}"')
    )
    wheels = tmp_path / "wheels"
    built = subprocess.run(
        [uv_bin, "build", "--wheel", "--out-dir", str(wheels), str(package)],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert built.returncode == 0, built.stderr
    # Dependency wheels built beside the customer wheel (an unreleased omnimarket)
    # are needed for the upgrade as well.
    for sibling in wheel.parent.glob("*.whl"):
        if not sibling.name.startswith("omninode_onex-"):
            shutil.copy2(sibling, wheels / sibling.name)
    newer_script = SCRIPT_TEXT.replace(
        f'ONEX_VERSION="{current}"', f'ONEX_VERSION="{newer}"'
    )
    upgraded = _run({**env, "ONEX_INSTALL_FIND_LINKS": str(wheels)}, newer_script, 1800)
    assert upgraded.returncode == 0, upgraded.stderr
    assert f"omninode-onex=={newer}" in upgraded.stderr
    assert {p.name for p in bin_dir.iterdir()} == {"onex"}


@pytest.mark.unit
def test_the_last_line_sends_the_customer_to_the_tui() -> None:
    """A customer lands in the TUI: bare `onex` in a terminal opens it, and /config sets up a model."""
    last = [
        line
        for line in SCRIPT_TEXT.splitlines()
        if line.strip().startswith('say "Done.')
    ]
    assert len(last) == 1, last
    assert "Run 'onex' to open ONEX" in last[0]
    assert "/config" in last[0]
