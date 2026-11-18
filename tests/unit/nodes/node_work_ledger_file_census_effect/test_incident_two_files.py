# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression for the census incident: 188 files against a ratchet of 186, two files over it.

``fixtures/incident_census_188.txt`` is the census output recorded on origin/dev at ebd9d70b23 (``--verbose``,
restricted to the summary lines and the hits of the two files that took the count past its row). The count rose
from 186 to 187 at 0679e4ad9a (a tick runtime that read the ledger path variable to name the file it appended to)
and from 187 to 188 at ebd9d70b23 (a contract falsifier that named the path variable). At 188,
the ledger-file-census hook refused commits.

The recorded output is the input of these tests; the live files are classified by the census's own classifier, so
the test fails while either file still holds an executable reader or writer and passes once neither does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_work_ledger_file_census_effect.adapters import (
    GitRepositorySource,
    YamlRatchetStore,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.classify import (
    executable_hits,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusOverlay,
    ModelCensusRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "src/omnimarket/nodes/node_work_ledger_file_census_effect"
RECORDED = Path(__file__).with_name("fixtures") / "incident_census_188.txt"
SUMMARY = re.compile(
    r"^CENSUS omnimarket counted state=(?P<state>\w+) files=(?P<files>\d+) "
    r"readers=\d+ writers=\d+ ratchet=(?P<ratchet>\d+)$"
)
HIT = re.compile(r"^  (?P<kind>reader|writer) (?P<path>\S+):(?P<line>\d+)$")


def _recorded() -> tuple[re.Match[str], tuple[str, ...]]:
    lines = RECORDED.read_text(encoding="utf-8").splitlines()
    summary = SUMMARY.match(lines[0])
    assert summary is not None
    paths = tuple(sorted({m["path"] for line in lines if (m := HIT.match(line))}))
    return summary, paths


def _overlay() -> ModelCensusOverlay:
    data = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    return ModelCensusOverlay.model_validate(data)


def test_recorded_incident_is_two_files_over_the_ratchet() -> None:
    summary, paths = _recorded()
    assert summary["state"] == "grew"
    assert int(summary["files"]) - int(summary["ratchet"]) == len(paths) == 2


@pytest.mark.parametrize("path", _recorded()[1])
def test_incident_file_holds_no_executable_ledger_file_read(path: str) -> None:
    """Reject a recorded file while it still has an executable reader or writer."""
    text = (ROOT / path).read_text(encoding="utf-8")
    hits = executable_hits(path, text, _overlay())
    assert hits == (), [f"{h.kind.value} {h.path}:{h.line}" for h in hits]


def test_live_census_excludes_incident_files() -> None:
    overlay = _overlay()
    result = HandlerFileCensus(
        overlay,
        GitRepositorySource(ROOT, overlay.max_file_bytes, {"omnimarket": "."}),
        YamlRatchetStore(ROOT / overlay.ratchet_file),
        InMemoryEmissionSink(),
    ).handle(
        ModelCensusRequest(
            correlation_id="incident-ratchet",
            repositories=("omnimarket",),
        )
    )

    assert result.passed, (result.failure_code, result.failure_detail)
    (count,) = result.counts
    _, paths = _recorded()
    remaining = sorted(set(paths).intersection(hit.path for hit in count.hits))
    assert not remaining, f"Incident files still contribute to the census: {remaining}"
