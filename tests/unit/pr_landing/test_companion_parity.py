# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-19827): the compute-versus-emitter parity report.

For every recorded companion the harness reports ``byte-equal`` or the list of
differing files. The parity verdict never fails this test: a difference is the
finding the harness exists to measure (it decides when the derivation moves to
the compute path, OMN-15192). What the test does assert is that the harness
itself works -- every fixture gets a verdict, and a file altered in a fixture is
reported as differing -- so a report that says "byte-equal" can be trusted.

Set ``COMPANION_PARITY_REPORT=<path>`` to also write the report as JSON.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from tests.unit.pr_landing.companion_parity import (
    CompanionParity,
    load_fixtures,
    parity_of,
    render_report,
)

pytestmark = pytest.mark.unit

_EMITTER = "emitter"
_VERDICTS = {"byte-equal", "differs", "error"}


def test_fixtures_cover_at_least_five_emitter_companions_from_the_day() -> None:
    fixtures = load_fixtures()
    emitter = [f for f in fixtures if f["producer"] == _EMITTER]
    assert len(emitter) >= 5
    for fixture in fixtures:
        provenance = fixture["provenance"]
        assert isinstance(provenance, dict)
        assert provenance["recorded_on"] == "2026-09-26"
        assert provenance["occ_base_commit"]
        assert provenance["producer_commits"]
        assert fixture["committed_files"]


def test_parity_report_names_a_verdict_for_every_companion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = [parity_of(fixture) for fixture in load_fixtures()]
    assert results
    for result in results:
        assert result.verdict in _VERDICTS
        if result.verdict == "differs":
            assert result.differing_files
        if result.verdict == "byte-equal":
            assert not result.differing_files
            assert result.compared_files
    report = render_report(results)
    with capsys.disabled():
        print("\n" + report)
    target = os.environ.get("COMPANION_PARITY_REPORT")
    if target:
        Path(target).write_text(
            json.dumps([r.as_dict() for r in results], indent=2) + "\n",
            encoding="utf-8",
        )


def _baseline_and_altered(
    fixture: dict[str, object],
) -> tuple[CompanionParity, CompanionParity, str]:
    baseline = parity_of(fixture)
    committed = fixture["committed_files"]
    assert isinstance(committed, dict)
    unchanged = [p for p in sorted(committed) if p not in baseline.differing_files]
    altered = copy.deepcopy(fixture)
    altered_files = altered["committed_files"]
    assert isinstance(altered_files, dict)
    if unchanged:
        path = unchanged[0]
        altered_files[path] = str(altered_files[path]) + "altered: true\n"
    else:
        path = "drift/dod_receipts/OMN-0/altered/command.yaml"
        altered_files[path] = "altered: true\n"
    return baseline, parity_of(altered), path


@pytest.mark.parametrize(
    "fixture",
    load_fixtures(),
    ids=lambda f: f"occ_{f['occ_pr']}",
)
def test_an_altered_committed_file_is_reported_as_differing(
    fixture: dict[str, object],
) -> None:
    baseline, altered, path = _baseline_and_altered(fixture)
    if baseline.verdict == "error":
        assert altered.verdict == "error"
        return
    assert altered.verdict == "differs"
    assert path in altered.differing_files
    assert set(altered.differing_files) == set(baseline.differing_files) | {path}
