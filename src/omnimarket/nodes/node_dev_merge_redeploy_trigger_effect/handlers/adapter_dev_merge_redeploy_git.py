# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real adapters for the dev-merge redeploy trigger's four outside reads (OMN-22478).

Git is read through the ``git`` program, the deploy-path classifier is the module the CI deploy
gate already uses (loaded by path in a child interpreter, run inside a detached checkout of the
merge so its per-node contract reads see the merge's tree), and the lane is read through the
deployment's own probe command from the node overlay. Every failure is one of the typed
unreadable errors of the protocol module; no adapter returns a value it could not read.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.protocol_dev_merge_redeploy import (
    BuildContextRefUnreadableError,
    ClassifierUnavailableError,
    LaneUnreadableError,
    MergeFilesUnreadableError,
)

NODE = "node_dev_merge_redeploy_trigger_effect"
GIT_TIMEOUT_SECONDS: float = load_node_timeouts(NODE).get("git_timeout_seconds")
CLASSIFIER_TIMEOUT_SECONDS: float = load_node_timeouts(NODE).get(
    "classifier_timeout_seconds"
)
LANE_PROBE_TIMEOUT_SECONDS: float = load_node_timeouts(NODE).get(
    "lane_probe_timeout_seconds"
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
Runner = Callable[..., Any]

# Run by a child interpreter inside a detached checkout of the merge: argv is
# (classifier path, repository). Reads the changed files as JSON on stdin and prints the
# runtime subset as JSON. -I keeps the checkout's own modules off the import path.
_CLASSIFY_SNIPPET = (
    "import importlib.util, json, sys\n"
    "spec = importlib.util.spec_from_file_location('deploy_path_classifier', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "sys.modules['deploy_path_classifier'] = mod\n"
    "spec.loader.exec_module(mod)\n"
    "files = json.load(sys.stdin)\n"
    "print(json.dumps(list(mod.find_runtime_paths(files, repository=sys.argv[2]))))\n"
)


def expand_clone_dir(value: str) -> Path:
    """``$OMNI_HOME/x`` or ``~/x`` resolved in this process; the result must be absolute."""
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute() or "$" in str(path):
        raise ValueError(f"clone path {value!r} does not resolve to an absolute path")
    return path


_RETARGETING = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"})


def _git_env() -> dict[str, str]:
    """This process's environment without the variables that retarget git away from ``-C``."""
    return {k: v for k, v in os.environ.items() if k not in _RETARGETING}


def _git(
    run: Runner, clone: Path, args: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        done: subprocess.CompletedProcess[str] = run(
            ["git", "-C", str(clone), *args],
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git did not run: {type(exc).__name__}: {exc}") from exc
    return done


def _has_commit(run: Runner, clone: Path, sha: str, timeout: float) -> bool:
    return (
        _git(run, clone, ["cat-file", "-e", f"{sha}^{{commit}}"], timeout).returncode
        == 0
    )


def _fetch_branch(run: Runner, clone: Path, branch: str, timeout: float) -> None:
    done = _git(
        run,
        clone,
        [
            "fetch",
            "--quiet",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ],
        timeout,
    )
    if done.returncode != 0:
        raise RuntimeError(f"fetch of {branch} failed: {done.stderr.strip()[-300:]}")


def _ensure_commit(
    run: Runner, clone: Path, branch: str, sha: str, timeout: float
) -> None:
    if _has_commit(run, clone, sha, timeout):
        return
    _fetch_branch(run, clone, branch, timeout)
    if not _has_commit(run, clone, sha, timeout):
        raise RuntimeError(f"{sha[:12]} is not in the clone after fetching {branch}")


class GitMergeFiles:
    """Changed paths of a squash merge against its first parent, from the host's clone."""

    def __init__(
        self, *, run: Runner = subprocess.run, timeout: float = GIT_TIMEOUT_SECONDS
    ) -> None:
        self._run = run
        self._timeout = timeout

    def changed_files(
        self, clone_dir: str, base: str, merge_sha: str
    ) -> tuple[str, ...]:
        try:
            clone = expand_clone_dir(clone_dir)
            _ensure_commit(self._run, clone, base, merge_sha, self._timeout)
            done = _git(
                self._run,
                clone,
                [
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "-z",
                    f"{merge_sha}^1",
                    merge_sha,
                ],
                self._timeout,
            )
        except (ValueError, RuntimeError) as exc:
            raise MergeFilesUnreadableError(str(exc)) from exc
        if done.returncode != 0:
            raise MergeFilesUnreadableError(
                f"git diff failed: {done.stderr.strip()[-300:]}"
            )
        return tuple(p for p in done.stdout.split("\0") if p)


class DeployPathClassifier:
    """The CI deploy gate's runtime-path classifier, run against a checkout of the merge."""

    def __init__(
        self,
        classifier_path: str,
        *,
        run: Runner = subprocess.run,
        git_timeout: float = GIT_TIMEOUT_SECONDS,
        timeout: float = CLASSIFIER_TIMEOUT_SECONDS,
    ) -> None:
        self._classifier = classifier_path
        self._run = run
        self._git_timeout = git_timeout
        self._timeout = timeout

    def runtime_paths(
        self, repo: str, clone_dir: str, merge_sha: str, files: Sequence[str]
    ) -> tuple[str, ...]:
        try:
            clone = expand_clone_dir(clone_dir)
            classifier = expand_clone_dir(self._classifier)
        except ValueError as exc:
            raise ClassifierUnavailableError(str(exc)) from exc
        if not classifier.is_file():
            raise ClassifierUnavailableError(
                f"classifier file {classifier} does not exist"
            )
        with tempfile.TemporaryDirectory(prefix="dev-merge-classify-") as tmp:
            tree = Path(tmp) / "tree"
            try:
                added = _git(
                    self._run,
                    clone,
                    ["worktree", "add", "--detach", "--quiet", str(tree), merge_sha],
                    self._git_timeout,
                )
                if added.returncode != 0:
                    raise ClassifierUnavailableError(
                        f"checkout of {merge_sha[:12]} failed: {added.stderr.strip()[-300:]}"
                    )
                try:
                    return self._classify(repo, classifier, tree, files)
                finally:
                    _git(
                        self._run,
                        clone,
                        ["worktree", "remove", "--force", str(tree)],
                        self._git_timeout,
                    )
            except RuntimeError as exc:
                raise ClassifierUnavailableError(str(exc)) from exc

    def _classify(
        self, repo: str, classifier: Path, tree: Path, files: Sequence[str]
    ) -> tuple[str, ...]:
        try:
            done = self._run(
                [sys.executable, "-I", "-c", _CLASSIFY_SNIPPET, str(classifier), repo],
                input=json.dumps(list(files)),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=tree,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClassifierUnavailableError(
                f"classifier did not run: {type(exc).__name__}: {exc}"
            ) from exc
        if done.returncode != 0:
            raise ClassifierUnavailableError(
                f"classifier exited {done.returncode}: {done.stderr.strip()[-300:]}"
            )
        try:
            parsed = json.loads(done.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise ClassifierUnavailableError("classifier printed no JSON list") from exc
        if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
            raise ClassifierUnavailableError("classifier did not print a list of paths")
        return tuple(parsed)


class GitBuildContextRef:
    """The tip of the build-context repository's branch, fetched fresh."""

    def __init__(
        self, *, run: Runner = subprocess.run, timeout: float = GIT_TIMEOUT_SECONDS
    ) -> None:
        self._run = run
        self._timeout = timeout

    def resolve(self, clone_dir: str, branch: str) -> str:
        try:
            clone = expand_clone_dir(clone_dir)
            _fetch_branch(self._run, clone, branch, self._timeout)
            done = _git(
                self._run,
                clone,
                ["rev-parse", f"refs/remotes/origin/{branch}"],
                self._timeout,
            )
        except (ValueError, RuntimeError) as exc:
            raise BuildContextRefUnreadableError(str(exc)) from exc
        sha = done.stdout.strip()
        if done.returncode != 0 or not _SHA.match(sha):
            raise BuildContextRefUnreadableError(
                f"origin/{branch} did not resolve to a commit: {done.stderr.strip()[-300:]}"
            )
        return sha


class ProvenanceLaneRevision:
    """The commit the running lane vendored, read through the deployment's probe command."""

    def __init__(
        self,
        probe_argv: Sequence[str],
        *,
        run: Runner = subprocess.run,
        timeout: float = LANE_PROBE_TIMEOUT_SECONDS,
        git_timeout: float = GIT_TIMEOUT_SECONDS,
    ) -> None:
        self._argv = list(probe_argv)
        self._run = run
        self._timeout = timeout
        self._git_timeout = git_timeout

    def vendored_revision(self, source_repo: str, section: str) -> str:
        try:
            done = self._run(
                self._argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LaneUnreadableError(
                f"lane probe did not run: {type(exc).__name__}: {exc}"
            ) from exc
        if done.returncode != 0:
            raise LaneUnreadableError(
                f"lane probe exited {done.returncode}: {done.stderr.strip()[-300:]}"
            )
        try:
            manifest = json.loads(done.stdout)
            sha = manifest["per_repo_vcs_provenance"][section][source_repo]["vcs_ref"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LaneUnreadableError(
                f"lane manifest has no vendored ref for {source_repo!r} ({type(exc).__name__})"
            ) from exc
        if not isinstance(sha, str) or not _SHA.match(sha):
            raise LaneUnreadableError(
                f"lane records {source_repo!r} at {sha!r}, not a 40-hex commit"
            )
        return sha

    def lane_carries(
        self, clone_dir: str, base: str, merge_sha: str, lane_sha: str
    ) -> bool:
        try:
            clone = expand_clone_dir(clone_dir)
            _ensure_commit(self._run, clone, base, lane_sha, self._git_timeout)
            done = _git(
                self._run,
                clone,
                ["merge-base", "--is-ancestor", merge_sha, lane_sha],
                self._git_timeout,
            )
        except (ValueError, RuntimeError) as exc:
            raise LaneUnreadableError(str(exc)) from exc
        if done.returncode == 0:
            return True
        if done.returncode == 1:
            return False
        raise LaneUnreadableError(
            f"merge-base exited {done.returncode}: {done.stderr.strip()[-300:]}"
        )


__all__ = [
    "DeployPathClassifier",
    "GitBuildContextRef",
    "GitMergeFiles",
    "ProvenanceLaneRevision",
    "expand_clone_dir",
]
