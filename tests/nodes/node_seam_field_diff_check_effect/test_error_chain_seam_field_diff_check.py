# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a difference a change adds refuses it; one the base already had does not (OMN-24121).

Failing control: the same mismatch is judged when the head commit introduces it and is not judged when
the base revision already carried it. The base side is evaluated from the base revision's own source
(a git extract read in a subprocess), so a head commit that fixes or reshapes a model cannot hide a
difference the base had, and cannot inherit one it did not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.seam_field_diff.model_seam_field_diff_check import (
    ModelSeamFieldDiffCheckRequest,
    ModelSeamFieldDiffCheckResult,
)
from omnimarket.nodes.node_seam_field_diff_check_effect.handlers.handler_seam_field_diff_check import (
    HandlerSeamFieldDiffCheck,
)
from tests.nodes.node_seam_field_diff_check_effect.fixture_repo import (
    MISSING_FIELD_CONSUMER_FIELDS,
    commit,
    config,
    new_package_name,
    new_repo,
    write_package,
)

pytestmark = pytest.mark.unit


def _check(
    repo: Path, package: str, ref: str = "HEAD~1", floor: int = 1
) -> ModelSeamFieldDiffCheckResult:
    return HandlerSeamFieldDiffCheck(
        config_override=config(package, floor=floor)
    ).handle(ModelSeamFieldDiffCheckRequest(repo_root=str(repo), base_ref=ref))


def test_a_consumer_field_the_producer_lacks_refuses_the_change_that_adds_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = new_package_name()
    repo = new_repo(tmp_path, package)
    write_package(repo, package, consumer_fields=MISSING_FIELD_CONSUMER_FIELDS)
    commit(repo, "head adds a required consumer field")
    monkeypatch.syspath_prepend(str(repo / "src"))
    result = _check(repo, package)
    assert result.exit_code == 1
    assert result.status == "refused"
    (finding,) = result.new_findings
    assert finding.field == "label"
    assert finding.issue.value == "missing_field"
    assert any(
        "missing_field" in line and "field label" in line
        for line in result.output_lines
    )


def test_a_difference_the_base_already_had_is_not_judged_the_same_difference_added_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = new_package_name()
    repo = new_repo(tmp_path, package, consumer_fields=MISSING_FIELD_CONSUMER_FIELDS)
    (repo / "README.md").write_text("an unrelated change\n")
    commit(repo, "head changes nothing about the pair")
    monkeypatch.syspath_prepend(str(repo / "src"))
    already = _check(repo, package)
    assert already.exit_code == 0, already.output_lines
    assert already.new_findings == ()

    added_package = new_package_name()
    second = tmp_path / "second"
    second.mkdir()
    added_repo = new_repo(second, added_package)
    write_package(
        added_repo, added_package, consumer_fields=MISSING_FIELD_CONSUMER_FIELDS
    )
    commit(added_repo, "head introduces it")
    monkeypatch.syspath_prepend(str(added_repo / "src"))
    assert _check(added_repo, added_package).exit_code == 1


def test_a_difference_a_change_fixes_is_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = new_package_name()
    repo = new_repo(tmp_path, package, consumer_fields=MISSING_FIELD_CONSUMER_FIELDS)
    write_package(repo, package)
    commit(repo, "head fixes the consumer")
    monkeypatch.syspath_prepend(str(repo / "src"))
    assert _check(repo, package).exit_code == 0


def test_an_unknown_base_ref_is_unreadable_not_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = new_package_name()
    repo = new_repo(tmp_path, package)
    monkeypatch.syspath_prepend(str(repo / "src"))
    result = _check(repo, package, ref="no-such-revision")
    assert result.exit_code == 2
    assert result.status == "unreadable"


def test_a_scan_that_resolves_fewer_pairs_than_the_floor_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = new_package_name()
    repo = new_repo(tmp_path, package)
    (repo / "README.md").write_text("x\n")
    commit(repo, "head")
    monkeypatch.syspath_prepend(str(repo / "src"))
    result = _check(repo, package, floor=5)
    assert result.exit_code == 2
    assert "fewer than the 5" in result.output_lines[0]
