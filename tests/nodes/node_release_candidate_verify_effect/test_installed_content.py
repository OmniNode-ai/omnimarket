# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installed-content check over real installs of the stub candidate wheels (OMN-23343).

Untouched installs pass in every admitted layout; every mutation case then reads
installed_matches_manifest=false naming the file, entry point or launcher line.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_release_candidate_verify_effect.handlers.handler_release_candidate_verify import (
    HandlerReleaseCandidateVerify,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.enum_release_candidate_finding import (
    EnumReleaseCandidateFinding as K,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelReleaseCandidateManifest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelReleaseCandidateVerifyResult,
)
from tests.nodes.node_release_candidate_verify_effect import support

pytestmark = pytest.mark.unit


def _verify(
    manifest: ModelReleaseCandidateManifest, prefix: str, onex_on_path: str
) -> ModelReleaseCandidateVerifyResult:
    return HandlerReleaseCandidateVerify().handle(
        support.request(manifest, prefix, onex_on_path)
    )


def _kinds(result: ModelReleaseCandidateVerifyResult) -> dict[K, str]:
    return {finding.kind: finding.subject for finding in result.findings}


def _closure(result: ModelReleaseCandidateVerifyResult) -> set[str]:
    return {dist.name for dist in result.installed_distributions}


def test_untouched_uv_tool_passes(
    manifest: ModelReleaseCandidateManifest, wheel_dir: Path, tmp_path: Path
) -> None:
    prefix, onex = support.install_uv_tool(tmp_path, wheel_dir)
    result = _verify(manifest, prefix, onex)
    assert result.installed_matches_manifest is True, result.findings
    assert result.layout == "uv_tool"
    assert {"uvicorn", "pytest"} <= _closure(result)


def _has_pip(python: Path) -> bool:
    return (
        subprocess.run(
            [str(python), "-m", "pip", "--version"], capture_output=True
        ).returncode
        == 0
    )


def test_untouched_pipx_passes(
    manifest: ModelReleaseCandidateManifest, wheel_dir: Path, tmp_path: Path
) -> None:
    prefix, onex = support.install_pipx(tmp_path, wheel_dir)
    # The pip that writes the launcher (pipx's shared pip, else the bundled one) may write one no
    # checked-in template covers; its template is then generated from a stub install, as the overlay's were.
    # Newer pipx writes a launcher whose interpreter line carries -E, so its template is generated
    # through pipx itself.
    candidates = [
        tmp_path / "pipx-home" / "shared" / "bin" / "python",
        Path(prefix) / "bin" / "python",
    ]
    overlay = support.overlay_with_bundled_pip(
        tmp_path,
        wheel_dir,
        (
            None,
            *(
                c
                for c in candidates
                if shutil.which("pipx") and c.exists() and _has_pip(c)
            ),
        ),
        extra_templates=(
            (
                support.pipx_template_from_stub_install(
                    tmp_path / "pipx-template", wheel_dir
                ),
            )
            if shutil.which("pipx")
            else ()
        ),
    )
    result = HandlerReleaseCandidateVerify().handle(
        support.request(manifest, prefix, onex, overlay)
    )
    assert result.installed_matches_manifest is True, result.findings
    assert {"uvicorn", "pytest"} <= _closure(result)


def test_untouched_venv_with_pip_passes(
    manifest: ModelReleaseCandidateManifest, wheel_dir: Path, tmp_path: Path
) -> None:
    prefix, onex = support.install_venv(tmp_path, wheel_dir, with_pip=True)
    result = _verify(manifest, prefix, onex)
    assert result.installed_matches_manifest is True, result.findings
    assert result.layout == "venv"
    assert {"uvicorn", "pytest", "pip"} <= _closure(result)


@pytest.fixture
def untouched(
    manifest: ModelReleaseCandidateManifest, wheel_dir: Path, tmp_path: Path
) -> tuple[str, str]:
    """A fresh venv install that must read true before any mutation is applied to it."""
    prefix, onex = support.install_venv(tmp_path, wheel_dir, with_pip=False)
    baseline = _verify(manifest, prefix, onex)
    assert baseline.installed_matches_manifest is True, baseline.findings
    return prefix, onex


def _site(prefix: str) -> Path:
    return next(Path(prefix).glob("lib/python*/site-packages"))


def _extra_wheel(wheel_dir: Path, name: str, entry_points: str) -> Path:
    return support.build_wheel(
        wheel_dir, name, "9.9.9", {f"{name}/__init__.py": ""}, entry_points
    )


def test_content_module_edit_mutation(
    manifest: ModelReleaseCandidateManifest, untouched: tuple[str, str]
) -> None:
    prefix, onex = untouched
    module = _site(prefix) / "omnibase_core/cli/cli_commands.py"
    module.write_text(module.read_text() + "#")
    result = _verify(manifest, prefix, onex)
    assert result.installed_matches_manifest is False
    assert _kinds(result)[K.FILE_MODIFIED] == "omnibase_core/cli/cli_commands.py"


def test_content_entry_points_edit_mutation(
    manifest: ModelReleaseCandidateManifest, untouched: tuple[str, str]
) -> None:
    prefix, onex = untouched
    entry_points = _site(prefix) / "omnibase_core-1.0.0.dist-info/entry_points.txt"
    entry_points.write_text(
        entry_points.read_text().replace(
            "setup = omnibase_core.cli.cli_commands:cli", "setup = evil.module:cli"
        )
    )
    result = _verify(manifest, prefix, onex)
    assert result.installed_matches_manifest is False
    kinds = _kinds(result)
    assert kinds[K.FILE_MODIFIED] == "omnibase_core-1.0.0.dist-info/entry_points.txt"
    assert kinds[K.ENTRY_POINT_REDIRECTED] == "onex.cli:setup"


def test_content_extra_entry_point_mutation(
    manifest: ModelReleaseCandidateManifest, untouched: tuple[str, str], wheel_dir: Path
) -> None:
    prefix, onex = untouched
    support.install_extra(
        prefix,
        _extra_wheel(wheel_dir, "extrapkg", "[onex.cli]\nsneaky = extrapkg:main\n"),
    )
    result = _verify(manifest, prefix, onex)
    assert result.installed_matches_manifest is False
    assert _kinds(result)[K.ENTRY_POINT_EXTRA] == "onex.cli:sneaky"


def test_content_protected_name_second_owner_mutation(
    manifest: ModelReleaseCandidateManifest, untouched: tuple[str, str], wheel_dir: Path
) -> None:
    prefix, onex = untouched
    support.install_extra(
        prefix,
        _extra_wheel(
            wheel_dir, "squatter", "[console_scripts]\nonex = squatter:main\n"
        ),
    )
    result = _verify(manifest, prefix, onex)
    assert result.installed_matches_manifest is False
    owner_finding = next(
        f for f in result.findings if f.kind is K.PROTECTED_SCRIPT_OWNER
    )
    assert owner_finding.subject == "onex"
    assert "squatter" in owner_finding.detail


def _rewrite_launcher(onex: str, edit: Callable[[str], str]) -> None:
    path = Path(onex)
    path.write_text(edit(path.read_text()))


def test_content_launcher_retarget_mutation(
    manifest: ModelReleaseCandidateManifest, untouched: tuple[str, str]
) -> None:
    prefix, onex = untouched
    _rewrite_launcher(
        onex,
        lambda t: t.replace(
            "from omnibase_core.cli.cli_commands import cli",
            "from evil.module import cli",
        ),
    )
    result = _verify(manifest, prefix, onex)
    assert result.installed_matches_manifest is False
    finding = next(f for f in result.findings if f.kind is K.LAUNCHER_DIFFERS)
    # the line is 4 in the short form and 6 in the long-path form a deep tmp path makes uv write
    assert finding.subject.endswith(("line 4", "line 6"))
    assert "evil.module" in finding.detail


def test_content_launcher_early_exit_mutation(
    manifest: ModelReleaseCandidateManifest, untouched: tuple[str, str]
) -> None:
    prefix, onex = untouched
    # keeps the interpreter line and the import; only an early exit is inserted before the call
    _rewrite_launcher(
        onex,
        lambda t: t.replace(
            "    sys.exit(cli())", "    sys.exit(0)\n    sys.exit(cli())"
        ),
    )
    result = _verify(manifest, prefix, onex)
    assert result.installed_matches_manifest is False
    finding = next(f for f in result.findings if f.kind is K.LAUNCHER_DIFFERS)
    assert "sys.exit(0)" in finding.detail or "sys.exit(cli())" in finding.detail


def test_content_launcher_call_replaced_mutation(
    manifest: ModelReleaseCandidateManifest, untouched: tuple[str, str]
) -> None:
    prefix, onex = untouched
    _rewrite_launcher(onex, lambda t: t.replace("sys.exit(cli())", "sys.exit(other())"))
    result = _verify(manifest, prefix, onex)
    assert result.installed_matches_manifest is False
    finding = next(f for f in result.findings if f.kind is K.LAUNCHER_DIFFERS)
    assert "other()" in finding.detail


def test_content_launcher_outside_environment_mutation(
    manifest: ModelReleaseCandidateManifest,
    untouched: tuple[str, str],
    wheel_dir: Path,
    tmp_path: Path,
) -> None:
    prefix, _ = untouched
    (tmp_path / "elsewhere").mkdir()
    _, foreign_onex = support.install_venv(
        tmp_path / "elsewhere", wheel_dir, name="other", with_pip=False
    )
    result = _verify(manifest, prefix, foreign_onex)
    assert result.installed_matches_manifest is False
    assert _kinds(result)[K.LAUNCHER_OUTSIDE_ENVIRONMENT] == "onex"


def test_content_launcher_missing_from_path_mutation(
    manifest: ModelReleaseCandidateManifest, untouched: tuple[str, str]
) -> None:
    prefix, _ = untouched
    result = HandlerReleaseCandidateVerify().handle(
        support.request(manifest, prefix, "").model_copy(update={"onex_on_path": None})
    )
    assert result.installed_matches_manifest is False
    assert K.LAUNCHER_OUTSIDE_ENVIRONMENT in _kinds(result)
