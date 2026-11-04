# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two ruled mechanical classes do not hide any other content change."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.occ_contract_pin import criterion_hash

from .reverse_substitution import (
    MECHANICAL_EXCEPTIONS,
    MOVED_TEST_FILES,
    EnumeratedChange,
    assert_reverse_substitution,
    check_manifest,
)

ORIGINAL = "def test_example():\n    # keep this evidence\n    assert left and right\n"


@pytest.mark.parametrize(
    "moved",
    [
        ORIGINAL,
        "def test_example( ):\n    # keep this evidence\n    assert (left and right)\n",
        "def test_example():\n    # keep this evidence\n    assert left\n    assert right\n",
    ],
)
def test_only_ruled_mechanical_changes_pass(moved: str) -> None:
    assert_reverse_substitution(ORIGINAL, moved)


@pytest.mark.parametrize(
    "moved",
    [
        ORIGINAL.replace("right", "wrong"),
        ORIGINAL.replace(" and ", " or "),
        ORIGINAL.replace("assert left and right", "assert right and left"),
        ORIGINAL.replace("assert left and right", "assert left"),
        ORIGINAL.replace(
            "assert left and right", 'assert left and right, "new message"'
        ),
        ORIGINAL.replace("keep this evidence", "changed evidence"),
        ORIGINAL.replace("    # keep this evidence\n", ""),
        ORIGINAL.replace("test_example", "test_other"),
        ORIGINAL + "\ndef test_added():\n    assert True\n",
        "",
    ],
)
def test_any_other_difference_is_refused(moved: str) -> None:
    with pytest.raises(
        AssertionError, match="unapproved reverse-substitution difference"
    ):
        assert_reverse_substitution(ORIGINAL, moved)


def test_pt018_negated_disjunction_split() -> None:
    assert_reverse_substitution(
        "assert not (left or right)\n", "assert not left\nassert not right\n"
    )


def test_other_lint_fixes_are_not_exceptions() -> None:
    with pytest.raises(AssertionError):
        assert_reverse_substitution("assert value == True\n", "assert value\n")


@pytest.mark.parametrize("kind", ["wiring", "assertion", "loader"])
def test_pre_enumerated_exact_edits_still_pass(kind: str) -> None:
    change = EnumeratedChange(kind, "left", "expected")  # type: ignore[arg-type]
    assert_reverse_substitution(
        ORIGINAL,
        ORIGINAL.replace("left", "expected"),
        enumerated_changes=(change,),
    )


def test_literal_reverse_substitution_precedes_comparison() -> None:
    assert_reverse_substitution(
        'assert host == "source-host"\n',
        'assert host == "fixture-host"\n',
        reverse_substitutions=(("fixture-host", "source-host"),),
    )


def test_unlisted_class_is_refused() -> None:
    change = EnumeratedChange("cleanup", "left", "expected")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unapproved enumerated class"):
        assert_reverse_substitution(ORIGINAL, ORIGINAL, enumerated_changes=(change,))


def test_exactly_the_two_ruled_exception_names() -> None:
    assert {
        "lint-required formatting",
        "PT018 assertion splits",
    } == MECHANICAL_EXCEPTIONS


@pytest.mark.parametrize("filename", MOVED_TEST_FILES)
def test_each_moved_file_keeps_content_under_formatting(filename: str) -> None:
    source = Path(__file__).with_name(filename).read_text(encoding="utf-8")
    assert_reverse_substitution(source, source)
    # A real file with a new, valid assertion must not pass as formatting.
    with pytest.raises(AssertionError):
        assert_reverse_substitution(source, source + "\nassert False\n")


def test_missing_historical_manifest_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANDING_PLACEMENT_REVERSE_SUBSTITUTION_MANIFEST", raising=False)
    with pytest.raises(ValueError, match="MANIFEST is required"):
        check_manifest()


@pytest.mark.parametrize("names", [[], [MOVED_TEST_FILES[0]] * 6])
def test_missing_or_duplicated_moved_files_refuse(
    names: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"filename": name} for name in names]))
    monkeypatch.setenv("LANDING_PLACEMENT_REVERSE_SUBSTITUTION_MANIFEST", str(manifest))
    with pytest.raises(ValueError, match="every moved test exactly once"):
        check_manifest()


def test_moved_tree_cannot_supply_its_own_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "filename": name,
                    "original_path": str(Path(__file__).with_name(name)),
                }
                for name in MOVED_TEST_FILES
            ]
        )
    )
    monkeypatch.setenv("LANDING_PLACEMENT_REVERSE_SUBSTITUTION_MANIFEST", str(manifest))
    with pytest.raises(ValueError, match="independent of the moved test tree"):
        check_manifest()


def test_criterion_binding_and_checker_change_together() -> None:
    root = Path(__file__).resolve().parents[3]
    contract = yaml.safe_load((root / "contracts/OMN-21408.yaml").read_text())
    criterion = next(
        item["statement"]
        for requirement in contract["requirements"]
        for item in requirement["acceptance"]
        if item["id"] == "AC2"
    )
    assert criterion == (
        "AC2: the reverse-substitution diff of every moved test file contains only "
        "the enumerated wiring, assertion and loader lines plus lint-required "
        "formatting and PT018 assertion splits as mechanical exceptions -- "
        "falsifier: any other added or changed line"
    )
    evidence = next(
        item
        for item in contract["dod_evidence"]
        if item["id"] == "dod-omn21408-ac2-reverse-substitution"
    )
    assert evidence["binds_ac"] == ["AC2"]
    assert evidence["ac_bindings"][0]["criterion_hash"] == criterion_hash(criterion)
    # The historical-file command must remain separate from the surrogate
    # controls, and must not silently pass when its inputs are unavailable.
    assert evidence["execution_scope"] == "local_done_gate"
    assert "check_manifest()" in evidence["checks"][0]["check_value"]
    hook = yaml.safe_load((root / ".pre-commit-config.yaml").read_text())
    entries = [item for repo in hook["repos"] for item in repo["hooks"]]
    entry = next(
        item
        for item in entries
        if item["id"] == "landing-placement-reverse-substitution"
    )["entry"]
    workflow = (root / ".github/workflows/ci.yml").read_text()
    assert f"run: {entry}" in workflow
