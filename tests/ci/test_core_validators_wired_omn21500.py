# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21500 T2.1: omnimarket consumes omnibase_core's validators by hook id.

The rules live in omnibase_core. This repository owns only the wiring: each
validator is a hook of the shared core hook repository at one dev-merge tag, and
the same hook ids run in CI as an unconditional job.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

CORE_URL = "https://github.com/OmniNode-ai/omnibase_core"
DEV_MERGE_TAG = re.compile(r"dev-\d{14}-[0-9a-f]{12}")
CI_JOB_ID = "core-validators"
CI_JOB_NAME = "Core Validators (OMN-21500)"

# The hook ids this repository wires from core for the ticket, each with the
# baseline file that holds the findings the tree had when it was wired (None: the
# tree has none).
WIRED_HOOKS: dict[str, str | None] = {
    "check-no-utcnow": None,
    "check-ai-slop": None,
    "check-hardcoded-model-config-compute": "config/hardcoded_model_config_baseline.yaml",
    "handler-di-gate": ".onex_ratchets/handler_di_baseline.txt",
    "check-protocol-binding": ".onex_ratchets/protocol_binding_baseline.txt",
}

# Hook ids the ticket names that are not wired here, each for a stated reason
# that a core change lifts. None of them has a local copy in this repository.
NOT_WIRED_HOOKS: dict[str, str] = {
    "check-no-raw-sqlite3": "no baseline support in the core hook; the tree holds findings",
    "check-no-direct-kafka-producer": "no baseline support in the core hook; the tree holds findings",
    "check-local-paths-compute": "no baseline support in the core hook; the tree holds findings",
    "spdx-headers-check": "no baseline support in the core hook; the tree holds findings",
    "check-no-dict-any": "baseline path is refused by canonical-file-shape: hook not in its gate list",
    "check-no-backward-compat": "baseline path is refused by canonical-file-shape: hook not in its gate list",
    "pydantic-extra-forbid": "baseline path is refused by canonical-file-shape: hook not in its gate list",
    "validate-single-class-per-file": "no hook id in the core hook repository",
    "detect-secrets": "no hook id in the core hook repository",
    "validate-union-usage": "no hook id in the core hook repository",
}


def _repos() -> list[dict[str, Any]]:
    loaded = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    repos = loaded["repos"]
    assert isinstance(repos, list)
    return repos


def _hooks_of(hook_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (str(repo["repo"]), str(repo.get("rev", "")), hook)
        for repo in _repos()
        for hook in repo["hooks"]
        if hook.get("id") == hook_id
    ]


def _ci_job() -> dict[str, Any]:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    job = jobs[CI_JOB_ID]
    assert isinstance(job, dict)
    return job


@pytest.mark.parametrize("hook_id", sorted(WIRED_HOOKS))
def test_wired_hook_resolves_to_omnibase_core_at_a_dev_merge_tag(hook_id: str) -> None:
    found = _hooks_of(hook_id)
    assert len(found) == 1, f"{hook_id}: expected one hook entry, found {len(found)}"
    url, rev, _ = found[0]
    assert url == CORE_URL, f"{hook_id} resolves to {url!r}, not {CORE_URL}"
    assert DEV_MERGE_TAG.fullmatch(rev), (
        f"{hook_id}: rev {rev!r} is not a dev-merge tag"
    )


def test_wired_hooks_share_one_tag() -> None:
    revs = {_hooks_of(hook_id)[0][1] for hook_id in WIRED_HOOKS}
    assert len(revs) == 1, f"core validators pinned at several tags: {sorted(revs)}"


@pytest.mark.parametrize("hook_id", sorted(NOT_WIRED_HOOKS))
def test_unwired_ticket_validator_has_no_local_copy(hook_id: str) -> None:
    assert NOT_WIRED_HOOKS[hook_id]
    for url, _, _ in _hooks_of(hook_id):
        assert url == CORE_URL, f"{hook_id} is a copy held at {url!r}"


def test_model_config_hook_declares_no_args_so_the_manifest_baseline_applies() -> None:
    _, _, hook = _hooks_of("check-hardcoded-model-config-compute")[0]
    assert "args" not in hook


@pytest.mark.parametrize(
    "hook_id", [h for h, baseline in sorted(WIRED_HOOKS.items()) if baseline]
)
def test_baselined_hook_names_a_committed_shrink_only_baseline(hook_id: str) -> None:
    baseline = WIRED_HOOKS[hook_id]
    assert baseline is not None
    _, _, hook = _hooks_of(hook_id)[0]
    args = [str(a) for a in hook.get("args", [])]
    if "args" in hook:
        assert args[args.index("--baseline") + 1] == baseline
    path = REPO_ROOT / baseline
    assert path.is_file(), f"{baseline} is not committed"
    text = path.read_text(encoding="utf-8")
    assert re.search(r"shrink|only leave|only shrinks", text, re.IGNORECASE), (
        f"{baseline} does not state the shrink-only rule"
    )


def test_baseline_entries_are_unique_per_file() -> None:
    for baseline in (
        ".onex_ratchets/handler_di_baseline.txt",
        ".onex_ratchets/protocol_binding_baseline.txt",
    ):
        lines = [
            line.split()
            for line in (REPO_ROOT / baseline).read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        paths = [parts[0] for parts in lines]
        assert paths, f"{baseline} holds no entries"
        assert len(paths) == len(set(paths)), f"{baseline} lists a file twice"
        assert all(len(parts) == 2 and parts[1].isdigit() for parts in lines)


def test_ci_job_runs_every_wired_hook_unconditionally_on_a_hosted_runner() -> None:
    job = _ci_job()
    assert job["name"] == CI_JOB_NAME
    assert job["runs-on"] == "ubuntu-latest"
    assert "needs" not in job
    assert "if" not in job
    runs = "\n".join(str(step.get("run", "")) for step in job["steps"])
    for hook_id in WIRED_HOOKS:
        assert hook_id in runs, f"ci.yml {CI_JOB_ID} does not run {hook_id}"
    assert "POSITIVE CONTROL FAILED" in runs


def test_ci_job_holds_no_tag_of_its_own() -> None:
    text = yaml.safe_dump(_ci_job())
    assert DEV_MERGE_TAG.search(text) is None, "the CI job names a core tag itself"
    assert "--config" not in text


def test_canonical_file_shape_hook_names_the_wired_tag() -> None:
    assert _hooks_of("canonical-file-shape")[0][1] == _hooks_of("check-no-utcnow")[0][1]
