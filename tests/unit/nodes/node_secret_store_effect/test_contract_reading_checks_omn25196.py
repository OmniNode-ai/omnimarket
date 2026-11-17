# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reading and Lab comment checks of the OMN-24611 and OMN-25181 contracts can fail (OMN-25196).

The checks read the pull request comments with ``gh api`` and compare the head a
comment names with HEAD of the checkout. Here the contract's own check text is
run through a shell with a stand-in ``gh`` that serves a fixed comment list (the
``--jq`` filter of the real call is reproduced: raw bodies, or one JSON string
per line for ``@json``), inside a throwaway git repository whose HEAD is known.
A packet with no comment, a reading from one host only, a reading at an older
head and a Lab comment at an older head or without the merge order must each fail.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_deploy_agent_key_check import key_check_value

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_STALE_HEAD = "d037540c300c8a1f2b3c4d5e6f708192a3b4c5d6"
_DIGEST = "a" * 64
_CHECK = "0123456789abcdef"

_FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
bodies = json.load(open(os.environ["FAKE_GH_COMMENTS"]))
if any("@json" in a for a in sys.argv[1:]):
    for body in bodies:
        print(json.dumps(body))
else:
    for body in bodies:
        print(body)
"""


def _check(contract: str, ac: str, marker: str) -> str:
    raw = yaml.safe_load(
        (_REPO_ROOT / "contracts" / f"{contract}.yaml").read_text(encoding="utf-8")
    )
    found = [
        check["check_value"]
        for item in raw["dod_evidence"]
        if ac in item.get("binds_ac", [])
        for check in item["checks"]
        if marker in check["check_value"]
    ]
    assert len(found) == 1, f"{contract} {ac}: one {marker} check expected"
    return str(found[0])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> dict[str, Any]:
    """A throwaway repository, its older commit and its HEAD, a stand-in gh on PATH."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("1", encoding="utf-8")
    _git(repo, "add", "a")
    _git(repo, "commit", "-q", "-m", "first")
    older = _git(repo, "rev-parse", "HEAD")
    (repo / "a").write_text("2", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "second")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    return {
        "repo": repo,
        "head": _git(repo, "rev-parse", "HEAD"),
        "older": older,
        "bin": bin_dir,
        "tmp": tmp_path,
    }


def _run(checkout: dict[str, Any], command: str, comments: list[str]) -> int:
    packet = checkout["tmp"] / "comments.json"
    packet.write_text(json.dumps(comments), encoding="utf-8")
    return subprocess.run(
        ["bash", "-c", command],
        cwd=checkout["repo"],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{checkout['bin']}:/usr/bin:/bin:/usr/local/bin",
            "FAKE_GH_COMMENTS": str(packet),
            "HOME": str(checkout["tmp"]),
        },
    ).returncode


def _key_reading(head: str, host: str, agent: str = _CHECK, store: str = _CHECK) -> str:
    return f"Reading: head={head} key check host={host} agent_check={agent} store_check={store}"


def _noop_reading(head: str, before: str = _DIGEST, after: str = _DIGEST) -> str:
    return (
        f"Reading: head={head} h051->h201 no-op accepted "
        f"unit_digest_before={before} unit_digest_after={after}"
    )


def _lab(
    head: str, tail: str = "Merge order: omnimarket 6297 first, then overlays#571."
) -> str:
    return f"Lab: head={head} host h053, 4 suites passed.\n{tail}"


# --- AC3 of the OMN-24611 contract: one equal reading per sending host, at HEAD -------


def test_ac3_passes_with_one_reading_per_host_at_head(checkout: dict[str, Any]) -> None:
    command = _check("OMN-24611", "AC3", "gh api")
    comments = [
        _key_reading(checkout["head"], "h051"),
        _key_reading(checkout["head"], "h201"),
    ]
    assert _run(checkout, command, comments) == 0


@pytest.mark.parametrize(
    "case",
    ["no-comment", "one-host-only", "older-head", "unequal", "short-check"],
)
def test_ac3_fails_without_one_equal_reading_per_host_at_head(
    checkout: dict[str, Any], case: str
) -> None:
    head, older = checkout["head"], checkout["older"]
    packets = {
        "no-comment": [],
        "one-host-only": [_key_reading(head, "h051"), _key_reading(head, "h051")],
        "older-head": [_key_reading(older, "h051"), _key_reading(older, "h201")],
        "unequal": [
            _key_reading(head, "h051"),
            _key_reading(head, "h201", store="fedcba9876543210"),
        ],
        "short-check": [
            _key_reading(
                head, "h051", agent="0123456789abcde", store="0123456789abcde"
            ),
            _key_reading(head, "h201"),
        ],
    }
    command = _check("OMN-24611", "AC3", "gh api")
    assert _run(checkout, command, packets[case]) != 0


def test_ac3_the_stale_head_of_the_second_review_fails(
    checkout: dict[str, Any],
) -> None:
    command = _check("OMN-24611", "AC3", "gh api")
    comments = [
        _key_reading(_STALE_HEAD, "h051"),
        _key_reading(_STALE_HEAD, "h201"),
    ]
    assert _run(checkout, command, comments) != 0


def test_ac3_a_hosted_merge_checkout_reads_the_pull_request_head(
    checkout: dict[str, Any],
) -> None:
    """On a merge checkout HEAD is the merge commit and the pull request head its second parent."""
    repo = checkout["repo"]
    _git(repo, "checkout", "-q", "-b", "pr", checkout["head"])
    (repo / "b").write_text("pr", encoding="utf-8")
    _git(repo, "add", "b")
    _git(repo, "commit", "-q", "-m", "pr work")
    pr_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (repo / "c").write_text("base", encoding="utf-8")
    _git(repo, "add", "c")
    _git(repo, "commit", "-q", "-m", "base moved")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    command = _check("OMN-24611", "AC3", "gh api")
    assert (
        _run(
            checkout,
            command,
            [_key_reading(pr_head, "h051"), _key_reading(pr_head, "h201")],
        )
        == 0
    )
    assert (
        _run(
            checkout,
            command,
            [
                _key_reading(checkout["head"], "h051"),
                _key_reading(checkout["head"], "h201"),
            ],
        )
        != 0
    )


def test_key_check_value_is_sixteen_lowercase_hex_characters() -> None:
    """The AC3 regex only matches when the check value is 16 hex characters."""
    for secret in ("a", "x" * 64, "dev-key"):
        assert re.fullmatch(r"[0-9a-f]{16}", key_check_value(secret))


# --- AC4 of the OMN-24611 contract: the signed no-op reading, at HEAD ------------------


def test_ac4_passes_with_the_reading_at_head(checkout: dict[str, Any]) -> None:
    command = _check("OMN-24611", "AC4", "gh api")
    assert _run(checkout, command, [_noop_reading(checkout["head"])]) == 0


@pytest.mark.parametrize("case", ["no-comment", "older-head", "digest-changed"])
def test_ac4_fails_without_the_reading_at_head(
    checkout: dict[str, Any], case: str
) -> None:
    packets = {
        "no-comment": [],
        "older-head": [_noop_reading(checkout["older"])],
        "digest-changed": [_noop_reading(checkout["head"], after="b" * 64)],
    }
    command = _check("OMN-24611", "AC4", "gh api")
    assert _run(checkout, command, packets[case]) != 0


# --- AC4 of the OMN-25181 contract: a Lab comment that names HEAD ----------------------


def test_lab_check_passes_with_a_lab_comment_at_head_and_the_merge_order(
    checkout: dict[str, Any],
) -> None:
    command = _check("OMN-25181", "AC4", "gh api")
    assert _run(checkout, command, ["unrelated", _lab(checkout["head"])]) == 0


@pytest.mark.parametrize(
    "case",
    ["no-comment", "no-lab-comment", "older-head", "no-merge-order", "not-first"],
)
def test_lab_check_fails_without_a_lab_comment_that_names_head(
    checkout: dict[str, Any], case: str
) -> None:
    head, older = checkout["head"], checkout["older"]
    packets = {
        "no-comment": [],
        "no-lab-comment": [
            "Reading: head=" + head + " h051->h201 no-op accepted",
            "contracts/OMN-24611.yaml deliver_key_to_env_file unit_digest_before",
        ],
        "older-head": [_lab(older), _lab(_STALE_HEAD)],
        "no-merge-order": [_lab(head, tail="Four suites passed.")],
        "not-first": [f"See the run.\n{_lab(head)}"],
    }
    command = _check("OMN-25181", "AC4", "gh api")
    assert _run(checkout, command, packets[case]) != 0


def test_lab_check_is_no_longer_a_grep_of_the_other_contract() -> None:
    command = _check("OMN-25181", "AC4", "gh api")
    assert "contracts/OMN-24611.yaml" not in command
    assert "Lab: head=" in command
