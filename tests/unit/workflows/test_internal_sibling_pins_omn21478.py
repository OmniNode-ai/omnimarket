# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Internal sibling pins reuse the core guard and gate admission (OMN-21478)."""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CONTEXT = "internal-sibling-pins / Internal Sibling Pins"
REUSABLE = "OmniNode-ai/omnibase_core/.github/workflows/internal-sibling-pins.yml@"
pytestmark = pytest.mark.unit


def _relock() -> ModuleType:
    """The release relock decision module (OMN-26166), imported where used."""
    return importlib.import_module("scripts.ci.release_relock")


def test_workflow_calls_core_at_a_full_sha_and_is_required() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/internal-sibling-pins.yml").read_text()
    )
    assert set(workflow["jobs"]) == {"internal-sibling-pins"}
    caller = workflow["jobs"]["internal-sibling-pins"]
    assert set(caller) == {"uses", "with"}
    assert caller["uses"].startswith(REUSABLE)
    tag = caller["uses"].removeprefix(REUSABLE)
    match = re.fullmatch(r"dev-[0-9]{14}-([0-9a-f]{12})", tag)
    assert match
    core_ref = caller["with"]["omnibase-core-ref"]
    assert re.fullmatch(r"[0-9a-f]{40}", core_ref)
    assert core_ref.startswith(match.group(1))

    manifest = yaml.safe_load((ROOT / ".github/required-checks.yaml").read_text())
    rows = [row for row in manifest["gates"] if row["name"] == CONTEXT]
    assert len(rows) == 1
    assert rows[0] == {
        "name": CONTEXT,
        "mode": "REQUIRED",
        "producer_kind": "cross_repo",
        "caller_workflow": "internal-sibling-pins.yml",
        "caller_job": "internal-sibling-pins",
        "cross_repo_ref": caller["uses"],
        "skip_semantics": "never",
        "rationale": rows[0]["rationale"],
    }
    assert "OMN-21478" in rows[0]["rationale"]
    assert "2026-10-16" in rows[0]["rationale"]


@pytest.mark.parametrize(
    ("distribution", "repo"),
    [
        ("omnibase-core", "omnibase_core"),
        ("omnibase-spi", "omnibase_spi"),
        ("omninode-memory", "omnimemory"),
    ],
)
def test_pyproject_and_lock_pin_the_sibling_to_one_dev_merge_tag(
    distribution: str, repo: str
) -> None:
    sources = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"][
        "sources"
    ]
    source = sources[distribution]
    assert source["git"] == f"https://github.com/OmniNode-ai/{repo}.git"
    assert re.fullmatch(r"dev-[0-9]{14}-[0-9a-f]{12}", source["tag"])

    packages = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    (locked,) = [p for p in packages if p["name"] == distribution]
    if distribution == "omnibase-core" and _relock().is_release_commit(ROOT):
        # RULING 2026-11-20T11:37:47Z: a release commit locks the released wheel
        # while the pyproject pin stays the dev merge tag the check reads.
        assert "registry" in locked["source"]
        return
    assert locked["source"]["git"].startswith(source["git"] + "?tag=" + source["tag"])


_REGISTRY_LOCK = """version = 1

[[package]]
name = "omnibase-infra"
version = "0.38.79"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "omnibase-core"
version = "0.47.73"
source = { registry = "https://pypi.org/simple" }
"""


def test_internal_sibling_pins_registry_lock_is_refused_on_a_non_release_commit() -> (
    None
):
    findings = _relock().check_lock(_REGISTRY_LOCK, release_commit=False)
    assert {(f.code, f.package) for f in findings} == {
        ("registry_source_off_release", "omnibase-infra"),
        ("registry_source_off_release", "omnibase-core"),
    }


def test_internal_sibling_pins_the_same_lock_marked_release_is_accepted() -> None:
    assert _relock().check_lock(_REGISTRY_LOCK, release_commit=True) == []


def test_internal_sibling_pins_this_trees_lock_matches_its_commit_kind() -> None:
    relock = _relock()
    findings = relock.check_lock(
        (ROOT / "uv.lock").read_text(),
        release_commit=relock.is_release_commit(ROOT),
    )
    assert findings == []


def test_internal_sibling_pins_pyproject_keeps_the_dev_tag_on_a_release_commit() -> (
    None
):
    # The core check reads [tool.uv.sources] only; a release commit relocks
    # uv.lock and leaves these pins, so the check's finding codes for dev stand.
    sources = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"][
        "sources"
    ]
    for distribution in ("omnibase-infra", "omnibase-core"):
        assert re.fullmatch(r"dev-[0-9]{14}-[0-9a-f]{12}", sources[distribution]["tag"])
