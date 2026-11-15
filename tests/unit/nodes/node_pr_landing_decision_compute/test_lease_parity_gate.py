# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests of the landing-decision parity gate (OMN-24484).

Covers the pure verdict rules in ``lease_parity`` -- captured ids must match,
ids added to a captured file must fail, ids of files the capture never saw are
not parity subjects -- the mode default, and the integrity of the golden
capture itself: a golden for a deleted file would hide a removal.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

import pytest

from tests.unit.nodes.node_pr_landing_decision_compute import conftest, lease_parity
from tests.unit.nodes.node_pr_landing_decision_compute.lease_parity import (
    GOLDEN_PATH,
    PARITY_ENV,
    parity_mode,
    parity_verdict,
)

REPO_ROOT: Final = GOLDEN_PATH.parents[3]
GOLDEN_IDS: Final = 506
GOLDEN_FILES: Final = 19


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _call(sha256: str) -> dict[str, Any]:
    return {"sha256": sha256}


def _golden() -> dict[str, list[dict[str, Any]]]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_matching_calls_match_the_capture() -> None:
    golden = {
        "tests/unit/x.py::test_a": [_call(_sha("first")), _call(_sha("second"))],
    }
    calls = [_call(_sha("first")), _call(_sha("second"))]
    assert parity_verdict("tests/unit/x.py::test_a", calls, golden) is None


@pytest.mark.unit
def test_a_captured_id_matches_its_own_capture() -> None:
    golden = _golden()
    nodeid, shas = next(
        (nodeid, [one["sha256"] for one in entries])
        for nodeid, entries in sorted(golden.items())
        if len(entries) >= 2
    )
    calls = [_call(sha) for sha in shas]
    assert parity_verdict(nodeid, calls, golden) is None


@pytest.mark.unit
def test_a_captured_id_with_no_calls_matches_an_empty_capture() -> None:
    golden = _golden()
    nodeid = next(nodeid for nodeid, one in sorted(golden.items()) if not one)
    assert parity_verdict(nodeid, [], golden) is None


@pytest.mark.unit
def test_a_call_against_an_empty_capture_fails() -> None:
    nodeid = "tests/unit/x.py::test_a"
    golden = {nodeid: []}
    verdict = parity_verdict(nodeid, [_call(_sha("first"))], golden)
    assert verdict is not None
    assert nodeid in verdict


@pytest.mark.unit
def test_a_changed_sha_fails_and_names_the_nodeid() -> None:
    nodeid = "tests/unit/x.py::test_a"
    golden = {nodeid: [_call(_sha("before the lift"))]}
    calls = [_call(_sha("after the lift"))]
    verdict = parity_verdict(nodeid, calls, golden)
    assert verdict is not None
    assert nodeid in verdict


@pytest.mark.unit
def test_a_different_call_count_fails() -> None:
    nodeid = "tests/unit/x.py::test_a"
    golden = {nodeid: [_call(_sha("first")), _call(_sha("second"))]}
    too_few = [_call(_sha("first"))]
    too_many = [
        _call(_sha("first")),
        _call(_sha("second")),
        _call(_sha("third")),
    ]
    for calls in (too_few, too_many):
        verdict = parity_verdict(nodeid, calls, golden)
        assert verdict is not None
        assert nodeid in verdict


@pytest.mark.unit
def test_reordering_the_calls_fails() -> None:
    nodeid = "tests/unit/x.py::test_a"
    golden = {nodeid: [_call(_sha("first")), _call(_sha("second"))]}
    calls = [_call(_sha("second")), _call(_sha("first"))]
    verdict = parity_verdict(nodeid, calls, golden)
    assert verdict is not None
    assert nodeid in verdict


@pytest.mark.unit
def test_an_id_added_to_a_captured_file_fails() -> None:
    golden = {"tests/unit/x.py::test_captured": [_call(_sha("first"))]}
    nodeid = "tests/unit/x.py::test_added_after_the_lift"
    verdict = parity_verdict(nodeid, [], golden)
    assert verdict == (
        f"{nodeid}: no pre-lift capture; capture on the pre-lift base "
        f"with {PARITY_ENV}=capture"
    )


@pytest.mark.unit
def test_a_file_the_capture_never_saw_is_not_a_subject() -> None:
    golden = {"tests/unit/x.py::test_captured": [_call(_sha("first"))]}
    nodeid = "tests/unit/new_file.py::test_written_after_the_capture"
    assert parity_verdict(nodeid, [], golden) is None
    assert parity_verdict(nodeid, [_call(_sha("first"))], golden) is None


@pytest.mark.unit
def test_the_golden_knows_only_files_that_exist() -> None:
    golden = _golden()
    assert len(golden) == GOLDEN_IDS
    files = {nodeid.split("::", 1)[0] for nodeid in golden}
    assert len(files) == GOLDEN_FILES
    missing = [name for name in sorted(files) if not REPO_ROOT.joinpath(name).exists()]
    assert missing == []


@pytest.mark.unit
def test_compare_is_the_default_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PARITY_ENV, raising=False)
    assert lease_parity.parity_mode() == "compare"
    assert conftest.parity_mode() == "compare"


@pytest.mark.unit
def test_capture_and_off_are_explicit_modes() -> None:
    assert parity_mode({PARITY_ENV: "capture"}) == "capture"
    assert parity_mode({PARITY_ENV: "off"}) == "off"
    assert parity_mode({}) == "compare"
