# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refused dispatch records and budget refunds without controller scripts."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.nodes.node_landing_not_started_effect import NodeLandingNotStartedEffect
from omnimarket.nodes.node_landing_not_started_effect.handlers import (
    handler_landing_not_started as ns,
)
from omnimarket.nodes.node_landing_not_started_effect.models import (
    ModelLandingNotStartedRequest,
)

pytestmark = pytest.mark.unit
NOW = "2030-01-01T01:00:00Z"
BEFORE = "2030-01-01T00:00:00Z"
REFUSED = ("head-moved", "claim-refused")


def outcome(
    lid: int = 1, subject: str = "example#1", result: str = "head-moved"
) -> dict[str, Any]:
    return {
        "kind": "dispatch_worker",
        "lease_id": lid,
        "subject": subject,
        "outcome": result,
        "detail": "head changed",
        "head_sha": "a" * 40,
    }


def facts() -> SimpleNamespace:
    def refund(
        window: dict[str, list[str]], shorts: list[str], *, now: str
    ) -> dict[str, list[str]]:
        result = {
            key: [stamp for stamp in stamps if key not in shorts or stamp != now]
            for key, stamps in window.items()
        }
        return {key: stamps for key, stamps in result.items() if stamps}

    def uncount(counts: dict[str, Any], heads: list[tuple[str, str]]) -> dict[str, Any]:
        for key, head in heads:
            if key in counts and counts[key]["head"] == head:
                counts[key]["n"] -= 1
                if counts[key]["n"] <= 0:
                    del counts[key]
        return counts

    return SimpleNamespace(
        LEASE_REFUSED_OUTCOMES=REFUSED,
        short_key=lambda key: key,
        lease_window_refund=refund,
        lease_window_of=lambda side: side.get("lease_window", {}),
        uncount_head_dispatches=uncount,
    )


def record(path: Path, outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return ns.record_not_started(path, outcomes, facts(), now=NOW, tick=1)


def test_records_each_refused_dispatch_once(tmp_path: Path) -> None:
    refused = [
        outcome(),
        outcome(2, "example#2"),
        outcome(4, "cause:example/repo:group", "claim-refused"),
    ]
    spawned = outcome(3, "example#3", "spawned")
    rows = record(tmp_path, [*refused, spawned])
    assert [r["lease_id"] for r in rows] == [1, 2, 4]
    for original, row in zip(refused, rows, strict=True):
        assert (row["pr"], row["outcome"], row["reason"], row["head"]) == (
            original["subject"],
            original["outcome"],
            original["detail"],
            original["head_sha"],
        )
        assert ns.not_started_of(tmp_path, row["lease_id"]) == row
    assert ns.not_started_of(tmp_path, 3) is None
    assert record(tmp_path, refused) == []
    journal = (tmp_path / ns.NOT_STARTED_JOURNAL).read_text().splitlines()
    assert [json.loads(line)["lease_id"] for line in journal] == [1, 2, 4]
    assert not (tmp_path / "worker-failures.jsonl").exists()
    assert not list(tmp_path.glob("workers/*/result.json"))
    assert not list(tmp_path.glob("workers/*/no-result-recorded.json"))


def test_entry_point_ignores_spawned(tmp_path: Path) -> None:
    assert NodeLandingNotStartedEffect.contract_path.is_file()
    result = NodeLandingNotStartedEffect().handle(
        ModelLandingNotStartedRequest(
            state_dir=tmp_path,
            outcomes=(outcome(result="spawned"),),
            refused=REFUSED,
            now=NOW,
            tick=1,
        )
    )
    assert result.rows == ()
    assert not list(tmp_path.iterdir())


def test_filters_only_dispatches_with_lease_ids() -> None:
    good = outcome()
    bad = [
        {**good, "kind": "other"},
        {**good, "lease_id": None},
        outcome(result="spawned"),
    ]
    assert ns.refused_dispatches([*bad, good], REFUSED) == [good]


def test_first_refusal_is_retained(tmp_path: Path) -> None:
    first = record(tmp_path, [outcome()])[0]
    assert record(tmp_path, [{**outcome(), "detail": "different reason"}]) == []
    assert ns.not_started_of(tmp_path, 1) == first


@pytest.mark.parametrize("body", ["bad json", "[]"])
def test_unreadable_marker_is_unknown(tmp_path: Path, body: str) -> None:
    assert ns.not_started_of(tmp_path, 1) is None
    wd = tmp_path / "workers/000001"
    wd.mkdir(parents=True)
    (wd / ns.NOT_STARTED_FILE).write_text(body)
    assert ns.not_started_of(tmp_path, 1) is None


def test_refund_keeps_other_heads_and_sidecar_fields(tmp_path: Path) -> None:
    side = {
        "lease_window": {
            "example#1": [BEFORE, NOW],
            "example#2": [NOW],
            "example#3": [NOW],
        },
        "head_dispatches": {
            "example#1": {"head": "a" * 40, "n": 2, "at": NOW},
            "example#2": {"head": "a" * 40, "n": 1, "at": NOW},
            "example#3": {"head": "b" * 40, "n": 1, "at": NOW},
        },
        "no_result_inputs": {"example#4": {"input": "input"}},
    }
    (tmp_path / "sidecar.json").write_text(json.dumps(side))
    record(
        tmp_path,
        [
            outcome(),
            outcome(2, "example#2"),
            outcome(3, "example#3", "spawned"),
            outcome(4, "cause:example/repo:group", "claim-refused"),
        ],
    )
    after = json.loads((tmp_path / "sidecar.json").read_text())
    assert after["lease_window"] == {"example#1": [BEFORE], "example#3": [NOW]}
    assert after["head_dispatches"] == {
        "example#1": {"head": "a" * 40, "n": 1, "at": NOW},
        "example#3": side["head_dispatches"]["example#3"],
    }
    assert after["no_result_inputs"] == side["no_result_inputs"]


def test_no_refused_pr_leaves_sidecar_untouched(tmp_path: Path) -> None:
    rows = [
        outcome(result="spawned"),
        outcome(2, "cause:example/repo:group", "claim-refused"),
    ]
    assert not ns.refund_not_started(tmp_path, rows, facts(), now=NOW)
    record(tmp_path, rows)
    assert not (tmp_path / "sidecar.json").exists()


@pytest.mark.parametrize("body", ["bad json", "[]"])
def test_refund_recovers_unreadable_sidecar(tmp_path: Path, body: str) -> None:
    (tmp_path / "sidecar.json").write_text(body)
    assert ns.refund_not_started(tmp_path, [outcome()], facts(), now=NOW)
    assert json.loads((tmp_path / "sidecar.json").read_text()) == {
        "lease_window": {},
        "head_dispatches": {},
    }
