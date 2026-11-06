# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real adapters of the dev-merge redeploy trigger, against real git repositories (OMN-22478).

Each test builds a throwaway origin and clone in a temporary directory and runs the adapter's
real git, classifier child process and probe command; nothing is mocked except that the lane
probe command is a Python one-liner that prints a manifest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.dev_merge_redeploy_overlay import (
    ModelDevMergeRedeployPolicy,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.adapter_dev_merge_redeploy_git import (
    DeployPathClassifier,
    GitBuildContextRef,
    GitMergeFiles,
    ProvenanceLaneRevision,
    expand_clone_dir,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.protocol_dev_merge_redeploy import (
    BuildContextRefUnreadableError,
    ClassifierUnavailableError,
    LaneUnreadableError,
    MergeFilesUnreadableError,
)

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=cwd,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str, text: str = "x") -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"change {name}")
    return _git(repo, "rev-parse", "HEAD")


class Repos:
    """An origin and a clone that is one commit behind it."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin"
        self.origin.mkdir()
        _git(self.origin, "init", "-q", "-b", "dev")
        self.base = _commit(self.origin, "README.md")
        self.clone = root / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.clone)],
            env=scrub_git_location_env(os.environ),
            check=True,
        )

    def merge(self, name: str, text: str = "x") -> str:
        return _commit(self.origin, name, text)


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path)


def test_merge_files_fetches_a_commit_the_clone_lacks_and_lists_its_paths(
    repos: Repos,
) -> None:
    sha = repos.merge("src/pkg/mod.py")
    files = GitMergeFiles().changed_files(str(repos.clone), "dev", sha)
    assert files == ("src/pkg/mod.py",)


def test_merge_files_lists_the_source_path_of_a_rename_as_well(repos: Repos) -> None:
    body = "line\n" * 200
    first = repos.merge("src/pkg/mod.py", body)
    (repos.origin / "docs").mkdir()
    _git(repos.origin, "mv", "src/pkg/mod.py", "docs/mod.md")
    _git(repos.origin, "commit", "-q", "-m", "move the module out of src")
    moved = _git(repos.origin, "rev-parse", "HEAD")
    assert first != moved
    files = GitMergeFiles().changed_files(str(repos.clone), "dev", moved)
    assert set(files) == {"src/pkg/mod.py", "docs/mod.md"}


def test_merge_files_refuses_a_commit_that_no_fetch_supplies(repos: Repos) -> None:
    with pytest.raises(MergeFilesUnreadableError, match="not in the clone"):
        GitMergeFiles().changed_files(str(repos.clone), "dev", "e" * 40)


def test_merge_files_refuses_a_path_that_does_not_resolve(tmp_path: Path) -> None:
    with pytest.raises(MergeFilesUnreadableError, match="absolute"):
        GitMergeFiles().changed_files("relative/path", "dev", "e" * 40)


CLASSIFIER = """
def find_runtime_paths(changed_files, repository=None, package_only_file=None):
    # Reads the checkout it runs in, as the CI gate does for a node's contract.
    from pathlib import Path
    marker = Path("RUNTIME_MARKER").read_text().strip()
    return [f for f in changed_files if f.startswith(marker)]
"""


def _classifier(tmp_path: Path) -> Path:
    path = tmp_path / "classifier.py"
    path.write_text(CLASSIFIER)
    return path


def test_classifier_runs_against_a_checkout_of_the_merge(
    repos: Repos, tmp_path: Path
) -> None:
    (repos.origin / "RUNTIME_MARKER").write_text("src/")
    _git(repos.origin, "add", "-A")
    _git(repos.origin, "commit", "-q", "-m", "marker")
    sha = repos.merge("src/a.py")
    files = GitMergeFiles().changed_files(str(repos.clone), "dev", sha)
    got = DeployPathClassifier(str(_classifier(tmp_path))).runtime_paths(
        "OmniNode-ai/x", str(repos.clone), sha, [*files, "docs/b.md"]
    )
    assert got == ("src/a.py",)
    assert _git(repos.clone, "worktree", "list").count("\n") == 0, (
        "the checkout is removed"
    )


def test_classifier_that_cannot_run_is_unavailable_never_not_runtime(
    repos: Repos, tmp_path: Path
) -> None:
    sha = repos.merge(
        "src/a.py"
    )  # no RUNTIME_MARKER in this tree: the classifier raises
    GitMergeFiles().changed_files(str(repos.clone), "dev", sha)
    with pytest.raises(ClassifierUnavailableError, match="exited"):
        DeployPathClassifier(str(_classifier(tmp_path))).runtime_paths(
            "OmniNode-ai/x", str(repos.clone), sha, ["src/a.py"]
        )
    with pytest.raises(ClassifierUnavailableError, match="does not exist"):
        DeployPathClassifier(str(tmp_path / "absent.py")).runtime_paths(
            "OmniNode-ai/x", str(repos.clone), sha, ["src/a.py"]
        )


def test_build_context_ref_is_the_fetched_tip(repos: Repos) -> None:
    tip = repos.merge("later.py")
    assert GitBuildContextRef().resolve(str(repos.clone), "dev") == tip


def test_build_context_ref_for_a_missing_branch_is_unreadable(repos: Repos) -> None:
    with pytest.raises(BuildContextRefUnreadableError):
        GitBuildContextRef().resolve(str(repos.clone), "no-such-branch")


def _probe(manifest: object, rc: int = 0) -> list[str]:
    body = json.dumps(manifest) if not isinstance(manifest, str) else manifest
    return [
        sys.executable,
        "-c",
        f"import sys; sys.stdout.write({body!r}); sys.exit({rc})",
    ]


def _manifest(sha: str) -> dict[str, object]:
    return {"per_repo_vcs_provenance": {"siblings": {"omnimarket": {"vcs_ref": sha}}}}


def test_lane_revision_reads_the_vendored_ref_from_the_manifest() -> None:
    lane = ProvenanceLaneRevision(_probe(_manifest("a" * 40)))
    assert lane.vendored_revision("omnimarket", "siblings") == "a" * 40


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        (_probe(_manifest("a" * 40), rc=3), "exited 3"),
        (_probe("not json"), "no vendored ref"),
        (_probe({"per_repo_vcs_provenance": {"siblings": {}}}), "no vendored ref"),
        (_probe(_manifest("main")), "not a 40-hex"),
        (["/nonexistent/probe"], "did not run"),
    ],
)
def test_lane_revision_unreadable_cases(argv: list[str], why: str) -> None:
    with pytest.raises(LaneUnreadableError, match=why):
        ProvenanceLaneRevision(argv).vendored_revision("omnimarket", "siblings")


def test_lane_revision_reads_the_build_context_section_for_the_repository_the_lane_builds_in() -> (
    None
):
    manifest = {
        "per_repo_vcs_provenance": {
            "siblings": {"infra": {"vcs_ref": "b" * 40}},
            "build_context": {"infra": {"vcs_ref": "c" * 40}},
        }
    }
    lane = ProvenanceLaneRevision(_probe(manifest))
    assert lane.vendored_revision("infra", "build_context") == "c" * 40
    assert lane.vendored_revision("infra", "siblings") == "b" * 40


def test_lane_revision_names_a_repository_missing_from_the_section_it_was_asked() -> (
    None
):
    manifest = {
        "per_repo_vcs_provenance": {"siblings": {"infra": {"vcs_ref": "b" * 40}}}
    }
    with pytest.raises(LaneUnreadableError, match="no vendored ref"):
        ProvenanceLaneRevision(_probe(manifest)).vendored_revision(
            "infra", "build_context"
        )


def test_lane_carries_is_containment_not_equality(repos: Repos) -> None:
    merge = repos.merge("a.py")
    later = repos.merge("b.py")
    lane = ProvenanceLaneRevision(_probe(_manifest(merge)))
    clone = str(repos.clone)
    assert lane.lane_carries(clone, "dev", merge, merge) is True
    assert lane.lane_carries(clone, "dev", merge, later) is True
    assert lane.lane_carries(clone, "dev", later, merge) is False
    with pytest.raises(LaneUnreadableError, match="not in the clone"):
        lane.lane_carries(clone, "dev", merge, "d" * 40)


def test_expand_clone_dir_resolves_omni_home_and_refuses_the_unresolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    assert expand_clone_dir("$OMNI_HOME/x") == tmp_path / "x"
    monkeypatch.delenv("OMNI_HOME")
    with pytest.raises(ValueError, match="absolute"):
        expand_clone_dir("$OMNI_HOME/x")


def test_the_packaged_overlay_example_loads_through_the_policy_model() -> None:
    from omnimarket.nodes import node_dev_merge_redeploy_trigger_effect as pkg

    example = Path(pkg.__file__).parent / "overlay.example.yaml"
    policy = ModelDevMergeRedeployPolicy.model_validate(
        yaml.safe_load(example.read_text())
    )
    assert policy.repos[0].base_branches == ("dev",)


def test_the_policy_refuses_a_repository_listed_twice() -> None:
    from omnimarket.nodes import node_dev_merge_redeploy_trigger_effect as pkg

    document = yaml.safe_load(
        (Path(pkg.__file__).parent / "overlay.example.yaml").read_text()
    )
    document["repos"] = [document["repos"][0], document["repos"][0]]
    with pytest.raises(ValueError, match="listed twice"):
        ModelDevMergeRedeployPolicy.model_validate(document)


def test_the_bus_opener_arguments_follow_the_opener_signature() -> None:
    from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.__main__ import (
        _opener_arguments,
    )

    root = Path("/workspace-root")
    arguments = _opener_arguments("kafka", "dev", None, root)
    keywords = list(arguments)
    assert keywords[:3] == ["bus", "lane", "kafka_bootstrap"]
    assert list(arguments.values()) == ["kafka", "dev", None, root]
