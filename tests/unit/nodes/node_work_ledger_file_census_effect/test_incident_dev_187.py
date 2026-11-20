# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression for the census incident at origin/dev 3c2a974be5: 187 files against a ratchet of 186.

``fixtures/incident_census_187.txt`` is the census output recorded on origin/dev at 3c2a974be5 (``--verbose``): the
summary line and every counted hit of the 187 counted files. Against the census at the merge of the ratchet
(186 files, bf4163f91a) four files were added and three were dropped, which took the count to 187 and made the
ledger-file-census hook refuse every omnimarket commit:

* ``contracts/OMN-24829.yaml``: a done-gate check that read the ledger file by its path variable.
* ``contracts/OMN-25860.yaml``: an acceptance-criterion falsifier that named the path variable. It is not moved
  here: origin/dev 0fd01a8c5a had already moved it, and this branch's copy equals dev's.
* ``tests/nodes/node_factory_floor_publish_effect/test_factory_floor_cold_archive_reads.py``: a fixture that named
  the ledger file.
* ``src/omnimarket/nodes/node_review_key_credit_probe_effect/handlers/handler_review_key_credit_probe.py``: a writer
  whose click option reads the path variable. It is not moved here: it is a writer the reader node does not serve,
  and moving it changes the environment of the deployed unit, which lives outside this repository.

The recorded output is the input of these tests; the live files are classified by the census's own classifier, so
the live-file cases fail on origin/dev 3c2a974be5 and pass once the moved files hold no executable reader or writer.
Two files are moved by this branch (``contracts/OMN-24829.yaml`` and the cold archive test) and one was moved on dev
(``contracts/OMN-25860.yaml``), so its case passes on dev's own 0fd01a8c5a. The cases that read only the recorded
fixture and the constants of this module check the fixture's own shape; no change to the code under test can fail
them, so they are not evidence that the census is fixed.
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
    ModelCensusResult,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "src/omnimarket/nodes/node_work_ledger_file_census_effect"
RECORDED = Path(__file__).with_name("fixtures") / "incident_census_187.txt"
SUMMARY = re.compile(
    r"^CENSUS omnimarket counted state=(?P<state>\w+) files=(?P<files>\d+) "
    r"readers=\d+ writers=\d+ ratchet=(?P<ratchet>\d+)$"
)
HIT = re.compile(r"^  (?P<kind>reader|writer) (?P<path>\S+):(?P<line>\d+)$")

#: The files of the recorded list that are off the ledger file's name or path variable at the head of this branch:
#: two moved here (``contracts/OMN-24829.yaml`` and the cold archive test) and one moved on dev
#: (``contracts/OMN-25860.yaml``, origin/dev 0fd01a8c5a).
MOVED = (
    "contracts/OMN-24829.yaml",
    "contracts/OMN-25860.yaml",
    "tests/nodes/node_factory_floor_publish_effect/test_factory_floor_cold_archive_reads.py",
)


def _recorded() -> tuple[re.Match[str], tuple[str, ...]]:
    lines = RECORDED.read_text(encoding="utf-8").splitlines()
    summary = SUMMARY.match(lines[0])
    assert summary is not None
    paths = tuple(sorted({m["path"] for line in lines if (m := HIT.match(line))}))
    return summary, paths


def _overlay() -> ModelCensusOverlay:
    data = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    return ModelCensusOverlay.model_validate(data)


def test_recorded_dev_is_over_the_ratchet() -> None:
    """Fixture only: checks the recorded output's own shape and cannot fail on a code change."""
    summary, paths = _recorded()
    assert summary["state"] == "grew"
    assert int(summary["files"]) == len(paths) == 187
    assert int(summary["files"]) > int(summary["ratchet"]) == 186


@pytest.mark.parametrize("path", MOVED)
def test_moved_file_is_on_the_recorded_dev_list(path: str) -> None:
    """Fixture only: checks the recorded output's own shape and cannot fail on a code change."""
    assert path in _recorded()[1]


@pytest.mark.parametrize("path", MOVED)
def test_moved_file_holds_no_executable_ledger_file_read(path: str) -> None:
    """Reject a recorded file while it still has an executable reader or writer."""
    text = (ROOT / path).read_text(encoding="utf-8")
    hits = executable_hits(path, text, _overlay())
    assert hits == (), [f"{h.kind.value} {h.path}:{h.line}" for h in hits]


def _live_census() -> tuple[ModelCensusOverlay, ModelCensusResult]:
    overlay = _overlay()
    result = HandlerFileCensus(
        overlay,
        GitRepositorySource(ROOT, overlay.max_file_bytes, {"omnimarket": "."}),
        YamlRatchetStore(ROOT / overlay.ratchet_file),
        InMemoryEmissionSink(),
    ).handle(
        ModelCensusRequest(
            correlation_id="incident-dev-187",
            repositories=("omnimarket",),
        )
    )
    return overlay, result


def test_live_census_is_within_the_ratchet_and_drops_the_moved_files() -> None:
    _, result = _live_census()

    assert result.passed, (result.failure_code, result.failure_detail)
    (count,) = result.counts
    assert count.files <= 186
    remaining = sorted(set(MOVED).intersection(hit.path for hit in count.hits))
    assert not remaining, f"Moved files still contribute to the census: {remaining}"


def test_ratchet_and_hook_hold_while_the_moved_files_drop_out() -> None:
    """AC3: the count comes under the ratchet by moving files, never by raising the ratchet or widening the hook.

    The ratchet bound is an upper bound: lowering the row (``--lower`` after a passing run) or adding a repository
    key leaves it green, and raising the row above 186 turns it red. Against origin/dev the ratchet and hook
    assertions pass too; the case fails there only through the live-census assertions it repeats.
    """
    overlay, result = _live_census()
    ratchet = yaml.safe_load((ROOT / overlay.ratchet_file).read_text())["ratchet"]
    assert ratchet["omnimarket"] <= 186

    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "ledger-file-census"
    ]
    assert len(hooks) == 1
    assert hooks[0]["stages"] == ["pre-commit"]
    assert hooks[0]["entry"].endswith("--repos omnimarket --directory omnimarket=.")

    (count,) = result.counts
    assert count.files <= ratchet["omnimarket"]
    remaining = sorted(set(MOVED).intersection(hit.path for hit in count.hits))
    assert not remaining, (
        f"The ratchet holds only if the moved files drop out: {remaining}"
    )
