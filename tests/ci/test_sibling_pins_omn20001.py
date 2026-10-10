# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20001 - CI reads sibling repositories only at the version this repo pins.

A workflow that clones, checks out or calls a sibling at its live branch turns
this repository red when the sibling merges. Every sibling read here resolves
to a lock version, a release tag or a sha. The Event Registry Drift job is
owned by the sibling-pin-drift-check lane and is not scanned.
A workflow_call-only reusable hosted for other repos pins omnibase_infra by
full commit sha instead of the lock tag, because it runs in its caller's CI.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SIBLINGS = (
    "omnibase_core",
    "omnibase_compat",
    "omnibase_spi",
    "omnibase_infra",
    "omnimemory",
    "omniclaude",
    "omniintelligence",
    "onex_change_control",
)
OWNED_ELSEWHERE_JOBS = {("ci.yml", "event-registry-drift")}
LOCK_PACKAGE = {
    "omnibase_core": "omnibase-core",
    "omnibase_compat": "omnibase-compat",
    "omnibase_spi": "omnibase-spi",
    "omnibase_infra": "omnibase-infra",
    "omnimemory": "omninode-memory",
}
MOVING_REFS = {"", "main", "dev", "master", "HEAD"}

_CLONE = re.compile(
    r"git clone\b[^\n]*github\.com/OmniNode-ai/(?P<repo>[a-z_]+)\.git[^\n]*"
)
_USES = re.compile(r"^OmniNode-ai/(?P<repo>[a-z_]+)/[^@]+@(?P<ref>\S+)$")
_LOCK_EXPR = re.compile(
    r"""\$\(grep -A1 '\^name = "(?P<pkg>[a-z-]+)"\$' uv\.lock[^\n]*?/p'\)"""
)


def _hosted_reusables() -> set[str]:
    found = set()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text()) or {}
        triggers = doc.get(True, doc.get("on"))
        if isinstance(triggers, dict) and set(triggers) == {"workflow_call"}:
            found.add(path.name)
    return found


def _steps() -> list[tuple[str, str, dict[str, Any]]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            if (path.name, job_id) in OWNED_ELSEWHERE_JOBS or not isinstance(job, dict):
                continue
            if "uses" in job:
                found.append((path.name, job_id, {"uses": job["uses"]}))
            for step in job.get("steps") or []:
                found.append((path.name, job_id, step))
    return found


def _lock_version(package: str) -> str:
    lock = (REPO_ROOT / "uv.lock").read_text()
    match = re.search(
        rf'^name = "{re.escape(package)}"\nversion = "([^"]+)"', lock, re.M
    )
    assert match, f"{package} has no uv.lock entry"
    return match.group(1)


def sibling_reads(
    steps: list[tuple[str, str, dict[str, Any]]],
) -> list[tuple[str, str, str, str]]:
    """(workflow, job, repo, unpinned-reason or '') for every sibling read."""
    reads = []
    for wf, job, step in steps:
        uses = str(step.get("uses", ""))
        m = _USES.match(uses)
        if m and m["repo"] in SIBLINGS:
            bad = "moving ref" if m["ref"] in MOVING_REFS else ""
            reads.append((wf, job, m["repo"], bad))
        with_ = step.get("with") or {}
        repo = str(with_.get("repository", "")).removeprefix("OmniNode-ai/")
        if repo in SIBLINGS:
            ref = str(with_.get("ref", ""))
            reads.append((wf, job, repo, "moving ref" if ref in MOVING_REFS else ""))
        for line in str(step.get("run", "")).splitlines():
            c = _CLONE.search(line)
            if c and c["repo"] in SIBLINGS:
                pinned = "--branch " in line and not re.search(
                    r"--branch\s+(dev|main)\b", line
                )
                pinned = pinned or "checkout --detach" in line
                reads.append((wf, job, c["repo"], "" if pinned else "no pinned ref"))
    return reads


def test_scan_sees_sibling_reads() -> None:
    """Positive control: the scan finds the known sibling reads, so zero cannot pass."""
    reads = sibling_reads(_steps())
    assert len(reads) > 50
    # OMN-20885: the schema-compat job installs the locked omnibase_spi wheel and
    # clones no sibling, so no workflow reads omnibase_spi from git any more.
    assert {r[2] for r in reads} >= set(SIBLINGS) - {"omnibase_spi"}


def test_every_sibling_read_is_pinned() -> None:
    unpinned = [r for r in sibling_reads(_steps()) if r[3]]
    assert not unpinned, f"sibling read at a moving ref: {unpinned}"


def test_scan_flags_a_live_branch_read() -> None:
    """Falsifier: the old shapes are reported as unpinned."""
    bad: list[tuple[str, str, dict[str, Any]]] = [
        (
            "w.yml",
            "j",
            {
                "run": "git clone --depth=1 https://github.com/OmniNode-ai/omniclaude.git ../omniclaude"
            },
        ),
        (
            "w.yml",
            "j",
            {
                "uses": "actions/checkout@v4",
                "with": {"repository": "OmniNode-ai/omnibase_infra", "ref": "dev"},
            },
        ),
        (
            "w.yml",
            "j",
            {
                "uses": "OmniNode-ai/omnibase_core/.github/workflows/validate-docs.yml@main"
            },
        ),
        (
            "w.yml",
            "j",
            {
                "run": "git clone --depth=1 --branch dev https://github.com/OmniNode-ai/omnibase_core.git ../omnibase_core"
            },
        ),
    ]
    assert [r[3] for r in sibling_reads(bad)] == [
        "no pinned ref",
        "moving ref",
        "moving ref",
        "no pinned ref",
    ]


def test_lock_derived_clones_read_a_package_the_lock_has() -> None:
    expected = set(LOCK_PACKAGE.values())
    used = set()
    for _, _, step in _steps():
        for m in _LOCK_EXPR.finditer(str(step.get("run", ""))):
            used.add(m["pkg"])
            _lock_version(m["pkg"])
    assert used
    assert used <= expected, used


def test_lock_expression_resolves_to_the_lock_version() -> None:
    """Dry run: the exact shell expression the workflows run prints the lock version."""
    cmd = next(
        m.group(0)
        for _, _, s in _steps()
        for m in _LOCK_EXPR.finditer(str(s.get("run", "")))
        if m["pkg"] == "omnibase-core"
    )
    done = subprocess.run(
        ["bash", "-c", f"printf %s {cmd}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == _lock_version("omnibase-core")


def test_infra_checkout_pins_match_lock(tmp_path: Path) -> None:
    hosted = _hosted_reusables()
    pins = {
        str((s.get("with") or {}).get("ref"))
        for wf, _, s in _steps()
        if wf not in hosted
        and str((s.get("with") or {}).get("repository")) == "OmniNode-ai/omnibase_infra"
    }
    assert pins == {
        f"v{_lock_version('omnibase-infra')}",
        "v${{ steps.pins.outputs.infra_version }}",
    }
    pin_step = next(
        s
        for wf, job, s in _steps()
        if wf == "ci.yml" and job == "sibling-release-compat" and s.get("id") == "pins"
    )
    output = tmp_path / "pins"
    subprocess.run(
        ["bash", "-c", pin_step["run"]],
        cwd=REPO_ROOT,
        env={"GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert output.read_text().splitlines() == [
        f"infra_version={_lock_version('omnibase-infra')}",
        f"core_version={_lock_version('omnibase-core')}",
    ]


def test_hosted_reusable_infra_checkouts_pin_a_full_sha() -> None:
    hosted = _hosted_reusables()
    pins = [
        (wf, str((s.get("with") or {}).get("ref", "")))
        for wf, _, s in _steps()
        if wf in hosted
        and str((s.get("with") or {}).get("repository")) == "OmniNode-ai/omnibase_infra"
    ]
    assert pins, "hosted reusables have no omnibase_infra checkouts"
    for wf, ref in pins:
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            f"{wf} omnibase_infra checkout must pin a full commit sha: {ref}"
        )


def test_hosted_reusable_classification() -> None:
    hosted = _hosted_reusables()
    assert "route-runner-reusable.yml" in hosted
    assert "ci.yml" not in hosted
