# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit proof for the release-on-merge decision step (OMN-18010).

WHAT FAILED, MEASURED
---------------------
A release here is a manual, ticket-driven step with no merge trigger, "Done" is
measured at merge rather than at release, and nothing audits the distance between
dev and the last tag. Consequence: ``omnimarket#2304`` merged 2026-09-05 and sat
unreleased with its release ticket in Backlog; ``#2334`` did the same; a PRD
scoring pass found five landed-but-not-deployed items at once.

``.github/workflows/release-on-merge.yml`` releases on the push a squash merge to
dev produces. ``scripts/ci/decide_release_on_merge.py`` is that workflow's entire
decision surface, and this module is its proof — the alternative to unit tests
here is merging something to find out, which is exactly the feedback loop that
let the original problem hide.

THE DECISION MATRIX, one leg per failure mode
---------------------------------------------
  * dev ahead of the highest tag  -> release at dev's version, no bump
  * dev level with the highest tag -> needs_bump, target = tag + 1 patch
  * a re-run after the release already landed -> needs_bump, never a second
    release at the same version
  * docs-only / no packaged path changed -> skip
  * the head subject carries the self-push marker -> skip (the anti-loop guard:
    without it, the post-release "open dev for the next version" commit touches
    pyproject.toml, re-enters the trigger, and releases an empty version once
    per merge forever)
  * a malformed or non-final [project].version -> exit 2, never a silent skip

Every skip carries a machine-readable ``skip_reason``: an unexplained skip and a
broken trigger look identical in a run log, and telling them apart after the fact
is the whole job of this gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.decide_release_on_merge import (
    SELF_PUSH_MARKER,
    SKIP_NO_PACKAGED_CHANGE,
    SKIP_SELF_PUSH,
    ReleaseDecisionError,
    collect_changed_paths,
    decide,
    highest_published,
    is_packaged_path,
    next_patch,
    parse_final_version,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "decide_release_on_merge.py"

TAGS = ["v0.4.9", "v0.4.17", "v0.4.18", "v1.0.0rc1", "not-a-tag"]


# ---------------------------------------------------------------------------
# Tag ordering — the lexical trap
# ---------------------------------------------------------------------------


def test_highest_published_orders_numerically_not_lexically() -> None:
    # A string sort puts v0.4.9 AFTER v0.4.18, which would make every decision
    # compare against the wrong release. omnimarket lives exactly in that range.
    assert highest_published(["v0.4.9", "v0.4.18", "v0.4.17"]) == "v0.4.18"


def test_highest_published_ignores_non_release_tags() -> None:
    assert highest_published(["v1.0.0rc1", "not-a-tag", "v0.4.2"]) == "v0.4.2"


def test_highest_published_is_empty_for_a_repo_with_no_release_tags() -> None:
    assert highest_published(["not-a-tag", "sprint-2026-09"]) == ""


def test_next_patch_increments_only_the_patch_component() -> None:
    assert next_patch("v0.4.9") == "0.4.10"
    assert next_patch("0.4.18") == "0.4.19"


# ---------------------------------------------------------------------------
# What counts as a packaged change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "src/omnimarket/__init__.py",
        "src/omnimarket/nodes/x/handler.py",
        "pyproject.toml",
        "uv.lock",
    ],
)
def test_packaged_paths_are_releasable(path: str) -> None:
    assert is_packaged_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "docs/README.md",
        "tests/unit/test_x.py",
        ".github/workflows/ci.yml",
        "scripts/ci/decide_release_on_merge.py",
        "",
        "   ",
        # A near-miss that must NOT match the src/ prefix.
        "srcx/thing.py",
    ],
)
def test_unpackaged_paths_are_not_releasable(path: str) -> None:
    assert is_packaged_path(path) is False


# ---------------------------------------------------------------------------
# The decision matrix
# ---------------------------------------------------------------------------


def _decide(
    *,
    dev_version: str = "0.4.19",
    tags: list[str] | None = None,
    changed_paths: list[str] | None = None,
    head_subject: str = "feat(OMN-1234): a real source merge (#2345)",
):
    return decide(
        dev_version=dev_version,
        tags=TAGS if tags is None else tags,
        changed_paths=["src/omnimarket/x.py"]
        if changed_paths is None
        else changed_paths,
        head_subject=head_subject,
    )


def test_dev_ahead_releases_at_devs_own_version_with_no_bump() -> None:
    # The steady state. The OMN-16344 release-identity gate already refuses any
    # src/** PR whose version is not ahead of the highest published tag, so by
    # the time a merge lands, dev's version IS the next release version and the
    # run simply tags it — no bump commit, no rebase, no race.
    decision = _decide(dev_version="0.4.19")
    assert decision.skip is False
    assert decision.needs_bump is False
    assert decision.version == "0.4.19"
    assert decision.latest_tag == "v0.4.18"


def test_dev_level_with_the_tag_needs_a_bump_to_tag_plus_one() -> None:
    decision = _decide(dev_version="0.4.18")
    assert decision.skip is False
    assert decision.needs_bump is True
    assert decision.version == "0.4.19"
    assert "ARMED" in decision.reason


def test_dev_behind_the_tag_still_targets_tag_plus_one() -> None:
    # Never dev+1: that would still be <= the published version, so the release
    # would collide with an existing tag.
    decision = _decide(dev_version="0.4.10")
    assert decision.needs_bump is True
    assert decision.version == "0.4.19"


def test_a_repo_with_no_release_tags_releases_at_devs_version() -> None:
    decision = _decide(dev_version="0.1.0", tags=["not-a-tag"])
    assert decision.skip is False
    assert decision.needs_bump is False
    assert decision.version == "0.1.0"
    assert decision.latest_tag == ""


def test_a_rerun_after_the_release_landed_asks_for_a_bump_not_a_second_release() -> (
    None
):
    # The no-double-release property, stated as the invariant that actually
    # holds it: re-running the same push after v0.4.19 was tagged sees dev level
    # with the highest tag, so it asks for a BUMP rather than re-releasing
    # 0.4.19. There is no separate "tag already exists" skip because the target
    # is either dev's version when dev is strictly above the highest tag, or
    # highest+1 — neither of which can already be a tag.
    decision = _decide(dev_version="0.4.19", tags=[*TAGS, "v0.4.19"])
    assert decision.skip is False
    assert decision.needs_bump is True
    assert decision.version == "0.4.20"
    assert decision.version != "0.4.19"


def test_the_resolved_target_is_never_an_existing_tag() -> None:
    # The invariant above, exercised across the whole neighbourhood of the
    # published range rather than asserted once.
    tags = ["v0.4.9", "v0.4.17", "v0.4.18", "v0.4.19"]
    for dev in ("0.4.10", "0.4.18", "0.4.19", "0.4.20", "0.5.0"):
        decision = _decide(dev_version=dev, tags=tags)
        assert decision.skip is False
        assert f"v{decision.version}" not in tags


def test_a_docs_only_push_is_a_skip() -> None:
    decision = _decide(changed_paths=["docs/README.md", "tests/unit/test_x.py"])
    assert decision.skip is True
    assert decision.skip_reason == SKIP_NO_PACKAGED_CHANGE


def test_an_empty_changed_set_is_a_skip_not_a_release() -> None:
    decision = _decide(changed_paths=[])
    assert decision.skip is True
    assert decision.skip_reason == SKIP_NO_PACKAGED_CHANGE


def test_the_release_trains_own_push_is_a_skip() -> None:
    # The anti-loop guard. The post-release commit touches pyproject.toml and
    # uv.lock, so it matches the paths filter; only the subject marker stops it
    # from releasing an empty version once per merge, forever.
    decision = _decide(
        dev_version="0.4.20",
        head_subject=f"chore(release): open dev for v0.4.20 {SELF_PUSH_MARKER}",
        changed_paths=["pyproject.toml", "uv.lock"],
    )
    assert decision.skip is True
    assert decision.skip_reason == SKIP_SELF_PUSH


def test_the_self_push_guard_wins_over_a_releasable_source_change() -> None:
    # Ordering proof: a self-push that also touched src/** must still skip.
    decision = _decide(
        head_subject=f"chore(release): v0.4.19 {SELF_PUSH_MARKER}",
        changed_paths=["src/omnimarket/x.py"],
    )
    assert decision.skip is True
    assert decision.skip_reason == SKIP_SELF_PUSH


@pytest.mark.parametrize(
    "dev_version", ["0.4.19rc1", "0.4", "", "latest", "0.4.19.post1", "v0.4.19-rc.1"]
)
def test_a_non_final_dev_version_raises_rather_than_skipping(dev_version: str) -> None:
    # A repo that cannot state its own version is a repo whose next release is
    # already broken. Reporting that as "nothing to do" is the exact failure
    # class this ticket exists to end.
    with pytest.raises(ReleaseDecisionError):
        _decide(dev_version=dev_version)


def test_a_non_final_dev_version_raises_even_for_a_docs_only_push() -> None:
    # The version is resolved BEFORE the skip checks on purpose: otherwise a
    # docs-only push masks the broken version string indefinitely.
    with pytest.raises(ReleaseDecisionError):
        _decide(dev_version="0.4.19rc1", changed_paths=["docs/README.md"])


def test_parse_final_version_accepts_a_leading_v() -> None:
    assert parse_final_version("v0.4.18", label="tag") == (0, 4, 18)


# ---------------------------------------------------------------------------
# The git-facing helpers and the CLI, against a real throwaway repository
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    # These OVERRIDE both `-C` and cwd; a leaked value from the caller's own
    # environment would silently point every command at another repository.
    for leaked in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(leaked, None)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev")
    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "omnimarket"\nversion = "0.4.18"\n', encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "tag", "v0.4.18")
    return root


def test_collect_changed_paths_uses_the_push_range(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "docs.md").write_text("d\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs")
    (repo / "src" / "mod.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "src")
    after = _git(repo, "rev-parse", "HEAD")

    # A push can carry several commits, so the workflow's `paths:` filter alone
    # is not sufficient — the range must be re-derived.
    assert set(collect_changed_paths(repo, before, after)) == {"docs.md", "src/mod.py"}


def test_collect_changed_paths_falls_back_to_the_head_commit_for_a_zero_before(
    repo: Path,
) -> None:
    (repo / "src" / "mod.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "src only")
    after = _git(repo, "rev-parse", "HEAD")
    zero = "0" * 40
    assert collect_changed_paths(repo, zero, after) == ["src/mod.py"]


def _run_cli(
    repo: Path, before: str, after: str, output: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--before",
            before,
            "--after",
            after,
            "--github-output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_emits_json_and_step_outputs_for_a_releasable_merge(
    repo: Path, tmp_path: Path
) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "omnimarket"\nversion = "0.4.19"\n', encoding="utf-8"
    )
    (repo / "src" / "mod.py").write_text("x = 9\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat(OMN-1): real change (#1)")
    after = _git(repo, "rev-parse", "HEAD")

    output = tmp_path / "gh_output"
    result = _run_cli(repo, before, after, output)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["skip"] is False
    assert payload["version"] == "0.4.19"
    assert payload["needs_bump"] is False

    written = output.read_text(encoding="utf-8")
    assert "skip=false" in written
    assert "version=0.4.19" in written
    assert "needs_bump=false" in written


def test_cli_skips_a_docs_only_merge(repo: Path, tmp_path: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs(OMN-1): readme (#2)")
    after = _git(repo, "rev-parse", "HEAD")

    output = tmp_path / "gh_output"
    result = _run_cli(repo, before, after, output)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["skip"] is True
    assert payload["skip_reason"] == SKIP_NO_PACKAGED_CHANGE
    assert "skip=true" in output.read_text(encoding="utf-8")


def test_cli_exits_2_on_a_non_final_project_version(repo: Path, tmp_path: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "omnimarket"\nversion = "0.4.19rc1"\n', encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: rc")
    after = _git(repo, "rev-parse", "HEAD")

    result = _run_cli(repo, before, after, tmp_path / "gh_output")
    assert result.returncode == 2
    assert "final X.Y.Z" in result.stderr


def test_cli_exits_2_and_reports_stderr_when_the_ref_is_unusable(
    repo: Path, tmp_path: Path
) -> None:
    # A sweep that swallows stderr reports a clean bill of health for a command
    # that never ran; this proves the failure is surfaced instead.
    result = _run_cli(repo, "", "deadbeef" * 5, tmp_path / "gh_output")
    assert result.returncode == 2
    assert result.stderr.strip()
