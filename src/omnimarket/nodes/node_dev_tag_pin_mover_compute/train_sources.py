# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effects of the dev-tag pin train's night, as plain functions (OMN-21895).

These are the ports ``HandlerDevTagPinTrain`` is handed: reading the siblings'
dev tags and CI, locking the candidate in a scratch copy of the consumer, and
pushing the one fixed branch. Nothing here chooses a tag; the choice is the
pure ``HandlerDevTagPinSet``.

``gather`` reads the same things omniclaude's ``relock_git_sources.py
--dev-tag-train`` reads (dev tags newest first, the commit's check runs), and
in addition what each candidate tag's own pyproject.toml pins for the other
siblings. omnimarket cannot import omniclaude's script, so the three small
readers are spelled here; omniclaude's copy can shrink onto them later.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_pin_set import (
    ModelPinSetCandidate,
    ModelPinSetRequest,
    ModelPinSetSibling,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_pin_train import (
    ModelResolveOutcome,
)

GreenOracle = Callable[[str, str], "bool | None"]

_DEV_TAG = re.compile(r"^dev-(?P<stamp>\d{14})-(?P<sha12>[0-9a-f]{12})$")
_OMNINODE_GIT = re.compile(
    r"^https://github\.com/OmniNode-ai/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
_URL_LINE = re.compile(r"^\s*-\s+(?P<url>https?://\S+)\s*$", re.MULTILINE)
# Tags examined per sibling, newest first: bounds the CI reads a night costs.
MAX_CANDIDATES = 15
_GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})


def _env() -> dict[str, str]:
    return scrub_git_location_env(os.environ)


def _git(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_env(),
        timeout=120,
    )


def stamp(tag: str) -> str:
    match = _DEV_TAG.match(tag)
    return match.group("stamp") if match else ""


def dev_tag_sources(pyproject_text: str) -> dict[str, tuple[str, str, str]]:
    """distribution -> (repo, url, tag) for each OmniNode-ai dev-tag git source."""
    sources = (
        tomllib.loads(pyproject_text).get("tool", {}).get("uv", {}).get("sources", {})
    )
    pins: dict[str, tuple[str, str, str]] = {}
    for distribution, attrs in sources.items():
        if not isinstance(attrs, dict) or set(attrs) & {"rev", "branch"}:
            continue
        url, tag = attrs.get("git"), attrs.get("tag")
        if not isinstance(url, str) or not isinstance(tag, str) or not stamp(tag):
            continue
        match = _OMNINODE_GIT.match(url)
        if match is not None:
            pins[distribution] = (match.group("repo"), url, tag)
    return pins


def dev_tags_newest_first(clone: Path) -> list[tuple[str, str]]:
    """(tag, commit) for every dev merge tag in ``clone``, newest stamp first."""
    result = _git(
        clone,
        "for-each-ref",
        "--format=%(refname:strip=2) %(*objectname) %(objectname)",
        "refs/tags/dev-*",
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot list dev tags in {clone}: {result.stderr.strip()}")
    tags: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and stamp(parts[0]):
            tags.append((parts[0], parts[1] if len(parts) == 3 else parts[-1]))
    return sorted(tags, key=lambda t: (stamp(t[0]), t[0]), reverse=True)


def check_runs_verdict(runs: list[tuple[str, str]]) -> bool | None:
    """Green, red, or None (unread) from (status, conclusion) pairs of one commit."""
    if not runs:
        return None
    if any(c and c not in _GREEN_CONCLUSIONS for _, c in runs):
        return False
    if any(status != "completed" for status, _ in runs):
        return None
    return True


def github_check_runs_green(repo: str, sha: str) -> bool | None:
    """The workflow's oracle: every check run on the dev commit, read with gh."""
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/OmniNode-ai/{repo}/commits/{sha}/check-runs?per_page=100",
            "--jq",
            r'.check_runs[] | "\(.status) \(.conclusion // "")"',
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        return None
    runs = []
    for line in result.stdout.splitlines():
        status, _, conclusion = line.partition(" ")
        runs.append((status, conclusion.strip()))
    return check_runs_verdict(runs)


def assume_unread(repo: str, sha: str) -> bool | None:
    return None


def _requires_at(clone: Path, ref: str, distributions: set[str]) -> dict[str, str]:
    """The dev tags the pyproject.toml at ``ref`` pins for ``distributions``."""
    shown = _git(clone, "show", f"{ref}:pyproject.toml")
    if shown.returncode != 0:
        return {}
    pins = dev_tag_sources(shown.stdout)
    return {d: pins[d][2] for d in distributions if d in pins}


def make_gather(
    *, consumer: str, registry_root: Path, is_green: GreenOracle
) -> Callable[[str], ModelPinSetRequest]:
    """Port: the consumer's pyproject.toml text -> every sibling and its candidates."""

    def gather(pyproject_text: str) -> ModelPinSetRequest:
        pins = dev_tag_sources(pyproject_text)
        clones: dict[str, Path] = {}
        tags_of: dict[str, list[tuple[str, str]]] = {}
        candidates: dict[str, list[ModelPinSetCandidate]] = {}
        for distribution, (repo, _url, current) in sorted(pins.items()):
            clone = registry_root / repo
            if not (clone / ".git").exists():
                raise RuntimeError(
                    f"{distribution}: no clone of {repo!r} under {registry_root}; "
                    "a train that examined no clone is indistinguishable from one "
                    "with nothing to move"
                )
            clones[distribution] = clone
            others = set(pins) - {distribution}
            tags_of[distribution] = dev_tags_newest_first(clone)
            candidates[distribution] = []
            for tag, sha in tags_of[distribution]:
                if (
                    stamp(tag) <= stamp(current)
                    or len(candidates[distribution]) >= MAX_CANDIDATES
                ):
                    break
                candidates[distribution].append(
                    ModelPinSetCandidate(
                        tag=tag,
                        green=is_green(pins[distribution][0], sha),
                        requires=_requires_at(clone, tag, others),
                    )
                )
        # A tag another sibling's candidate names is a candidate too, however old
        # it is: omnibase_infra's newest tag may name a core tag past the window.
        for distribution, (repo, _url, current) in sorted(pins.items()):
            have = {c.tag for c in candidates[distribution]}
            commit_of = dict(tags_of[distribution])
            others = set(pins) - {distribution}
            named = {
                c.requires[distribution]
                for other, listed in candidates.items()
                if other != distribution
                for c in listed
                if distribution in c.requires
            }
            for tag in sorted(named - have, key=stamp, reverse=True):
                if stamp(tag) > stamp(current) and tag in commit_of:
                    candidates[distribution].append(
                        ModelPinSetCandidate(
                            tag=tag,
                            green=is_green(repo, commit_of[tag]),
                            requires=_requires_at(clones[distribution], tag, others),
                        )
                    )
        siblings: list[ModelPinSetSibling] = []
        for distribution, (_repo, url, current) in sorted(pins.items()):
            others = set(pins) - {distribution}
            commit_of = dict(tags_of[distribution])
            siblings.append(
                ModelPinSetSibling(
                    distribution=distribution,
                    repository_url=url,
                    current_tag=current,
                    current_requires=_requires_at(
                        clones[distribution], commit_of.get(current, current), others
                    ),
                    candidates=tuple(candidates[distribution]),
                )
            )
        return ModelPinSetRequest(consumer=consumer, siblings=tuple(siblings))

    return gather


def make_scratch_resolve(
    repo_root: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Callable[[str], ModelResolveOutcome]:
    """Port: ``uv lock`` on the candidate pyproject.toml in a scratch copy.

    The scratch copy holds the consumer's own pyproject.toml (candidate text),
    its uv.lock and the files the project metadata names, so uv reads the same
    ``[tool.uv.sources]`` the Sibling Release Compatibility Gate locks with.
    The consumer's tree is never touched.
    """

    def resolve(candidate_text: str) -> ModelResolveOutcome:
        with tempfile.TemporaryDirectory(prefix="pin-train-resolve-") as scratch_dir:
            scratch = Path(scratch_dir)
            for name in ("uv.lock", "README.md", "LICENSE", ".python-version"):
                source = repo_root / name
                if source.is_file():
                    shutil.copy2(source, scratch / name)
            (scratch / "pyproject.toml").write_text(candidate_text, encoding="utf-8")
            result = run(
                ["uv", "lock"],
                cwd=scratch,
                capture_output=True,
                text=True,
                check=False,
                timeout=900,
            )
            if result.returncode != 0:
                return ModelResolveOutcome(
                    ok=False,
                    stderr=result.stderr,
                    conflicting_urls=tuple(
                        m.group("url") for m in _URL_LINE.finditer(result.stderr)
                    ),
                )
            return ModelResolveOutcome(
                ok=True,
                lock_text=(scratch / "uv.lock").read_text(encoding="utf-8"),
            )

    return resolve


def make_publish(
    repo_root: Path,
    *,
    branch: str,
    base: str,
    title: str,
    commit_message: str,
    trailers: tuple[str, ...] = (),
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Callable[[str, str, str], str]:
    """Port: write the candidate, push the one fixed branch, open or update the PR.

    One fixed branch is rebuilt from this run's base and force-pushed, and an
    open PR on it has its body rewritten instead of a second PR being opened,
    so a night is one bump per consumer however often it runs.
    """

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=True,
            env=_env(),
            timeout=300,
        )

    def gh(*args: str) -> subprocess.CompletedProcess[str]:
        return run(
            ["gh", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )

    def publish(pyproject_text: str, lock_text: str, body: str) -> str:
        (repo_root / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
        (repo_root / "uv.lock").write_text(lock_text, encoding="utf-8")
        body_file = repo_root.parent / f"{repo_root.name}.pin-train-body.md"
        body_file.write_text(body, encoding="utf-8")
        git("checkout", "-B", branch)
        git("add", "pyproject.toml", "uv.lock")
        commit = ["commit", "-m", commit_message]
        for trailer in trailers:
            commit += ["--trailer", trailer]
        git(*commit)
        git("push", "--force", "origin", branch)
        listed = gh(
            "pr",
            "list",
            "--head",
            branch,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            ".[0].number // empty",
        ).stdout.strip()
        if listed:
            gh("pr", "edit", listed, "--body-file", str(body_file))
            return f"updated #{listed}"
        gh(
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            str(body_file),
        )
        return "created"

    return publish
