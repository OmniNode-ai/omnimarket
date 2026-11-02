# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The count read through the container from files shaped like the lab's own, with counts written by hand.

The PR watcher records and the ADOPTED, CLAIM and TERMINAL rows below are shaped from the 2026-11-01 records of
the lab (trimmed to the cells the reader uses, repository names replaced by public ones); the expected counts are
written by hand from them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_merged_running_count_compute.models.model_merged_running_count import (
    ModelMrcConfig,
)
from omnimarket.nodes.node_merged_running_count_effect.handlers.handler_merged_running_count_effect import (
    HandlerMergedRunningCountEffect,
)
from omnimarket.nodes.node_merged_running_count_effect.models.model_merged_running_count_run import (
    ModelMergedRunningCountRunRequest,
    ModelMergedRunningCountRunResult,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_contract_config import (
    load_contract_config,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_local_files import (
    ModelSourceLabels,
    SourceLocalFiles,
)
from omnimarket.protocols.protocol_merged_running_sources import (
    ProtocolMergedRunningSources,
)

NOW = datetime(2026, 11, 1, 23, 9, 34, tzinfo=UTC)

WATCHER = {
    "merges": {
        "omnibase_core#1310": {
            "repo": "omnibase_core",
            "number": 1310,
            "base": "main",
            "merge_sha": "00c003a4bfaf2a94a9c3fc166442286c125d86e7",
            "merged_at": "2026-11-01T17:36:30Z",
        },
        "omniclaude#1386": {
            "repo": "omniclaude",
            "number": 1386,
            "base": "main",
            "merge_sha": "597b612904d8734cff5484ee7b1f9ac96da74354",
            "merged_at": "2026-11-01T17:31:16Z",
        },
        "omnibase_infra#5478": {
            "repo": "omnibase_infra",
            "number": 5478,
            "base": "dev",
            "merge_sha": "1baf2f7d7f13a0089d155ddf61b71b7bc1d3ab1c",
            "merged_at": "2026-11-01T18:07:13Z",
        },
        "omnimarket#4734": {
            "repo": "omnimarket",
            "number": 4734,
            "base": "dev",
            "merge_sha": "35170a88a3d8a1e8089f07b2466e3adcf40e8583",
            "merged_at": "2026-11-01T20:10:19Z",
        },
    },
    "prs": {
        "omnibase_core#2211": {
            "facts": {
                "repo": "omnibase_core",
                "number": 2211,
                "state": "OPEN",
                "base": "dev",
                "armed": True,
            },
            "handoff": None,
            "queued": False,
        },
        "omnibase_infra#5511": {
            "facts": {
                "repo": "omnibase_infra",
                "number": 5511,
                "state": "OPEN",
                "base": "dev",
                "armed": False,
            },
            "handoff": {"lane": "landing-L22409", "needs": "land"},
            "queued": False,
        },
        "omnibase_core#1310": {
            "facts": {
                "repo": "omnibase_core",
                "number": 1310,
                "state": "MERGED",
                "base": "main",
                "armed": False,
            },
            "handoff": {"lane": "x"},
            "queued": False,
        },
    },
}

LEDGER = "\n".join(
    [
        "2026-11-01T17:53:03Z | STATUS | lane=adopt-merged | principal=p | ticket=OMN-21864 | actor=script:adopt_merged | model=none | ADOPTED pr=omnibase_core#1310 merge=00c003a4bfaf | targets=morning-workflows-lib:1 | drift=0",
        "2026-11-01T18:25:06Z | STATUS | lane=adopt-merged | principal=p | ticket=OMN-17427 | actor=script:adopt_merged | model=none | ADOPTED pr=omnibase_infra#5478 merge=1baf2f7d7f13 | targets=lab-pool-driver-worktree:1 | drift=0",
        "2026-11-01T19:25:03Z | STATUS | lane=adopt-merged | principal=p | ticket=OMN-21843 | actor=script:adopt_merged | model=none | ADOPTED pr=omniclaude#1386 merge=597b612904d8 | targets=omni-marketplace:2 | drift=0",
        # A MSG that merely mentions the words is not an adoption.
        "2026-11-01T23:10:25Z | MSG | from=l | to=floor-fix-1101 | ticket=OMN-21733 | the ledger rows (ADOPTED pr= STATUS text) feed the count",
        "2026-11-01T22:43:42Z | CLAIM | lane=floor-fix-1101 | principal=p | ticket=OMN-21733 | actor=a | model=m | host=h051 | run=r",
        "2026-11-01T23:08:27Z | CLAIM | lane=merged-running-count-1101 | principal=p | ticket=OMN-21733 | actor=a | model=m | host=h101 | run=r",
        "2026-11-01T21:00:00Z | CLAIM | lane=ended-lane | principal=p | ticket=OMN-1 | actor=a | model=m | host=h051 | run=r",
        "2026-11-01T22:00:00Z | TERMINAL | lane=ended-lane | principal=p | ticket=OMN-1 | outcome=done",
        "2026-10-30T10:00:00Z | CLAIM | lane=abandoned-lane | principal=p | ticket=OMN-2 | actor=a | model=m | host=h053 | run=r",
        "2026-11-01T22:44:00Z | CLAIM | lane=hostless-lane | principal=p | ticket=OMN-3 | actor=a | model=m",
    ]
)

HOST_TABLE = {
    "hosts": [
        {"name": "h051", "cap": 24},
        {"name": "h053", "cap": 6},
        {"name": "h101", "cap": 12},
        {"name": "h202", "cap": 8, "available": False},
    ]
}

LABELS = ModelSourceLabels(
    adopt_lane="adopt-merged",
    adopted_kind="adopted",
    merged_state="merged",
    handed_off_state="handed_off",
    armed_state="armed",
    queued_state="queued",
    open_state="open",
    claim_row="CLAIM",
    terminal_row="TERMINAL",
    lane_live_window_hours=24,
)


class _Container:
    def __init__(self, sources: SourceLocalFiles) -> None:
        self._sources = sources

    def get_service(self, key: Any) -> Any:
        if key is not ProtocolMergedRunningSources:
            raise LookupError(key)
        return self._sources


def _sources(tmp_path: Path, ledger: str = LEDGER) -> SourceLocalFiles:
    (tmp_path / "state.json").write_text(json.dumps(WATCHER))
    (tmp_path / "ledger.md").write_text(ledger + "\n")
    (tmp_path / "hosts.yaml").write_text(yaml.safe_dump(HOST_TABLE))
    return SourceLocalFiles(
        watcher_state=tmp_path / "state.json",
        ledger=tmp_path / "ledger.md",
        labels=LABELS,
        hosts_table=tmp_path / "hosts.yaml",
        placement_read=tmp_path / "absent-hosts.json",
        now=NOW,
    )


def _config(**changes: object) -> ModelMrcConfig:
    base = load_contract_config().model_dump(mode="json")
    base["evidence_sources"] = [
        {
            "kind": "adopted",
            "repos": ["omnibase_core", "omniclaude", "omnibase_infra"],
            "missing_reason": "not_yet_adopted",
        },
        {"kind": "deployed", "repos": [], "missing_reason": "not_yet_deployed"},
    ]
    base.update(changes)
    return ModelMrcConfig.model_validate(base)


def _run(
    tmp_path: Path, ledger: str = LEDGER, config: ModelMrcConfig | None = None
) -> ModelMergedRunningCountRunResult:
    container: Any = _Container(_sources(tmp_path, ledger))
    return HandlerMergedRunningCountEffect(container).handle(
        ModelMergedRunningCountRunRequest(now=NOW, config=config or _config())
    )


def test_recorded_day_counts_match_the_hand_count(tmp_path: Path) -> None:
    result = _run(tmp_path)
    day = next(d for d in result.count.days if d.local_date == "2026-11-01")
    # Hand count: four merges that day (1310, 1386, 5478, 4734); three have an ADOPTED row.
    assert (day.merged, day.running, day.not_running) == (4, 3, 1)
    not_running = [m for m in day.merges if not m.running]
    assert [(m.repo, m.number, m.reason) for m in not_running] == [
        ("omnimarket", 4734, "no_evidence_source")
    ]
    assert {m.evidence_kind for m in day.merges if m.running} == {"adopted"}
    # Armed, handed-off and already-merged-with-a-hand-off records are read but never add to the count.
    assert result.pr_records_read == 4 + 2
    assert result.count.ignored_evidence == 0


def test_a_message_that_mentions_adopted_rows_is_not_evidence(tmp_path: Path) -> None:
    assert _run(tmp_path).evidence_records_read == 3


def test_idle_slots_are_the_table_cap_minus_open_claims_inside_the_window(
    tmp_path: Path,
) -> None:
    idle = {h.host: h for h in _run(tmp_path).count.idle_hosts}
    # h051: cap 24, floor-fix-1101 open, ended-lane has a TERMINAL; h053: abandoned-lane is past the window.
    assert (idle["h051"].slots, idle["h051"].live_slots, idle["h051"].idle) == (
        24,
        1,
        23,
    )
    assert (idle["h053"].slots, idle["h053"].live_slots, idle["h053"].idle) == (6, 0, 6)
    assert (idle["h101"].slots, idle["h101"].live_slots, idle["h101"].idle) == (
        12,
        1,
        11,
    )
    assert (idle["h202"].state, idle["h202"].idle) == ("out", 0)


def test_removing_the_adopted_row_moves_running_by_exactly_one(tmp_path: Path) -> None:
    without = "\n".join(
        line for line in LEDGER.splitlines() if "pr=omnibase_infra#5478" not in line
    )
    result = _run(tmp_path, ledger=without)
    day = next(d for d in result.count.days if d.local_date == "2026-11-01")
    assert (day.merged, day.running) == (4, 2)
    assert next(m for m in day.merges if m.number == 5478).reason == "not_yet_adopted"


def test_a_declared_timezone_in_the_overlay_config_moves_the_day(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path, config=_config(operator_timezone="Asia/Tokyo", window_days=2)
    )
    # 17:31Z to 20:10Z on the 1st is 02:31 to 05:10 on the 2nd in Tokyo.
    days = {d.local_date: d.merged for d in result.count.days}
    assert days == {"2026-11-02": 4, "2026-11-01": 0}


def test_the_container_serves_only_the_declared_protocol(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        _Container(_sources(tmp_path)).get_service(object)
