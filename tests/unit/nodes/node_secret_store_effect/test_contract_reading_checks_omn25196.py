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
import shutil
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
#: Both Lab-comment contracts carry the same check; each copy is run (OMN-25210).
_LAB_CONTRACTS = ["OMN-25181", "OMN-25196"]
#: What a hosted pull request merge checkout sets in the runner's environment.
_HOSTED_MERGE_REF = {"GITHUB_REF": "refs/pull/6297/merge"}

_FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
bodies = json.load(open(os.environ["FAKE_GH_COMMENTS"]))
if any("@json" in a for a in sys.argv[1:]):
    for body in bodies:
        # jq and gojq leave U+2028 and U+0085 unescaped in @json; so does this stand-in.
        print(json.dumps(body, ensure_ascii=False))
else:
    for body in bodies:
        print(body)
"""


#: The unbound local_done_gate item that carries each reading check (OMN-24611).
_READING_ITEMS = {
    ("OMN-24611", "AC3"): "dod-omn24611-ac3-live-reading",
    ("OMN-24611", "AC4"): "dod-omn24611-ac4-live-reading",
    ("OMN-25181", "AC4"): "dod-omn25181-ac4-binding",
    ("OMN-25196", "AC4"): "dod-omn25196-ac4-lab-comment",
}


def _check(contract: str, ac: str, marker: str) -> str:
    raw = yaml.safe_load(
        (_REPO_ROOT / "contracts" / f"{contract}.yaml").read_text(encoding="utf-8")
    )
    item_id = _READING_ITEMS[(contract, ac)]
    found = [
        check["check_value"]
        for item in raw["dod_evidence"]
        if item["id"] == item_id
        for check in item["checks"]
        if marker in check["check_value"]
    ]
    assert len(found) == 1, f"{contract} {ac}: one {marker} check expected"
    return str(found[0])


def _tool_path() -> str:
    """The directories of the host's own git and python3, then the usual ones."""
    dirs = [
        str(Path(found).parent)
        for found in (shutil.which("git"), shutil.which("python3"))
        if found
    ]
    return ":".join([*dict.fromkeys(dirs), "/usr/bin", "/bin", "/usr/local/bin"])


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
            "PATH": _tool_path(),
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


def _run(
    checkout: dict[str, Any],
    command: str,
    comments: list[str],
    extra_env: dict[str, str] | None = None,
) -> int:
    packet = checkout["tmp"] / "comments.json"
    packet.write_text(json.dumps(comments), encoding="utf-8")
    return subprocess.run(
        ["bash", "-c", command],
        cwd=checkout["repo"],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{checkout['bin']}:{_tool_path()}",
            "FAKE_GH_COMMENTS": str(packet),
            "HOME": str(checkout["tmp"]),
            **(extra_env or {}),
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


def _merge_checkout(checkout: dict[str, Any]) -> str:
    """Make HEAD a merge commit of main and a pr branch; return the pr branch tip."""
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
    return pr_head


def test_ac3_a_hosted_merge_ref_reads_the_pull_request_head(
    checkout: dict[str, Any],
) -> None:
    """On the hosted merge ref HEAD is the merge commit and the pull request head its second parent."""
    pr_head = _merge_checkout(checkout)
    command = _check("OMN-24611", "AC3", "gh api")
    both = [_key_reading(pr_head, "h051"), _key_reading(pr_head, "h201")]
    assert _run(checkout, command, both, _HOSTED_MERGE_REF) == 0
    at_base = [
        _key_reading(checkout["head"], "h051"),
        _key_reading(checkout["head"], "h201"),
    ]
    assert _run(checkout, command, at_base, _HOSTED_MERGE_REF) != 0


def test_ac3_merge_ref_a_local_merge_does_not_accept_the_second_parent(
    checkout: dict[str, Any],
) -> None:
    """A branch that merged dev locally is not a hosted merge ref: its second parent is dev's tip (OMN-25210)."""
    dev_tip = _merge_checkout(checkout)
    command = _check("OMN-24611", "AC3", "gh api")
    comments = [_key_reading(dev_tip, "h051"), _key_reading(dev_tip, "h201")]
    assert _run(checkout, command, comments) != 0
    assert _run(checkout, command, comments, {"GITHUB_REF": "refs/heads/main"}) != 0


def test_ac4_merge_ref_a_local_merge_does_not_accept_the_second_parent(
    checkout: dict[str, Any],
) -> None:
    dev_tip = _merge_checkout(checkout)
    command = _check("OMN-24611", "AC4", "gh api")
    assert _run(checkout, command, [_noop_reading(dev_tip)]) != 0
    assert _run(checkout, command, [_noop_reading(dev_tip)], _HOSTED_MERGE_REF) == 0


@pytest.mark.parametrize("contract", _LAB_CONTRACTS)
def test_lab_merge_ref_a_local_merge_does_not_accept_the_second_parent(
    checkout: dict[str, Any], contract: str
) -> None:
    dev_tip = _merge_checkout(checkout)
    command = _check(contract, "AC4", "gh api")
    assert _run(checkout, command, [_lab(dev_tip)]) != 0
    assert _run(checkout, command, [_lab(dev_tip)], _HOSTED_MERGE_REF) == 0


# --- CRLF: a reading line inside a multi-line comment with carriage returns -----------


def test_ac3_crlf_reading_lines_inside_a_multi_line_comment_match(
    checkout: dict[str, Any],
) -> None:
    head = checkout["head"]
    body = "\r\n".join(
        [
            "Readings from the hosts:",
            _key_reading(head, "h051"),
            _key_reading(head, "h201"),
            "done",
        ]
    )
    command = _check("OMN-24611", "AC3", "gh api")
    assert _run(checkout, command, [body]) == 0


def test_ac3_crlf_a_reading_at_an_older_head_still_fails(
    checkout: dict[str, Any],
) -> None:
    older = checkout["older"]
    body = "\r\n".join([_key_reading(older, "h051"), _key_reading(older, "h201")])
    command = _check("OMN-24611", "AC3", "gh api")
    assert _run(checkout, command, [body]) != 0


def test_ac4_crlf_the_no_op_reading_inside_a_multi_line_comment_matches(
    checkout: dict[str, Any],
) -> None:
    body = "\r\n".join(["Operator note", _noop_reading(checkout["head"]), "thanks"])
    command = _check("OMN-24611", "AC4", "gh api")
    assert _run(checkout, command, [body]) == 0


# --- U+2028 and U+0085 in a comment do not break the packet parse ----------------------


@pytest.mark.parametrize("separator", ["\u2028", "\u0085"], ids=["u2028", "u0085"])
@pytest.mark.parametrize("contract", _LAB_CONTRACTS)
def test_lab_check_u2028_or_u0085_in_one_comment_does_not_fail_the_packet(
    checkout: dict[str, Any], contract: str, separator: str
) -> None:
    """One unrelated comment holding the separator must not hide the Lab comment at HEAD."""
    command = _check(contract, "AC4", "gh api")
    comments = [f"first{separator}second", _lab(checkout["head"])]
    assert _run(checkout, command, comments) == 0


@pytest.mark.parametrize("contract", _LAB_CONTRACTS)
def test_lab_check_u2028_inside_the_lab_comment_itself_still_matches(
    checkout: dict[str, Any], contract: str
) -> None:
    command = _check(contract, "AC4", "gh api")
    comments = [
        _lab(
            checkout["head"],
            tail="Merge order:\u2028omnimarket 6297 first, then overlays#571.",
        )
    ]
    assert _run(checkout, command, comments) == 0


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


# --- AC4 of the OMN-25181 and OMN-25196 contracts: a Lab comment that names HEAD -------


@pytest.mark.parametrize("contract", _LAB_CONTRACTS)
def test_lab_check_passes_with_a_lab_comment_at_head_and_the_merge_order(
    checkout: dict[str, Any], contract: str
) -> None:
    command = _check(contract, "AC4", "gh api")
    assert _run(checkout, command, ["unrelated", _lab(checkout["head"])]) == 0


@pytest.mark.parametrize("contract", _LAB_CONTRACTS)
@pytest.mark.parametrize(
    "case",
    ["no-comment", "no-lab-comment", "older-head", "no-merge-order", "not-first"],
)
def test_lab_check_fails_without_a_lab_comment_that_names_head(
    checkout: dict[str, Any], contract: str, case: str
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
    command = _check(contract, "AC4", "gh api")
    assert _run(checkout, command, packets[case]) != 0


@pytest.mark.parametrize("contract", _LAB_CONTRACTS)
def test_lab_check_is_no_longer_a_grep_of_the_other_contract(contract: str) -> None:
    command = _check(contract, "AC4", "gh api")
    assert "contracts/OMN-24611.yaml" not in command
    assert "Lab: head=" in command
