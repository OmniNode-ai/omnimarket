# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_claude_code_binding_install_effect (OMN-18531, T0.3 AC6, AC7, AC12)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from omnimarket.models.claude_code_binding import (
    EnumClientMaxMode,
    ModelBindingRenderRequest,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.binding_install_error import (
    BindingInstallError,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.handlers import (
    handler_binding_install,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.handler_binding_install import (
    HandlerClaudeCodeBindingInstall,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.models import (
    ModelBindingInstallRequest,
)
from omnimarket.nodes.node_claude_code_binding_render_compute.handlers.handler_binding_render import (
    HandlerClaudeCodeBindingRender,
)

from .conftest import make_binding

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
_GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


@pytest.fixture(autouse=True)
def _work_tree_probe_ends_at_tmp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Judge only the directories inside tmp_path.

    A shared /tmp may hold a stray .git entry; the probe walks every parent of the
    target, so without this the verdict would depend on the host's /tmp.
    """

    def inside_tmp_path_work_tree(path: Path) -> bool:
        return any(
            (p / ".git").exists()
            for p in (path, *path.parents)
            if p == tmp_path or tmp_path in p.parents
        )

    monkeypatch.setattr(
        handler_binding_install, "_inside_a_work_tree", inside_tmp_path_work_tree
    )


def scrub_git_location_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop the git location variables before shelling out to git (OMN-14891)."""
    scrubbed = {k: v for k, v in env.items() if k not in _GIT_LOCATION_ENV}
    scrubbed["GIT_CONFIG_NOSYSTEM"] = "1"
    return scrubbed


def _install(tmp_path: Path, *bindings, supported=("P0", "P1", "P16"), **kw):
    request = ModelBindingInstallRequest(
        bindings=tuple(bindings),
        max_mode=kw.pop("max_mode", EnumClientMaxMode.REQUIRED),
        mods_dir=str(tmp_path / "mods"),
        settings_path=str(tmp_path / ".claude" / "settings.json"),
        supported_probe_rows=tuple(supported),
        **kw,
    )
    return HandlerClaudeCodeBindingInstall().handle(request), request


def test_ac6_installed_sha256_equals_the_renders(tmp_path, replacing_binding) -> None:
    result, _request = _install(tmp_path, replacing_binding)
    fresh = HandlerClaudeCodeBindingRender().handle(
        ModelBindingRenderRequest(
            bindings=(replacing_binding,), max_mode=EnumClientMaxMode.REQUIRED
        )
    )
    assert result.module_path is not None
    on_disk = Path(result.module_path).read_bytes()
    assert (
        hashlib.sha256(on_disk).hexdigest()
        == result.installed_sha256
        == fresh.module_sha256
    )
    assert on_disk.decode() == fresh.module_text
    record = json.loads(
        (Path(result.module_path).parents[1] / "install_record.json").read_text()
    )
    assert record["render_sha256"] == fresh.module_sha256


def test_ac6_a_mod_folder_is_written_with_its_manifest(
    tmp_path, replacing_binding
) -> None:
    result, _ = _install(tmp_path, replacing_binding)
    root = Path(result.module_path).parents[1]
    assert json.loads((root / "hooks" / "hooks.json").read_text()) == {
        "modules": ["./register.ts"]
    }
    assert json.loads((root / ".claude-plugin" / "plugin.json").read_text())["name"]


def test_ac7_install_refuses_a_target_inside_a_work_tree(
    tmp_path, replacing_binding
) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    request = ModelBindingInstallRequest(
        bindings=(replacing_binding,),
        max_mode=EnumClientMaxMode.REQUIRED,
        mods_dir=str(tmp_path / "repo" / "mods"),
        settings_path=str(tmp_path / ".claude" / "settings.json"),
        supported_probe_rows=("P0", "P1"),
    )
    with pytest.raises(BindingInstallError, match="work tree"):
        HandlerClaudeCodeBindingInstall().handle(request)


def test_ac7_no_rendered_module_is_tracked_in_any_repository() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.splitlines()
    rendered = [
        f
        for f in tracked
        if f.endswith((".ts", ".tsx", ".js"))
        and (REPO / f).is_file()
        and "Rendered by node_claude_code_binding_render_compute"
        in (REPO / f).read_text(errors="ignore")
    ]
    assert rendered == []


def test_ac12_additive_registration_goes_to_user_settings_only(
    tmp_path, additive_binding
) -> None:
    result, _ = _install(tmp_path, additive_binding)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["SessionStart"] == [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "onex-claude-code-hook --binding session_start",
                }
            ]
        }
    ]
    assert result.module_path is None
    assert not (tmp_path / "mods").exists()


def test_ac12_a_project_settings_file_is_refused(tmp_path, additive_binding) -> None:
    request = ModelBindingInstallRequest(
        bindings=(additive_binding,),
        max_mode=EnumClientMaxMode.OBSERVE,
        mods_dir=str(tmp_path / "mods"),
        settings_path=str(tmp_path / ".claude" / "settings.local.json"),
        supported_probe_rows=("P16",),
    )
    with pytest.raises(BindingInstallError, match="user settings"):
        HandlerClaudeCodeBindingInstall().handle(request)


def test_ac12_a_row_whose_probe_rows_are_unsupported_is_not_installed(
    tmp_path, additive_binding
) -> None:
    result, _ = _install(tmp_path, additive_binding, supported=("P0",))
    assert result.skipped_bindings == ("session_start",)
    assert result.registered_commands == ()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_ac12_a_reinstall_with_a_row_removed_removes_its_registration(
    tmp_path, additive_binding, replacing_binding
) -> None:
    other = make_binding(
        name="per_prompt",
        kind=additive_binding.kind,
        hook_event="UserPromptSubmit",
        matcher_tool=None,
        probe_rows=("P16",),
    )
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]
                },
            }
        )
    )
    _install(tmp_path, additive_binding, other)
    result, _ = _install(tmp_path, other)
    settings = json.loads(settings_path.read_text())
    assert "SessionStart" not in settings["hooks"]
    assert settings["hooks"]["UserPromptSubmit"]
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "mine"
    assert settings["theme"] == "dark"
    assert result.removed_commands == (
        "onex-claude-code-hook --binding session_start",
        "onex-claude-code-hook --binding per_prompt",
    )


def test_removing_every_replacing_row_removes_the_mod_folder(
    tmp_path, replacing_binding
) -> None:
    result, _ = _install(tmp_path, replacing_binding)
    root = Path(result.module_path).parents[1]
    assert root.exists()
    _install(tmp_path)
    assert not root.exists()
