# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rescue-patch verdicts and scans against synthetic temporary git repositories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.rescue_patch_review import (
    handler_rescue_patch_review as compute_handler,
)
from omnimarket.models.rescue_patch_review import (
    EnumRescuePatchVerdict,
    ModelRescuePatchReviewRequest,
)
from omnimarket.nodes.node_rescue_patch_review_effect.handlers import (
    handler_rescue_patch_scan as scan_handler,
)
from omnimarket.nodes.node_rescue_patch_review_effect.handlers.adapter_compute_reviewer import (
    review_patch_with_compute_node as _review_patch,
)
from omnimarket.nodes.node_rescue_patch_review_effect.models import (
    ModelRescuePatchScanRequest,
    ModelRescuePatchScanResult,
)

pytestmark = pytest.mark.unit

NOW = "2026-10-15T06:00:00Z"

REAL_PATCH = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
-VALUE = 1
+VALUE = 2
 KEEP = True
"""

FORMAT_PATCH = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
-VALUE   =   1
+VALUE = 1
 KEEP = True
"""

FOLLOW_UP_PATCH = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
-VALUE = 2
+VALUE = 3
 KEEP = True
"""

JUNK_PATCH = """\
diff --git a/.pytest_cache/v/cache/lastfailed b/.pytest_cache/v/cache/lastfailed
new file mode 100644
--- /dev/null
+++ b/.pytest_cache/v/cache/lastfailed
@@ -0,0 +1 @@
+{}
diff --git a/dist/app.js b/dist/app.js
new file mode 100644
--- /dev/null
+++ b/dist/app.js
@@ -0,0 +1 @@
+x
"""


def _review(
    patch: str, *, applies: bool = True, present: bool = False
) -> tuple[EnumRescuePatchVerdict, str]:
    result = compute_handler.HandlerRescuePatchReview().handle(
        ModelRescuePatchReviewRequest(
            patch_text=patch, applies_to_base=applies, already_on_base=present
        )
    )
    return result.verdict, result.reason


def test_compute_empty_patch_is_discarded() -> None:
    verdict, reason = _review("")
    assert verdict is EnumRescuePatchVerdict.DISCARD_EMPTY
    assert "no changed file" in reason


def test_compute_a_change_already_on_the_base_is_superseded() -> None:
    verdict, reason = _review(REAL_PATCH, applies=False, present=True)
    assert verdict is EnumRescuePatchVerdict.DISCARD_SUPERSEDED
    assert "already on the base" in reason


def test_compute_build_output_and_caches_only_is_junk() -> None:
    verdict, reason = _review(JUNK_PATCH)
    assert verdict is EnumRescuePatchVerdict.DISCARD_JUNK
    assert ".pytest_cache/v/cache/lastfailed" in reason


def test_compute_one_real_file_among_junk_survives() -> None:
    verdict, _ = _review(JUNK_PATCH + REAL_PATCH)
    assert verdict is EnumRescuePatchVerdict.NEEDS_READ


def test_compute_whitespace_only_change_is_formatting() -> None:
    verdict, reason = _review(FORMAT_PATCH)
    assert verdict is EnumRescuePatchVerdict.DISCARD_FORMATTING
    assert "whitespace" in reason


def test_compute_a_real_change_that_applies_needs_a_read() -> None:
    verdict, reason = _review(REAL_PATCH)
    assert verdict is EnumRescuePatchVerdict.NEEDS_READ
    assert "applies to the base" in reason
    assert "src/app.py" in reason


def test_compute_a_real_change_that_no_longer_applies_still_needs_a_read() -> None:
    verdict, reason = _review(REAL_PATCH, applies=False)
    assert verdict is EnumRescuePatchVerdict.NEEDS_READ
    assert "no longer applies" in reason


# --- the effect node, against real git ---------------------------------------------------------


def _git(cwd: Path, *argv: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv],
        cwd=cwd,
        env=scrub_git_location_env(os.environ),
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    """An registry_root holding one clone with an origin/main, and an empty rescue root."""
    omni = tmp_path / "registry_root"
    clone = omni / "widgets"
    (clone / "src").mkdir(parents=True)
    _git(clone, "init", "-b", "main")
    (clone / "src/app.py").write_text("VALUE = 1\nKEEP = True\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "base")
    _git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    root = tmp_path / "worktree-rescue"
    root.mkdir()
    return {"omni": omni, "clone": clone, "root": root, "state": tmp_path / "state"}


def _save(
    root: Path, name: str, patch: str, *, repo: str = "OmniNode-ai/widgets"
) -> Path:
    directory = root / f"{name}-20261013" / f"{name}__widgets"
    directory.mkdir(parents=True)
    (directory / "MANIFEST.json").write_text(
        json.dumps({"repo": repo, "base": "origin/main", "ticket": name, "lane": "t"})
    )
    (directory / "commits.patch").write_text(patch)
    return directory


def _scan(w: dict[str, Path]) -> ModelRescuePatchScanResult:
    return scan_handler.HandlerRescuePatchScan(reviewer=_review_patch).handle(
        ModelRescuePatchScanRequest(
            rescue_root=w["root"],
            registry_root=w["omni"],
            state_dir=w["state"],
            now=NOW,
        )
    )


def _verdict(directory: Path) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((directory / "VERDICT.json").read_text())
    return loaded


def test_scan_writes_a_verdict_for_a_change_that_applies(
    world: dict[str, Path],
) -> None:
    saved = _save(world["root"], "keepme", REAL_PATCH)
    result = _scan(world)
    verdict = _verdict(saved)
    assert verdict["verdict"] == "needs-read"
    assert verdict["engine"] == "deterministic"
    assert verdict["decided_utc"] == NOW
    assert verdict["applies_to_base"] == {"origin/main": "clean"}
    assert result.reviewed == 1
    assert result.needs_read == 1
    assert result.discarded == 0


def test_scan_marks_a_change_already_on_the_base_superseded(
    world: dict[str, Path],
) -> None:
    clone = world["clone"]
    (clone / "src/app.py").write_text("VALUE = 2\nKEEP = True\n")
    _git(clone, "commit", "-am", "land the change")
    _git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    saved = _save(world["root"], "gone", REAL_PATCH)
    result = _scan(world)
    assert _verdict(saved)["verdict"] == "discard-superseded"
    assert result.discarded == 1


def test_scan_marks_a_change_that_no_longer_applies_for_a_read(
    world: dict[str, Path],
) -> None:
    clone = world["clone"]
    (clone / "src/app.py").write_text("VALUE = 7\nKEEP = True\n")
    _git(clone, "commit", "-am", "diverge")
    _git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    saved = _save(world["root"], "stale", REAL_PATCH)
    _scan(world)
    verdict = _verdict(saved)
    assert verdict["verdict"] == "needs-read"
    assert verdict["applies_to_base"] == {"origin/main": "fails"}


def test_scan_reads_commits_and_uncommitted_patches_as_one_sequence(
    world: dict[str, Path],
) -> None:
    saved = _save(world["root"], "both", REAL_PATCH)
    (saved / "uncommitted.patch").write_text(FOLLOW_UP_PATCH)
    _scan(world)
    assert _verdict(saved)["applies_to_base"] == {"origin/main": "clean"}


def test_scan_reads_the_repository_from_the_directory_name_when_the_manifest_has_none(
    world: dict[str, Path],
) -> None:
    saved = _save(world["root"], "older", REAL_PATCH)
    (saved / "MANIFEST.json").write_text(
        json.dumps({"base": "origin/main", "ticket": "older"})
    )
    _scan(world)
    assert _verdict(saved)["verdict"] == "needs-read"


def test_scan_never_rewrites_a_verdict_that_exists(world: dict[str, Path]) -> None:
    saved = _save(world["root"], "done", REAL_PATCH)
    (saved / "VERDICT.json").write_text(
        json.dumps({"verdict": "keep", "engine": "lab"})
    )
    result = _scan(world)
    assert _verdict(saved) == {"verdict": "keep", "engine": "lab"}
    assert result.reviewed == 0
    assert result.already_reviewed == 1


def test_scan_writes_no_verdict_when_the_repository_is_not_cloned(
    world: dict[str, Path],
) -> None:
    saved = _save(world["root"], "noclone", REAL_PATCH, repo="OmniNode-ai/absent")
    result = _scan(world)
    assert not (saved / "VERDICT.json").exists()
    assert result.unjudgeable == 1
    assert "absent" in result.detail[0]


def test_scan_writes_its_heartbeat_when_there_is_nothing_to_review(
    world: dict[str, Path],
) -> None:
    result = _scan(world)
    beat = json.loads((world["state"] / "last-run.json").read_text())
    assert beat["ran_utc"] == NOW
    assert beat["reviewed"] == 0
    assert beat["needs_read"] == 0
    assert result.reviewed == 0


def test_scan_a_missing_rescue_root_is_a_zero_not_a_crash_and_is_reported(
    world: dict[str, Path],
) -> None:
    world["root"].rmdir()
    result = _scan(world)
    assert result.reviewed == 0
    beat = json.loads((world["state"] / "last-run.json").read_text())
    assert beat["root_present"] is False


def test_scan_skips_the_discard_directory(world: dict[str, Path]) -> None:
    saved = _save(world["root"] / "discard", "old", REAL_PATCH)
    result = _scan(world)
    assert not (saved / "VERDICT.json").exists()
    assert result.reviewed == 0


@pytest.mark.parametrize(
    "path", ["dist/.env", "build/config.env", ".cache/credentials.json", "dist/KEY.PEM"]
)
def test_compute_secret_shaped_paths_inside_junk_need_a_read(path: str) -> None:
    patch = REAL_PATCH.replace("src/app.py", path)
    verdict, _ = _review(patch)
    assert verdict is EnumRescuePatchVerdict.NEEDS_READ


@pytest.mark.parametrize("path", ["src/build/app.py", "src/dist/app.py"])
def test_compute_nested_build_directories_need_a_read(path: str) -> None:
    verdict, _ = _review(REAL_PATCH.replace("src/app.py", path))
    assert verdict is EnumRescuePatchVerdict.NEEDS_READ


def test_compute_log_only_patch_is_junk() -> None:
    verdict, _ = _review(REAL_PATCH.replace("src/app.py", "run.log"))
    assert verdict is EnumRescuePatchVerdict.DISCARD_JUNK


def test_scan_unreadable_base_is_unjudgeable(world: dict[str, Path]) -> None:
    saved = _save(world["root"], "missing-base", REAL_PATCH)
    (saved / "MANIFEST.json").write_text(
        json.dumps({"repo": "widgets", "base": "missing-ref"})
    )
    result = _scan(world)
    assert result.unjudgeable == 1
    assert result.reviewed == 0
    assert not (saved / "VERDICT.json").exists()
    beat = json.loads((world["state"] / "last-run.json").read_text())
    assert beat["unjudgeable"] == 1
    assert "missing-ref" in beat["detail"][0]


def test_scan_preserves_the_real_index_and_checkout(world: dict[str, Path]) -> None:
    clone = world["clone"]
    (clone / "src/app.py").write_text("VALUE = 8\nKEEP = True\n")
    _git(clone, "add", "src/app.py")
    (clone / "src/app.py").write_text("VALUE = 9\nKEEP = True\n")
    status = _git(clone, "status", "--porcelain")
    index = (clone / ".git/index").read_bytes()
    saved = _save(world["root"], "isolated", REAL_PATCH)
    _scan(world)
    assert _verdict(saved)["applies_to_base"] == {"origin/main": "clean"}
    assert (clone / ".git/index").read_bytes() == index
    assert _git(clone, "status", "--porcelain") == status
    assert (clone / "src/app.py").read_text() == "VALUE = 9\nKEEP = True\n"
    beat = json.loads((world["state"] / "last-run.json").read_text())
    assert beat["pending"] == [str(saved)]


@pytest.mark.parametrize("manifest", ["{broken", "{}"])
def test_scan_unreadable_manifest_is_unjudgeable(
    world: dict[str, Path], manifest: str
) -> None:
    saved = _save(world["root"], "bad-manifest", REAL_PATCH)
    (saved / "MANIFEST.json").write_text(manifest)
    result = _scan(world)
    assert result.unjudgeable == 1
    assert result.reviewed == 0
    assert not (saved / "VERDICT.json").exists()


def test_node_entry_points_are_registered_without_console_scripts() -> None:
    import tomllib

    repo = Path(__file__).resolve().parents[3]
    project = tomllib.loads((repo / "pyproject.toml").read_text())["project"]
    for name in ("node_rescue_patch_review_compute", "node_rescue_patch_review_effect"):
        assert project["entry-points"]["onex.nodes"][name] == f"omnimarket.nodes.{name}"
    assert "onex-rescue-patch-review" not in project.get("scripts", {})


def test_cli_env_and_default_directories(
    world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from omnimarket.nodes.node_rescue_patch_review_effect.__main__ import main

    monkeypatch.setenv("OMNI_HOME", str(world["omni"]))
    home = world["state"].parent / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    assert main(["--now", NOW]) == 0
    assert capsys.readouterr().out == (
        "rescue-patch-review: reviewed=0 needs_read=0 discarded=0 "
        "already_reviewed=0 unjudgeable=0\n"
    )
    beat = json.loads(
        (home / ".local/state/omni/rescue-patch-review/last-run.json").read_text()
    )
    assert beat["rescue_root"] == str(world["root"])
    assert beat["ran_utc"] == NOW


def test_cli_explicit_paths_and_findings_exit_zero(world: dict[str, Path]) -> None:
    saved = _save(world["root"], "cli", REAL_PATCH)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_rescue_patch_review_effect",
            "--omni-home",
            str(world["omni"]),
            "--rescue-root",
            str(world["root"]),
            "--state-dir",
            str(world["state"]),
            "--now",
            NOW,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == (
        "rescue-patch-review: reviewed=1 needs_read=1 discarded=0 "
        "already_reviewed=0 unjudgeable=0\n"
    )
    assert _verdict(saved)["decided_utc"] == NOW


def test_cli_requires_registry_without_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_rescue_patch_review_effect.__main__ import main

    monkeypatch.delenv("OMNI_HOME", raising=False)
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2
    assert (
        "--omni-home is required when OMNI_HOME is not set" in capsys.readouterr().err
    )
