# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: this repository runs omnibase_core's check-action-pins.

The rule (third-party refs pinned to a full sha, first-party refs a dev-merge tag)
lives in omnibase_core; its tests cover each ref class. These tests cover what
belongs to this repository: the overlay carries the rule values, the pre-commit
hook and the CI step name the same core dev-merge tag, and no workflow ref of an
organisation the overlay names is a sha unless the overlay lists it with a reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAY = REPO_ROOT / ".action-pin-overlay.yaml"
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CORE_URL = "https://github.com/OmniNode-ai/omnibase_core"
_USES = re.compile(r"""^\s*(?:-\s*)?uses:\s*["']?([^"'\s]+)""")
_SHA = re.compile(r"[0-9a-fA-F]{40}")


def _overlay() -> dict[str, object]:
    loaded = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _hook_rev() -> str:
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    revs = [
        repo["rev"]
        for repo in config["repos"]
        if repo.get("repo") == CORE_URL
        and any(hook.get("id") == "check-action-pins" for hook in repo["hooks"])
    ]
    assert len(revs) == 1, "exactly one core repo block carries check-action-pins"
    return str(revs[0])


def test_overlay_carries_the_rule_values() -> None:
    overlay = _overlay()
    assert overlay["first_party_orgs"] == ["OmniNode-ai"]
    assert re.compile(str(overlay["tag_pattern"]))
    assert overlay["first_party_main"] == "fail"


def test_hook_and_ci_name_the_same_core_dev_merge_tag() -> None:
    pattern = re.compile(str(_overlay()["tag_pattern"]))
    rev = _hook_rev()
    assert pattern.fullmatch(rev), f"hook rev {rev!r} is not a dev-merge tag"
    step = re.search(
        rf"ACTION_PINS_CORE: \"omnibase-core @ git\+{re.escape(CORE_URL)}@(\S+)\"",
        CI.read_text(encoding="utf-8"),
    )
    assert step is not None, "ci.yml has no ACTION_PINS_CORE env"
    assert step.group(1) == rev


def test_every_known_exception_is_a_sha_with_a_reason() -> None:
    exceptions = _overlay()["known_exceptions"]
    assert isinstance(exceptions, list)
    for entry in exceptions:
        assert isinstance(entry, dict)
        assert _SHA.fullmatch(str(entry["uses"]).rpartition("@")[2])
        assert len(str(entry["reason"]).strip()) >= 10


def test_first_party_refs_are_tags_or_listed_exceptions() -> None:
    overlay = _overlay()
    pattern = re.compile(str(overlay["tag_pattern"]))
    exceptions = overlay["known_exceptions"]
    assert isinstance(exceptions, list)
    listed = {str(entry["uses"]) for entry in exceptions}
    offenders: list[str] = []
    for workflow in sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml")):
        for number, line in enumerate(workflow.read_text("utf-8").splitlines(), 1):
            match = _USES.match(line)
            if match is None or not match.group(1).startswith("OmniNode-ai/"):
                continue
            value = match.group(1)
            if value not in listed and not pattern.fullmatch(value.rpartition("@")[2]):
                offenders.append(f"{workflow.name}:{number}: {value}")
    assert offenders == []
