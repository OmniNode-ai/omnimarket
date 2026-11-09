# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stub wheels, real installers and a manifest built from the stub wheels (OMN-23343).

The wheels are hand-built (no network); the installs are real: uv tool, pipx (or the pip venv pipx
wraps when pipx is absent) and an ordinary venv. The manifest is derived from the wheels exactly as
the train derives it from the published ones: wheel sha256, RECORD, entry_points.txt.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from omnimarket.nodes.node_release_candidate_verify_effect.handlers.handler_release_candidate_verify import (
    load_default_overlay,
)
from omnimarket.nodes.node_release_candidate_verify_effect.handlers.verifier_pypi_ancestry import (
    parse_wheel_record,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelManifestDashboardBundle,
    ModelManifestDistribution,
    ModelManifestPlugin,
    ModelManifestPluginFile,
    ModelManifestProtectedScript,
    ModelManifestRequiredChange,
    ModelReleaseCandidateManifest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_overlay import (
    ModelLauncherTemplate,
    ModelReleaseCandidateOverlay,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_request import (
    ModelReleaseCandidateVerifyRequest,
)

ONEX_TARGET = "omnibase_core.cli.cli_commands:cli"
PLUGIN_FILES = {
    ".claude-plugin/plugin.json": '{"name": "onex-delegate", "version": "0.3.3"}\n',
    "commands/delegate.md": "delegate\n",
    "hooks/hooks.json": "{}\n",
}


def build_wheel(
    out: Path,
    name: str,
    version: str,
    files: dict[str, str],
    entry_points: str = "",
    requires: tuple[str, ...] = (),
) -> Path:
    dist_info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {req}\n" for req in requires)
    members = dict(files)
    members[f"{dist_info}/METADATA"] = metadata
    members[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: stub\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    if entry_points:
        members[f"{dist_info}/entry_points.txt"] = entry_points
    record = io.StringIO()
    path = out / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for member, text in members.items():
            data = text.encode()
            archive.writestr(member, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(
                b"="
            )
            record.write(f"{member},sha256={digest.decode()},{len(data)}\n")
        record.write(f"{dist_info}/RECORD,,\n")
        archive.writestr(f"{dist_info}/RECORD", record.getvalue())
    return path


def build_candidate_wheels(out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    return {
        "omnibase_core": build_wheel(
            out,
            "omnibase_core",
            "1.0.0",
            {
                "omnibase_core/__init__.py": "",
                "omnibase_core/cli/__init__.py": "",
                "omnibase_core/cli/cli_commands.py": "def cli():\n    return 0\n\n\ndef other():\n    return 1\n",
            },
            f"[console_scripts]\nonex = {ONEX_TARGET}\n\n[onex.cli]\nsetup = omnibase_core.cli.cli_commands:cli\n"
            "models = omnibase_core.cli.cli_commands:cli\ndelegate = omnibase_core.cli.cli_commands:cli\n"
            "dashboard = omnibase_core.cli.cli_commands:cli\n",
        ),
        "omnibase_infra": build_wheel(
            out,
            "omnibase_infra",
            "1.0.0",
            {
                "omnibase_infra/__init__.py": "",
                "omnibase_infra/agent.py": "def main():\n    return 0\n",
            },
            "[console_scripts]\nomni-agent = omnibase_infra.agent:main\n",
        ),
        "omnimarket": build_wheel(
            out,
            "omnimarket",
            "1.0.0",
            {"omnimarket/__init__.py": "", "omnimarket/nodes/__init__.py": ""},
            "[onex.nodes]\nnode_a = omnimarket.nodes\n",
            requires=("uvicorn", "pytest"),
        ),
        "uvicorn": build_wheel(
            out,
            "uvicorn",
            "0.1.0",
            {"uvicorn/__init__.py": ""},
            "[console_scripts]\nuvicorn = uvicorn:main\n",
        ),
        "pytest": build_wheel(
            out,
            "pytest",
            "0.1.0",
            {"pytest/__init__.py": ""},
            "[console_scripts]\npytest = pytest:main\npy.test = pytest:main\n",
        ),
    }


def _entry_points(wheel: Path) -> dict[str, dict[str, str]]:
    with zipfile.ZipFile(wheel) as archive:
        name = next(
            (n for n in archive.namelist() if n.endswith("entry_points.txt")), None
        )
        if name is None:
            return {}
        text = archive.read(name).decode()
    groups: dict[str, dict[str, str]] = {}
    group = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            groups[group] = {}
        elif line and group:
            key, _, value = line.partition("=")
            groups[group][key.strip()] = value.strip()
    return groups


def distribution_of(wheel: Path, name: str, version: str) -> ModelManifestDistribution:
    with zipfile.ZipFile(wheel) as archive:
        record_name = next(
            n for n in archive.namelist() if n.endswith(".dist-info/RECORD")
        )
        record = parse_wheel_record(archive.read(record_name).decode())
    return ModelManifestDistribution(
        name=name,
        version=version,
        wheel_filename=wheel.name,
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        record=record,
        entry_points=_entry_points(wheel),
    )


def write_plugin(root: Path) -> list[ModelManifestPluginFile]:
    listed = []
    for relative, text in PLUGIN_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        listed.append(
            ModelManifestPluginFile(
                path=relative, sha256=hashlib.sha256(text.encode()).hexdigest()
            )
        )
    return listed


def candidate_manifest(
    wheels: dict[str, Path], plugin_files: list[ModelManifestPluginFile]
) -> ModelReleaseCandidateManifest:
    return ModelReleaseCandidateManifest(
        manifest_id="launch-candidate-test",
        distributions=(
            distribution_of(wheels["omnibase_core"], "omnibase-core", "1.0.0"),
            distribution_of(wheels["omnibase_infra"], "omnibase-infra", "1.0.0"),
            distribution_of(wheels["omnimarket"], "omnimarket", "1.0.0"),
        ),
        protected_scripts=(
            ModelManifestProtectedScript(
                name="onex", owner="omnibase-core", target=ONEX_TARGET
            ),
            ModelManifestProtectedScript(
                name="omni-agent",
                owner="omnibase-infra",
                target="omnibase_infra.agent:main",
            ),
        ),
        plugin=ModelManifestPlugin(
            version="0.3.3", omniclaude_commit="abcdef0123", files=tuple(plugin_files)
        ),
        dashboard_bundle=ModelManifestDashboardBundle(
            version="v1.1.11", sha256="a" * 64
        ),
        required_changes=(
            ModelManifestRequiredChange(
                repo="omnimarket", ticket="OMN-0001", sha="1111111"
            ),
        ),
    )


def _run(args: list[str], env: dict[str, str] | None = None) -> None:
    # UV_LINK_MODE=copy: uv hardlinks installed files from its cache by default, so the mutation
    # cases would edit the cached wheel itself and corrupt every later install of that wheel.
    merged = {
        **os.environ,
        "UV_NO_CONFIG": "1",
        "UV_LINK_MODE": "copy",
        **(env or {}),
    }
    # Not the worktree: uv would read its pyproject and resolve the stub names against it.
    done = subprocess.run(
        args, capture_output=True, text=True, env=merged, cwd=tempfile.gettempdir()
    )
    if done.returncode:
        raise RuntimeError(f"{args} exited {done.returncode}: {done.stderr}")


def _specs() -> list[str]:
    return ["omnibase-core==1.0.0", "omnibase-infra==1.0.0", "omnimarket==1.0.0"]


def install_uv_tool(base: Path, wheel_dir: Path) -> tuple[str, str]:
    """Returns (environment prefix, command -v onex) for a uv tool environment."""
    tools, bin_dir = base / "uv-tools", base / "uv-bin"
    env = {"UV_TOOL_DIR": str(tools), "UV_TOOL_BIN_DIR": str(bin_dir)}
    args = [
        "uv",
        "tool",
        "install",
        "--offline",
        "--no-index",
        "--find-links",
        str(wheel_dir),
    ]
    args += [
        "--python",
        sys.executable,
        "--python-preference",
        "only-system",
        _specs()[0],
    ]
    for spec in _specs()[1:]:
        args += ["--with", spec]
    _run(args, env)
    return str(tools / "omnibase-core"), str(bin_dir / "onex")


def pip_template_from_stub_install(
    base: Path, wheel_dir: Path
) -> ModelLauncherTemplate:
    """The launcher template of whatever pip this interpreter bundles, generated from a stub install."""
    stub = base / "pip-template-stub"
    _run([sys.executable, "-m", "venv", str(stub)])
    _run(
        [
            str(stub / "bin" / "pip"),
            "install",
            "-q",
            "--no-index",
            "--no-deps",
            "--find-links",
            str(wheel_dir),
            "omnibase-core==1.0.0",
        ]
    )
    text = (stub / "bin" / "onex").read_text()
    text = text.replace(f"{stub}/bin/python3", "{interpreter}").replace(
        f"{stub}/bin/python", "{interpreter}"
    )
    text = text.replace(
        "from omnibase_core.cli.cli_commands import cli",
        "from {target_module} import {target_attr}",
    )
    text = text.replace("sys.exit(cli())", "sys.exit({target_attr}())")
    pip_version = subprocess.run(
        [str(stub / "bin" / "pip"), "--version"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[1]
    return ModelLauncherTemplate(
        name="pip-bundled",
        installer="pip",
        installer_version=pip_version,
        interpreter_names=("python", "python3"),
        text=text,
    )


def overlay_with_bundled_pip(
    base: Path, wheel_dir: Path
) -> ModelReleaseCandidateOverlay:
    default = load_default_overlay()
    return default.model_copy(
        update={
            "launcher_templates": (
                *default.launcher_templates,
                pip_template_from_stub_install(base, wheel_dir),
            )
        }
    )


def install_pipx(base: Path, wheel_dir: Path) -> tuple[str, str]:
    """A pipx environment reached through its bin symlink; pipx itself when present, else the pip venv it wraps."""
    pipx_bin = base / "pipx-bin"
    pip_args = f"--no-index --find-links {wheel_dir}"
    if shutil.which("pipx"):
        env = {"PIPX_HOME": str(base / "pipx-home"), "PIPX_BIN_DIR": str(pipx_bin)}
        _run(
            [
                "pipx",
                "install",
                "--python",
                sys.executable,
                "--pip-args",
                pip_args,
                "--include-deps",
                _specs()[0],
            ],
            env,
        )
        _run(
            ["pipx", "inject", "--pip-args", pip_args, "omnibase-core", *_specs()[1:]],
            env,
        )
        return str(base / "pipx-home" / "venvs" / "omnibase-core"), str(
            pipx_bin / "onex"
        )
    prefix = base / "pipx-home" / "venvs" / "omnibase-core"
    _run([sys.executable, "-m", "venv", str(prefix)])
    _run(
        [
            str(prefix / "bin" / "pip"),
            "install",
            "-q",
            "--no-index",
            "--find-links",
            str(wheel_dir),
            *_specs(),
        ]
    )
    pipx_bin.mkdir(parents=True)
    (pipx_bin / "onex").symlink_to(prefix / "bin" / "onex")
    return str(prefix), str(pipx_bin / "onex")


def install_venv(
    base: Path, wheel_dir: Path, name: str = "venv", with_pip: bool = True
) -> tuple[str, str]:
    """An ordinary uv venv, with pip added when asked, whose bin is on PATH."""
    prefix = base / name
    if with_pip:
        _run([sys.executable, "-m", "venv", str(prefix)])
    else:
        _run(["uv", "venv", "-q", "--python", sys.executable, str(prefix)])
    _run(
        [
            "uv",
            "pip",
            "install",
            "-q",
            "--offline",
            "--no-index",
            "--find-links",
            str(wheel_dir),
            "--python",
            str(prefix / "bin" / "python"),
            *_specs(),
        ]
    )
    return str(prefix), str(prefix / "bin" / "onex")


def install_extra(prefix: str, wheel: Path) -> None:
    _run(
        [
            "uv",
            "pip",
            "install",
            "-q",
            "--offline",
            "--no-index",
            "--no-deps",
            "--python",
            str(Path(prefix) / "bin" / "python"),
            str(wheel),
        ]
    )


def request(
    manifest: ModelReleaseCandidateManifest,
    prefix: str,
    onex_on_path: str,
    overlay: ModelReleaseCandidateOverlay | None = None,
) -> ModelReleaseCandidateVerifyRequest:
    return ModelReleaseCandidateVerifyRequest(
        operation="verify_installed",
        manifest=manifest,
        environment_prefix=prefix,
        onex_on_path=onex_on_path,
        overlay=overlay,
    )
