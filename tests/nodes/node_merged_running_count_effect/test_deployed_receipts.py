# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence kind deployed from the lane-a lab-pass receipt, and the placement advertisement behind idle slots."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_merged_running_count import ModelMrcConfig
from omnimarket.nodes.node_merged_running_count_effect.handlers.handler_merged_running_count_effect import (
    HandlerMergedRunningCountEffect,
)
from omnimarket.nodes.node_merged_running_count_effect.models.model_merged_running_count_run import (
    ModelMergedRunningCountRunRequest,
    ModelMergedRunningCountRunResult,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_lab_pass_receipts import (
    ModelLabPassLabels,
    SourceLabPassReceipts,
    build_lab_pass_source,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_local_files import (
    SourceLocalFiles,
)
from omnimarket.protocols.protocol_merged_running_sources import (
    ProtocolMergedRunningSources,
)
from tests.nodes.node_merged_running_count_effect.test_effect_merged_running_count import (
    HOST_TABLE,
    LABELS,
    LEDGER,
    NOW,
    WATCHER,
    _config,
)

INFRA_SHA = "1baf2f7d7f13a0089d155ddf61b71b7bc1d3ab1c"
OTHER_SHA = "33b737bbc0a05524772462e123d721663472a3d7"
LAB_LABELS = ModelLabPassLabels(
    lane="lane-a",
    repos=("omnibase_infra",),
    kind="deployed",
    source="lane-a lab-pass receipt",
    pass_result="PASS",
)


def _projection(sha: str, result: str, lane: str = "lane-a") -> str:
    return json.dumps(
        {
            "topic": "onex.snapshot.projection.lab.lane-health.v1",
            "rows": [
                {
                    "lane": lane,
                    "receipt_observed_at": "2026-11-01T22:19:45+00:00",
                    "receipt_sha": sha,
                    "receipt_result": result,
                }
            ],
        }
    )


def _serves(sha: str, result: str, lane: str = "lane-a") -> Callable[[str], str]:
    return lambda _url: _projection(sha, result, lane)


class _Container:
    def __init__(self, sources: SourceLocalFiles) -> None:
        self._sources = sources

    def get_service(self, key: Any) -> Any:
        if key is not ProtocolMergedRunningSources:
            raise LookupError(key)
        return self._sources


def _run(
    tmp_path: Path,
    fetch: Any = None,
    *,
    ledger: str = LEDGER,
    placement: list[dict[str, Any]] | None = None,
) -> ModelMergedRunningCountRunResult:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "state.json").write_text(json.dumps(WATCHER))
    (tmp_path / "ledger.md").write_text(ledger + "\n")
    (tmp_path / "hosts.yaml").write_text(yaml.safe_dump(HOST_TABLE))
    placement_file = tmp_path / "hosts.json"
    if placement is not None:
        placement_file.write_text(json.dumps(placement))
    lab_pass = (
        None
        if fetch is None
        else SourceLabPassReceipts(
            url="http://lab.invalid/p", labels=LAB_LABELS, fetch=fetch
        )
    )
    sources = SourceLocalFiles(
        watcher_state=tmp_path / "state.json",
        ledger=tmp_path / "ledger.md",
        labels=LABELS,
        hosts_table=tmp_path / "hosts.yaml",
        placement_read=placement_file,
        now=NOW,
        lab_pass=lab_pass,
    )
    config: ModelMrcConfig = _config(
        evidence_sources=[
            {
                "kind": "adopted",
                "repos": ["omnibase_core", "omniclaude"],
                "missing_reason": "not_yet_adopted",
            },
            {
                "kind": "deployed",
                "repos": ["omnibase_infra"],
                "missing_reason": "not_yet_deployed",
            },
        ]
    )
    container: Any = _Container(sources)
    return HandlerMergedRunningCountEffect(container).handle(
        ModelMergedRunningCountRunRequest(now=NOW, config=config)
    )


def _without_adopted_infra() -> str:
    return "\n".join(
        line for line in LEDGER.splitlines() if "pr=omnibase_infra#5478" not in line
    )


def _day(result: ModelMergedRunningCountRunResult) -> Any:
    return next(d for d in result.count.days if d.local_date == "2026-11-01")


def test_deployed_receipt_moves_running_by_exactly_one(tmp_path: Path) -> None:
    ledger = _without_adopted_infra()
    before = _run(tmp_path / "a", _serves(OTHER_SHA, "PASS"), ledger=ledger)
    after = _run(tmp_path / "b", _serves(INFRA_SHA, "PASS"), ledger=ledger)
    assert (_day(before).merged, _day(before).running) == (4, 2)
    assert (_day(after).merged, _day(after).running) == (4, 3)
    merge = next(m for m in _day(after).merges if m.number == 5478)
    assert (merge.running, merge.evidence_kind, merge.evidence_source) == (
        True,
        "deployed",
        "lane-a lab-pass receipt",
    )
    assert after.count.evidence_unread == ()


def test_deployed_fail_receipt_is_no_evidence(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _serves(INFRA_SHA, "FAIL"),
        ledger=_without_adopted_infra(),
    )
    merge = next(m for m in _day(result).merges if m.number == 5478)
    assert (merge.running, merge.reason) == (False, "not_yet_deployed")
    assert result.count.evidence_unread == ()


def test_deployed_receipt_of_another_lane_is_no_evidence(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _serves(INFRA_SHA, "PASS", lane="lane-b"),
        ledger=_without_adopted_infra(),
    )
    assert next(m for m in _day(result).merges if m.number == 5478).running is False


def test_deployed_unread_names_its_cause_and_keeps_the_rest_of_the_count(
    tmp_path: Path,
) -> None:
    def refuse(url: str) -> str:
        raise OSError("connection refused")

    result = _run(tmp_path, refuse, ledger=_without_adopted_infra())
    assert any("connection refused" in cause for cause in result.count.evidence_unread)
    assert (_day(result).merged, _day(result).running) == (4, 2)


def test_deployed_source_not_configured_is_unread_not_zero(tmp_path: Path) -> None:
    result = _run(tmp_path, None, ledger=_without_adopted_infra())
    assert result.count.evidence_unread != ()


def test_idle_slots_from_the_placement_advertisement_name_their_source(
    tmp_path: Path,
) -> None:
    ads = [{"host_name": "h051", "max_units": 10}]
    idle = {
        h.host: h
        for h in _run(
            tmp_path, _serves(OTHER_SHA, "PASS"), placement=ads
        ).count.idle_hosts
    }
    assert (
        idle["h051"].slots,
        idle["h051"].idle,
        idle["h051"].slots_source,
        idle["h051"].cause,
    ) == (10, 9, "placement", "")
    assert idle["h053"].slots_source == "table_cap"
    assert idle["h053"].cause == "no placement advertisement for the host"


def test_idle_slots_fall_back_to_the_table_cap_with_the_cause_when_no_advertisement_file(
    tmp_path: Path,
) -> None:
    idle = {
        h.host: h for h in _run(tmp_path, _serves(OTHER_SHA, "PASS")).count.idle_hosts
    }
    assert (idle["h051"].slots, idle["h051"].slots_source) == (24, "table_cap")
    assert idle["h051"].cause == "no placement advertisement read"


def test_idle_host_with_no_cap_and_no_advertisement_reads_unread_not_zero(
    tmp_path: Path,
) -> None:
    table = {"hosts": [{"name": "h900"}]}
    (tmp_path / "hosts.yaml").write_text(yaml.safe_dump(table))
    (tmp_path / "state.json").write_text(json.dumps(WATCHER))
    (tmp_path / "ledger.md").write_text(LEDGER + "\n")
    sources = SourceLocalFiles(
        watcher_state=tmp_path / "state.json",
        ledger=tmp_path / "ledger.md",
        labels=LABELS,
        hosts_table=tmp_path / "hosts.yaml",
        placement_read=tmp_path / "none.json",
        now=NOW,
    )
    host = sources.read_hosts()[0]
    assert (host.slots, host.slots_source) == (None, "")
    assert host.cause == "no placement advertisement read and no cap in the host table"


def test_deployed_source_is_built_from_the_contract_and_the_environment() -> None:
    declared = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_merged_running_count_effect/contract.yaml"
        ).read_text()
    )["runtime"]["lab_pass"]
    env = {declared["url_env"]: "http://lab.invalid/p", declared["lane_env"]: "lane-a"}
    source, cause = build_lab_pass_source(declared, env, _serves(INFRA_SHA, "PASS"))
    assert source is not None
    assert cause == ""
    read = source.read()
    assert {(e.repo, e.kind, e.source) for e in read.evidence} == {
        ("omnibase_infra", "deployed", "lane-a lab-pass receipt"),
        ("omnimarket", "deployed", "lane-a lab-pass receipt"),
    }
    unset, why = build_lab_pass_source(
        declared, {declared["url_env"]: "http://lab.invalid/p"}
    )
    assert unset is None
    assert declared["lane_env"] in why
