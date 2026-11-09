# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gate on the lane-result classifier discovery read.

The read lives in ``docs/reference/lane-result-classifier-discovery-read.json``. These tests hold it to
two rules: every behavioural equivalent carries a verdict (wire or build) backed by a code path that
exists and a run read, and an equivalent with no runtime caller is never reported as working. The
wiring state is recomputed here from the repository's own files, so the read cannot say wired for a
node nothing calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
READ_PATH = REPO / "docs" / "reference" / "lane-result-classifier-discovery-read.json"

VERDICTS = frozenset({"wire", "build"})
#: Evidence kinds that register a node without anything calling it.
REGISTRATION_KINDS = frozenset({"entry_point", "bus_subscription"})
#: Evidence kinds that put a node under a runtime caller.
RUNTIME_KINDS = frozenset({"in_repo_caller", "external_caller", "schedule"})
RUN_READ_CELLS = ("host", "command", "observed", "read_at")


def _load() -> dict[str, Any]:
    read: dict[str, Any] = json.loads(READ_PATH.read_text(encoding="utf-8"))
    return read


def _equivalents(read: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["id"]: entry for entry in read["equivalents"]}


def _entry_point_registered(node: str) -> bool:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    return f'{node} = "omnimarket.nodes.{node}"' in text


def _subscribes(node: str) -> bool:
    contract = yaml.safe_load(
        (REPO / "src/omnimarket/nodes" / node / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    return bool((contract.get("event_bus") or {}).get("subscribe_topics"))


def _evidence_counts(
    entry: dict[str, Any],
    evidence: dict[str, Any],
    equivalents: dict[str, Any],
    seen: set[str],
) -> bool:
    kind = evidence["kind"]
    if kind in REGISTRATION_KINDS:
        return False
    if kind == "external_caller":
        # Another repository is not readable from this one's tests: the read attests it and says
        # where, and the run read of the equivalent carries the observation.
        return evidence.get("attested") is True and bool(evidence.get("repo"))
    if kind == "schedule":
        return (REPO / evidence["path"]).is_file()
    if kind == "in_repo_caller":
        caller = REPO / evidence["path"]
        if not caller.is_file():
            return False
        if f"omnimarket.nodes.{entry['node']}" not in caller.read_text(
            encoding="utf-8"
        ):
            return False
        via = evidence.get("caller_equivalent")
        if via is None:
            return True
        return _computed_wired(equivalents[via], equivalents, seen)
    return False


def _computed_wired(
    entry: dict[str, Any], equivalents: dict[str, Any], seen: set[str] | None = None
) -> bool:
    """Wired when some evidence entry puts a runtime caller over the node."""
    visited = set() if seen is None else seen
    if entry["id"] in visited:
        return False
    visited = visited | {entry["id"]}
    return any(
        _evidence_counts(entry, evidence, equivalents, visited)
        for evidence in entry["wiring_evidence"]
    )


def verdict_problems(entry: dict[str, Any]) -> list[str]:
    """Why the entry's verdict is not backed by a code path and a run read; empty when it is."""
    problems: list[str] = []
    if entry.get("verdict") not in VERDICTS:
        problems.append(
            f"{entry.get('id')}: verdict {entry.get('verdict')!r} is not wire or build"
        )
    if not str(entry.get("verdict_basis", "")).strip():
        problems.append(f"{entry.get('id')}: no verdict_basis")
    code_path = entry.get("code_path", "")
    if not code_path or not (REPO / code_path).is_file():
        problems.append(f"{entry.get('id')}: code_path {code_path!r} is not a file")
    run_read = entry.get("run_read") or {}
    for cell in RUN_READ_CELLS:
        if not str(run_read.get(cell, "")).strip():
            problems.append(f"{entry.get('id')}: run_read has no {cell}")
    return problems


def wiring_problems(entry: dict[str, Any], equivalents: dict[str, Any]) -> list[str]:
    """Where the entry's reported state outruns its runtime wiring; empty when it does not."""
    problems: list[str] = []
    wired = _computed_wired(entry, equivalents)
    recorded = entry.get("wiring_state")
    if recorded not in ("wired", "unwired"):
        problems.append(
            f"{entry.get('id')}: wiring_state {recorded!r} is not wired or unwired"
        )
    elif (recorded == "wired") != wired:
        problems.append(
            f"{entry.get('id')}: records {recorded} but the repository shows "
            f"{'a runtime caller' if wired else 'no runtime caller'}"
        )
    if not wired and entry.get("reported_state") != "not-working":
        problems.append(
            f"{entry.get('id')}: has no runtime caller and is reported {entry.get('reported_state')!r}"
        )
    if entry.get("reported_state") == "working" and not wired:
        problems.append(f"{entry.get('id')}: reported working without runtime wiring")
    return problems


def test_verdicts_cited() -> None:
    read = _load()
    equivalents = _equivalents(read)
    assert len(equivalents) == len(read["equivalents"]), "equivalent ids must be unique"
    assert equivalents, "the read lists no equivalents"
    problems = [p for entry in equivalents.values() for p in verdict_problems(entry)]
    assert problems == []
    # The read must name the node that carries the classifier and a tracked list of results.
    assert "lane-result-sweep" in equivalents
    assert "ledger-terminal-rows" in equivalents
    # Both verdicts occur, so the read has decided something rather than listing everything one way.
    assert {entry["verdict"] for entry in equivalents.values()} == VERDICTS


def test_verdicts_cited_rejects_an_uncited_verdict() -> None:
    """Positive control: an entry without a real code path, run read or verdict is refused."""
    entry = _equivalents(_load())["lane-result-sweep"]
    assert verdict_problems(entry) == []
    assert verdict_problems({**entry, "verdict": "maybe"})
    assert verdict_problems(
        {**entry, "code_path": "src/omnimarket/nodes/none/handler.py"}
    )
    assert verdict_problems(
        {**entry, "run_read": {**entry["run_read"], "observed": ""}}
    )
    assert verdict_problems({**entry, "verdict_basis": " "})


def test_unwired_not_working() -> None:
    read = _load()
    equivalents = _equivalents(read)
    problems = [
        p for entry in equivalents.values() for p in wiring_problems(entry, equivalents)
    ]
    assert problems == []
    # The sweep is registered as an entry point and still has no caller: the read says so.
    sweep = equivalents["lane-result-sweep"]
    assert _entry_point_registered(sweep["node"])
    assert not _subscribes(sweep["node"])
    assert sweep["wiring_state"] == "unwired"
    assert sweep["reported_state"] == "not-working"
    # An unwired equivalent exists in the read, so the rule above was exercised.
    assert any(entry["wiring_state"] == "unwired" for entry in equivalents.values())


def test_unwired_not_working_rejects_a_registered_node_reported_working() -> None:
    """Positive control: an entry point or a bus subscription alone never makes a node working."""
    equivalents = _equivalents(_load())
    sweep = equivalents["lane-result-sweep"]
    assert wiring_problems(sweep, equivalents) == []
    claimed_working = {**sweep, "reported_state": "working"}
    assert wiring_problems(claimed_working, equivalents)
    claimed_wired = {**sweep, "wiring_state": "wired", "reported_state": "working"}
    assert wiring_problems(claimed_wired, equivalents)
    # A node whose only caller is an unwired equivalent is unwired too.
    normalize = equivalents["error-class-normalize"]
    assert not _computed_wired(normalize, equivalents)
    # A wired equivalent with a real caller passes, so the check does not refuse everything.
    close = equivalents["remote-lane-close"]
    assert _computed_wired(close, equivalents)
    assert wiring_problems(close, equivalents) == []


@pytest.mark.parametrize("entry_id", ["lane-result-sweep", "remote-lane-close"])
def test_code_paths_name_a_handler_of_the_node(entry_id: str) -> None:
    entry = _equivalents(_load())[entry_id]
    assert f"nodes/{entry['node']}/" in entry["code_path"]
