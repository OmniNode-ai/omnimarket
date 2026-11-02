# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_overlay_tree_install_effect: the overlay tree is built whole from a commit, checked, smoked, swapped, recorded."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_overlay_tree_install_effect.handlers.handler_overlay_tree_install import (
    HandlerOverlayTreeInstall,
    tree_of,
)
from omnimarket.nodes.node_overlay_tree_install_effect.models.model_overlay_tree_install import (
    ModelOverlayCheck,
    ModelOverlayTreeInstallRequest,
)

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_overlay_tree_install_effect"
PLUGIN = "plugins/onex-overlays"


def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, env=scrub_git_location_env(os.environ), **kw)


def git(clone: Path, *args: str) -> str:
    done = _run(
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def write_plugin(
    clone: Path, overlays: dict[str, str], name: str = "onex-overlays"
) -> None:
    base = clone / PLUGIN
    (base / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (base / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name}), encoding="utf-8"
    )
    for overlay, text in overlays.items():
        (base / "overlays" / overlay).mkdir(parents=True, exist_ok=True)
        (base / "overlays" / overlay / "overlay.yaml").write_text(
            text, encoding="utf-8"
        )


def commit(clone: Path, message: str) -> str:
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", message)
    return git(clone, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    path = tmp_path / "clone"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


def request(tmp_path: Path, ref: str, **kw: Any) -> ModelOverlayTreeInstallRequest:
    fields: dict[str, Any] = {
        "operation": "install",
        "install_path": str(tmp_path / "cache" / "0.1.0"),
        "clone": str(tmp_path / "clone"),
        "ref": ref,
        "record": str(tmp_path / "state" / "overlay_tree.json"),
    }
    fields.update(kw)
    return ModelOverlayTreeInstallRequest(**fields)


def test_install_builds_the_tree_whole_equal_to_the_commit_and_records_the_revision(
    tmp_path: Path, clone: Path
) -> None:
    write_plugin(clone, {"floor": "owner_login: someone\n"})
    first = commit(clone, "one")
    manifest = tmp_path / "installed_plugins.json"
    dest = tmp_path / "cache" / "0.1.0"
    manifest.write_text(
        json.dumps(
            {
                "plugins": {
                    "onex-overlays@x": [
                        {"installPath": str(dest), "gitCommitSha": "old"}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    result = HandlerOverlayTreeInstall().handle(
        request(tmp_path, first, installed_plugins=str(manifest))
    )
    assert result.verdict == "pass", result
    assert result.commit == first
    assert tree_of(dest)[
        ".claude-plugin/plugin.json"
    ]  # the dotfile directory is shipped
    recorded = json.loads(
        (tmp_path / "state" / "overlay_tree.json").read_text(encoding="utf-8")
    )
    assert recorded["trees"]["0.1.0"]["commit"] == first
    entry = json.loads(manifest.read_text(encoding="utf-8"))["plugins"][
        "onex-overlays@x"
    ][0]
    assert entry["gitCommitSha"] == first


def test_a_branch_installs_its_tip_and_the_previous_tree_is_kept(
    tmp_path: Path, clone: Path
) -> None:
    write_plugin(clone, {"floor": "a: 1\n"})
    first = commit(clone, "one")
    git(clone, "checkout", "-q", "-b", "fix")
    write_plugin(clone, {"floor": "a: 1\nowner_login: someone\n"})
    tip = commit(clone, "two")
    handler = HandlerOverlayTreeInstall()
    assert handler.handle(request(tmp_path, first)).verdict == "pass"
    result = handler.handle(request(tmp_path, "fix"))
    assert result.verdict == "pass"
    assert result.commit == tip
    dest = tmp_path / "cache" / "0.1.0"
    assert "owner_login" in (dest / "overlays/floor/overlay.yaml").read_text(
        encoding="utf-8"
    )
    assert result.kept
    assert (tmp_path / "cache" / result.kept).is_dir()


def test_an_overlay_tree_that_differs_from_its_commit_is_refused_and_nothing_is_installed(
    tmp_path: Path, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_plugin(clone, {"floor": "a: 1\n"})
    first = commit(clone, "one")
    handler = HandlerOverlayTreeInstall()
    real_build = handler.build

    def drifting_build(
        clone_path: Path, sha: str, subdir: str, staged: Path
    ) -> dict[str, str]:
        shipped = real_build(clone_path, sha, subdir, staged)
        (staged / "overlays" / "floor" / "overlay.yaml").write_text(
            "a: 2\n", encoding="utf-8"
        )
        return shipped

    monkeypatch.setattr(handler, "build", drifting_build)
    result = handler.handle(request(tmp_path, first))
    assert result.verdict == "refused"
    assert [c.name for c in result.checks if not c.ok] == ["tree-equal"]
    assert not (tmp_path / "cache" / "0.1.0").exists()


def test_a_tree_that_fails_the_smoke_is_refused_and_the_installed_tree_stays(
    tmp_path: Path, clone: Path
) -> None:
    write_plugin(clone, {"floor": "a: 1\n"})
    good = commit(clone, "good")
    write_plugin(clone, {"floor": "a: [unclosed\n"})
    bad = commit(clone, "bad")
    handler = HandlerOverlayTreeInstall()
    assert handler.handle(request(tmp_path, good)).verdict == "pass"
    result = handler.handle(request(tmp_path, bad))
    assert result.verdict == "refused"
    assert any(c.name.startswith("parses ") and not c.ok for c in result.checks)
    assert (tmp_path / "cache" / "0.1.0" / "overlays/floor/overlay.yaml").read_text(
        encoding="utf-8"
    ) == "a: 1\n"


def test_an_installed_tree_that_fails_its_second_check_is_rolled_back_and_the_manifest_restored(
    tmp_path: Path, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_plugin(clone, {"floor": "a: 1\n"})
    first = commit(clone, "one")
    write_plugin(clone, {"floor": "a: 2\n"})
    second = commit(clone, "two")
    manifest = tmp_path / "installed_plugins.json"
    dest = tmp_path / "cache" / "0.1.0"
    manifest.write_text(
        json.dumps(
            {
                "plugins": {
                    "onex-overlays@x": [
                        {"installPath": str(dest), "gitCommitSha": first}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    handler = HandlerOverlayTreeInstall()
    assert handler.handle(request(tmp_path, first)).verdict == "pass"
    calls = {"n": 0}
    real_smoke = handler.smoke

    def smoke_fails_when_installed(root: Path, name: str) -> list[ModelOverlayCheck]:
        calls["n"] += 1
        if (
            calls["n"] == 2
        ):  # the first call smokes the staged tree, the second the installed one
            return [ModelOverlayCheck(name="manifest", ok=False, detail="injected")]
        return real_smoke(root, name)

    monkeypatch.setattr(handler, "smoke", smoke_fails_when_installed)
    result = handler.handle(request(tmp_path, second, installed_plugins=str(manifest)))
    assert result.verdict == "rolled-back"
    assert (dest / "overlays/floor/overlay.yaml").read_text(
        encoding="utf-8"
    ) == "a: 1\n"
    assert (
        json.loads(manifest.read_text(encoding="utf-8"))["plugins"]["onex-overlays@x"][
            0
        ]["gitCommitSha"]
        == first
    )
    recorded = json.loads(
        (tmp_path / "state" / "overlay_tree.json").read_text(encoding="utf-8")
    )
    assert recorded["trees"]["0.1.0"]["commit"] == first


def test_verify_checks_the_installed_tree_against_its_recorded_revision(
    tmp_path: Path, clone: Path
) -> None:
    write_plugin(clone, {"floor": "a: 1\n"})
    first = commit(clone, "one")
    handler = HandlerOverlayTreeInstall()
    handler.handle(request(tmp_path, first))
    assert handler.handle(request(tmp_path, "", operation="verify")).verdict == "pass"
    verified = handler.handle(
        request(tmp_path, "", operation="verify", clone=str(clone))
    )
    assert verified.verdict == "pass"
    assert verified.commit == first
    (tmp_path / "cache" / "0.1.0" / "overlays/floor/overlay.yaml").write_text(
        "a: hand edit\n", encoding="utf-8"
    )
    edited = handler.handle(request(tmp_path, "", operation="verify", clone=str(clone)))
    assert edited.verdict == "fail"
    assert [c.name for c in edited.checks if not c.ok] == ["tree-equal"]


def test_a_dry_run_installs_nothing(tmp_path: Path, clone: Path) -> None:
    write_plugin(clone, {"floor": "a: 1\n"})
    result = HandlerOverlayTreeInstall().handle(
        request(tmp_path, commit(clone, "one"), dry_run=True)
    )
    assert result.verdict == "dry-run"
    assert not (tmp_path / "cache" / "0.1.0").exists()


def test_an_unknown_revision_is_refused(tmp_path: Path, clone: Path) -> None:
    write_plugin(clone, {"floor": "a: 1\n"})
    commit(clone, "one")
    result = HandlerOverlayTreeInstall().handle(request(tmp_path, "no-such-branch"))
    assert result.verdict == "refused"


def test_the_contract_names_the_handler_and_the_node_is_registered() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["name"] == "node_overlay_tree_install_effect"
    assert hasattr(
        importlib.import_module(contract["handler"]["module"]),
        contract["handler"]["name"],
    )
    assert hasattr(
        importlib.import_module(contract["entrypoint"]["module"]),
        contract["entrypoint"]["name"],
    )
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert (
        pyproject["project"]["entry-points"]["onex.nodes"][
            "node_overlay_tree_install_effect"
        ]
        == "omnimarket.nodes.node_overlay_tree_install_effect"
    )
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == contract["name"]
