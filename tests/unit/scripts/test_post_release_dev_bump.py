# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit proof for the post-release dev version bump (OMN-18010, porting OMN-13912).

The defect being pinned is not "a helper miscomputes a patch number". It is a
*sequencing* defect in the release train: publishing X.Y.Z from dev HEAD arms the
release-identity gate (omnimarket's own ``scripts/check_release_identity.py``,
OMN-16344) against dev itself, and nothing in the train disarms it. Two measured
windows, in the omnibase_infra series this helper is ported from:

* v0.38.10 tagged 2026-08-26T01:38:23Z; dev sat at 0.38.10 until an unrelated PR
  bumped it 2026-08-26T03:44:31Z — ~2h06m armed.
* v0.38.11 tagged 2026-08-28T00:49:31Z; dev sat at 0.38.11 until an unrelated PR
  bumped it 2026-08-28T02:27:16Z — ~1h38m armed.

Under release-on-merge (OMN-18010) that window would recur on EVERY merge instead
of once per hand-cut release, which is what makes the port load-bearing here.

Properties under test, each a leg of that failure:

  * **armed case**   -- dev == published is a BUMP, not a shrug
  * **behind case**  -- dev < published bumps to published+1, never to dev+1
  * **idempotent**   -- dev already ahead is a NOOP, so a re-run does not open a
                        second bump PR
  * **final-only**   -- an rc/pre-release version is refused, never patch-bumped
  * **narrow write** -- only ``[project].version`` moves; a ``version`` key under
                        any other table is byte-identical afterwards
  * **--set mode**   -- OMN-18010's addition: put dev at an EXACT version, and
                        never move dev backwards
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.post_release_dev_bump import (
    ACTION_BUMP,
    ACTION_NOOP,
    BumpConfigError,
    apply_decision,
    decide,
    decide_target,
    next_patch,
    parse_final_version,
    read_project_version,
    rewrite_project_version,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "post_release_dev_bump.py"

# A pyproject shaped like this repo's: [project].version is the one that moves,
# and there is a decoy `version` under a later table that must not.
PYPROJECT_TEMPLATE = """\
[build-system]
requires = ["hatchling"]

[project]
name = "omnimarket"
version = "{version}"
requires-python = ">=3.12"

[tool.some-vendor]
version = "9.9.9"
"""


def _write_pyproject(tmp_path: Path, version: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT_TEMPLATE.format(version=version), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Version parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.4.19", (0, 4, 19)), ("v0.4.19", (0, 4, 19)), (" v1.0.0 ", (1, 0, 0))],
)
def test_parse_accepts_final_versions_with_or_without_v(
    raw: str, expected: tuple[int, int, int]
) -> None:
    assert parse_final_version(raw, label="version") == expected


@pytest.mark.parametrize(
    "raw", ["", "0.4", "0.4.19rc1", "v0.4.19-rc.1", "0.4.19.post1", "latest"]
)
def test_parse_refuses_non_final_versions(raw: str) -> None:
    # An rc must never drive a dev bump: it would consume a real patch number
    # and compare against a version the release-identity gate does not publish.
    with pytest.raises(BumpConfigError):
        parse_final_version(raw, label="released version")


def test_next_patch_increments_only_the_patch_component() -> None:
    assert next_patch("v0.4.19") == "0.4.20"
    # The lexical trap: 0.4.9 -> 0.4.10, not 0.4.91 and not 0.5.0.
    assert next_patch("0.4.9") == "0.4.10"


# ---------------------------------------------------------------------------
# The --released decision
# ---------------------------------------------------------------------------


def test_armed_case_dev_equals_published_is_a_bump() -> None:
    decision = decide(dev_version="0.4.19", released_version="v0.4.19")
    assert decision.action == ACTION_BUMP
    assert decision.target_version == "0.4.20"
    assert "ARMED" in decision.reason


def test_behind_case_bumps_past_published_not_past_dev() -> None:
    # A release cut from somewhere other than dev HEAD leaves dev BEHIND the
    # published version. Bumping dev+1 would still be <= published and would
    # leave the gate armed; the target must be published+1.
    decision = decide(dev_version="0.4.17", released_version="v0.4.19")
    assert decision.action == ACTION_BUMP
    assert decision.target_version == "0.4.20"


def test_already_ahead_is_a_noop_so_the_step_is_idempotent() -> None:
    decision = decide(dev_version="0.4.20", released_version="v0.4.19")
    assert decision.action == ACTION_NOOP
    assert decision.target_version == "0.4.20"


def test_decision_target_satisfies_the_release_identity_invariant() -> None:
    # The gate's own rule: dev must be STRICTLY greater than the highest
    # published version. Prove the chosen target actually clears it.
    released = "v0.4.19"
    for dev in ("0.4.17", "0.4.19"):
        decision = decide(dev_version=dev, released_version=released)
        assert parse_final_version(
            decision.target_version, label="target"
        ) > parse_final_version(released, label="released")


# ---------------------------------------------------------------------------
# The --set decision (OMN-18010)
# ---------------------------------------------------------------------------


def test_set_moves_dev_up_to_the_requested_version() -> None:
    decision = decide_target(dev_version="0.4.18", target_version="0.4.19")
    assert decision.action == ACTION_BUMP
    assert decision.target_version == "0.4.19"


def test_set_is_a_noop_when_dev_already_states_the_target() -> None:
    # arm-dev must converge on a re-run of the same drifted merge rather than
    # opening a second bump PR.
    decision = decide_target(dev_version="0.4.19", target_version="0.4.19")
    assert decision.action == ACTION_NOOP


def test_set_never_moves_dev_backwards() -> None:
    # Lowering dev would RE-ARM the release-identity gate this flow exists to
    # keep disarmed, so a dev version above the target is a noop, not a rewrite.
    decision = decide_target(dev_version="0.4.25", target_version="0.4.19")
    assert decision.action == ACTION_NOOP
    assert decision.target_version == "0.4.25"


@pytest.mark.parametrize("target", ["0.4.19rc1", "0.4", "", "latest"])
def test_set_refuses_a_non_final_target(target: str) -> None:
    with pytest.raises(BumpConfigError):
        decide_target(dev_version="0.4.18", target_version=target)


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def test_rewrite_touches_only_the_project_table(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, "0.4.19")
    updated = rewrite_project_version(path.read_text(encoding="utf-8"), "0.4.20")
    assert 'version = "0.4.20"' in updated
    # The decoy under [tool.some-vendor] is untouched.
    assert 'version = "9.9.9"' in updated
    assert updated.count('version = "0.4.20"') == 1


def test_apply_bumps_the_file_and_noop_leaves_it_byte_identical(
    tmp_path: Path,
) -> None:
    path = _write_pyproject(tmp_path, "0.4.19")
    before = path.read_text(encoding="utf-8")

    bump = decide(read_project_version(path), "v0.4.19")
    assert apply_decision(path, bump) is True
    assert read_project_version(path) == "0.4.20"

    after_first = path.read_text(encoding="utf-8")
    noop = decide(read_project_version(path), "v0.4.19")
    assert noop.action == ACTION_NOOP
    assert apply_decision(path, noop) is False
    assert path.read_text(encoding="utf-8") == after_first
    assert after_first != before


def test_rewrite_refuses_a_pyproject_with_no_project_version() -> None:
    with pytest.raises(BumpConfigError):
        rewrite_project_version('[project]\nname = "x"\n', "0.4.20")


def test_read_refuses_a_pyproject_with_no_project_version(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(BumpConfigError):
        read_project_version(path)


# ---------------------------------------------------------------------------
# The CLI the release-on-merge jobs actually invoke
# ---------------------------------------------------------------------------


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_emits_a_json_decision_and_applies_the_bump(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, "0.4.19")
    result = _run(["--released", "v0.4.19", "--pyproject", str(path), "--apply"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == ACTION_BUMP
    assert payload["target_version"] == "0.4.20"
    assert payload["applied"] is True
    assert read_project_version(path) == "0.4.20"


def test_cli_without_apply_decides_but_does_not_write(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, "0.4.19")
    result = _run(["--released", "v0.4.19", "--pyproject", str(path)])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == ACTION_BUMP
    assert payload["applied"] is False
    assert read_project_version(path) == "0.4.19"


def test_cli_set_mode_writes_the_exact_version(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, "0.4.18")
    result = _run(["--set", "0.4.19", "--pyproject", str(path), "--apply"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == ACTION_BUMP
    assert payload["target_version"] == "0.4.19"
    assert read_project_version(path) == "0.4.19"


def test_cli_requires_exactly_one_mode(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, "0.4.18")
    neither = _run(["--pyproject", str(path)])
    assert neither.returncode == 2
    both = _run(["--released", "v0.4.18", "--set", "0.4.19", "--pyproject", str(path)])
    assert both.returncode == 2


def test_cli_exits_2_on_a_prerelease_tag(tmp_path: Path) -> None:
    path = _write_pyproject(tmp_path, "0.4.19")
    result = _run(["--released", "v0.5.0rc1", "--pyproject", str(path), "--apply"])
    assert result.returncode == 2
    assert "final X.Y.Z" in result.stderr
    assert read_project_version(path) == "0.4.19"
