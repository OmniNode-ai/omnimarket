# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The beta board reads a probe's workflow runs from the source only when its list for the schedule slot is not cached (OMN-19998).

Golden (the failing control of "a second cycle with unchanged sources reads from the cache"): the first refresh runs `scm run-list` for
each probe that declares its schedule; the second, past the refresh interval and in the same slot, runs none and keeps the reads with
this cycle's read time. A new slot, a slot whose run has not finished and a failed read are read again; a probe with no schedule is read
every cycle. Error chain: a cache that cannot be reached still returns the reads; the overlay refuses a cron it cannot parse. The named
injected break (a slot that changes on every call) turns the golden assertion red.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_beta_board_effect.handlers import adapter_beta_board_sources
from omnimarket.nodes.node_beta_board_effect.handlers.adapter_beta_board_sources import (
    refresh_probes,
)
from omnimarket.nodes.node_beta_board_effect.handlers.handler_beta_board import (
    OWN_CONTRACT,
    HandlerBetaBoard,
    load_runtime,
)
from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelBetaBoardOverlay,
    ModelBetaBoardRequest,
    ModelBetaBoardRuntime,
    ModelSourceReads,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.handler_source_read_cache import (
    HandlerSourceReadCache,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.schedule_revision import (
    schedule_slot,
)
from tests.nodes.node_beta_board_effect.support import NOW, OVERLAY, run_list_answer
from tests.nodes.node_source_read_cache_effect.support import (
    PREFIX,
    TTL_BACKSTOP,
    DownBackend,
    memory_reader,
)

pytestmark = pytest.mark.unit

# The hourly slot before NOW (12:00) is 12:00 itself; the next is 13:00.
HOURLY = "0 * * * *"


def _runtime(
    cron: str | None = HOURLY, second_cron: str | None = None
) -> ModelBetaBoardRuntime:
    doc = json.loads(json.dumps(OVERLAY))
    doc["probes"][0]["cron"] = cron
    doc["probes"][1]["cron"] = second_cron
    for probe in doc["probes"]:
        if probe["cron"] is None:
            del probe["cron"]
    return load_runtime(
        OWN_CONTRACT, environ={}, overlay=ModelBetaBoardOverlay.model_validate(doc)
    )


class Runner:
    """Stands in for ``onex scm run-list``: one run finished a minute after the current hour (its slot); counts the calls."""

    def __init__(self, status: str = "completed", fail: bool = False) -> None:
        self.status = status
        self.fail = fail
        self.calls: list[str] = []
        self.now = NOW

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        self.calls.append(argv[argv.index("--workflow") + 1])
        if self.fail:
            return 1, "", "down"
        slot = self.now.replace(minute=0, second=0, microsecond=0)
        return (
            0,
            run_list_answer(
                [
                    (
                        self.status,
                        "success" if self.status == "completed" else "",
                        slot + timedelta(minutes=1),
                    )
                ]
            ),
            "",
        )


def _refresh(
    tmp_path: Path,
    runner: Runner,
    at: datetime,
    cache: CachedSourceReader | None,
    rt: ModelBetaBoardRuntime | None = None,
) -> ModelSourceReads:
    runner.now = at
    reads = refresh_probes(rt or _runtime(), tmp_path, at, runner, lambda: at, cache)
    path = tmp_path / (rt or _runtime()).reads_relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(reads.model_dump_json(), encoding="utf-8")
    return reads


def _past_interval(at: datetime) -> datetime:
    return at + timedelta(seconds=_runtime().probe_refresh_seconds + 1)


def test_golden_a_second_cycle_in_the_same_slot_reads_from_the_cache(
    tmp_path: Path,
) -> None:
    cache, _ = memory_reader()
    runner = Runner()
    at = NOW + timedelta(minutes=2)
    first = _refresh(tmp_path, runner, at, cache)
    assert sorted(runner.calls) == ["cloud.yml", "local.yml"]
    second_at = _past_interval(at)
    second = _refresh(tmp_path, runner, second_at, cache)
    assert runner.calls.count("local.yml") == 1, (
        "the cached probe was read from the source again"
    )
    assert runner.calls.count("cloud.yml") == 2, (
        "a probe with no schedule is read every cycle"
    )
    local_first = next(r for r in first.reads if r.source_id == "probe-local")
    local_second = next(r for r in second.reads if r.source_id == "probe-local")
    assert local_second.points == local_first.points
    assert local_second.read_at == second_at
    assert cache.tally.hits == 1


def test_a_new_schedule_slot_is_a_miss_and_is_read_again(tmp_path: Path) -> None:
    cache, _ = memory_reader()
    runner = Runner()
    at = NOW + timedelta(minutes=2)
    _refresh(tmp_path, runner, at, cache)
    _refresh(tmp_path, runner, at + timedelta(hours=1), cache)
    assert runner.calls.count("local.yml") == 2


def test_a_slot_whose_run_has_not_finished_is_read_again_every_cycle(
    tmp_path: Path,
) -> None:
    cache, backend = memory_reader()
    runner = Runner(status="in_progress")
    at = NOW + timedelta(minutes=2)
    _refresh(tmp_path, runner, at, cache)
    _refresh(tmp_path, runner, _past_interval(at), cache)
    assert runner.calls.count("local.yml") == 2
    assert backend.keys == []


def test_a_probe_with_no_schedule_is_read_every_cycle(tmp_path: Path) -> None:
    cache, _ = memory_reader()
    runner = Runner()
    at = NOW + timedelta(minutes=2)
    rt = _runtime(cron=None)
    _refresh(tmp_path, runner, at, cache, rt)
    _refresh(tmp_path, runner, _past_interval(at), cache, rt)
    assert runner.calls.count("local.yml") == 2
    assert cache.tally.hits == 0
    assert cache.tally.bypassed == 0


def test_error_chain_a_failed_read_is_never_cached(tmp_path: Path) -> None:
    cache, backend = memory_reader()
    at = NOW + timedelta(minutes=2)
    _refresh(tmp_path, Runner(fail=True), at, cache)
    assert backend.keys == []


def test_error_chain_an_unreachable_cache_still_returns_the_reads(
    tmp_path: Path,
) -> None:
    cache = CachedSourceReader(
        HandlerSourceReadCache(DownBackend(), PREFIX, TTL_BACKSTOP)
    )
    runner = Runner()
    reads = _refresh(tmp_path, runner, NOW + timedelta(minutes=2), cache)
    local = next(r for r in reads.reads if r.source_id == "probe-local")
    assert local.read == "read"
    assert local.points
    assert cache.tally.unavailable == 1


def test_the_overlay_refuses_a_cron_it_cannot_parse() -> None:
    doc = json.loads(json.dumps(OVERLAY))
    doc["probes"][0]["cron"] = "hourly"
    with pytest.raises(ValidationError):
        ModelBetaBoardOverlay.model_validate(doc)
    doc["probes"][0]["cron"] = "0 * * * *"
    assert ModelBetaBoardOverlay.model_validate(doc).probes[0].cron == "0 * * * *"


def test_named_injected_break_a_slot_that_changes_every_call_reads_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, _ = memory_reader()
    runner = Runner()
    at = NOW + timedelta(minutes=2)
    _refresh(tmp_path, runner, at, cache)
    real = schedule_slot
    counter = {"n": 0}

    def drifting(crons: Sequence[str], now: datetime) -> datetime | None:
        counter["n"] += 1
        base = real(crons, now)
        return base - timedelta(minutes=counter["n"]) if base else None

    monkeypatch.setattr(adapter_beta_board_sources, "schedule_slot", drifting)
    _refresh(tmp_path, runner, _past_interval(at), cache)
    assert runner.calls.count("local.yml") == 2


def test_the_build_stage_writes_the_cache_tally_receipt(tmp_path: Path) -> None:
    cache, _ = memory_reader()
    runner = Runner()
    handler = HandlerBetaBoard(
        runtime=_runtime(),
        run=runner,
        clock=lambda: NOW + timedelta(minutes=2),
        cache=cache,
    )
    runner.now = NOW + timedelta(minutes=2)
    result = handler.handle(ModelBetaBoardRequest(stage="build", home=str(tmp_path)))
    assert result.ok
    receipt = json.loads(
        (tmp_path / ".onex_state/source-cache/beta_board.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["backend"] == "memory"
    assert receipt["source_reads"] == 1


MISSING_REASON = (
    "GitHub GET /repos/x/actions/workflows/y/runs answered HTTP 404: Not Found"
)


class MissingRunner(Runner):
    """Answers HTTP 404 for the workflow named, as the source-control node does for a workflow GitHub does not have."""

    def __init__(self, missing: str) -> None:
        super().__init__()
        self.missing = missing

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        workflow = argv[argv.index("--workflow") + 1]
        if workflow == self.missing:
            self.calls.append(workflow)
            return (
                0,
                json.dumps(
                    {"outcome": "failed", "result": {"failure_reason": MISSING_REASON}}
                ),
                "",
            )
        return super().__call__(argv, timeout)


class Tip:
    def __init__(self) -> None:
        self.sha = "a" * 40
        self.reads = 0

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        self.reads += 1
        return 0, self.sha + "\n", ""


def test_a_missing_workflow_is_not_asked_again_until_the_default_branch_moves(
    tmp_path: Path,
) -> None:
    for name in ("localrepo", "cloudrepo"):
        (tmp_path / name).mkdir()
    cache, _ = memory_reader()
    runner = MissingRunner("cloud.yml")
    tip = Tip()
    rt = _runtime()
    at = NOW + timedelta(minutes=2)

    def cycle(moment: datetime, runner: Runner = runner) -> ModelSourceReads:
        runner.now = moment
        reads = refresh_probes(rt, tmp_path, moment, runner, lambda: moment, cache, tip)
        path = tmp_path / rt.reads_relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reads.model_dump_json(), encoding="utf-8")
        return reads

    first = cycle(at)
    cloud_first = next(r for r in first.reads if r.source_id == "probe-cloud")
    assert cloud_first.read == "unread"
    assert runner.calls.count("cloud.yml") == 1
    second_at = _past_interval(at)
    second = cycle(second_at)
    cloud_second = next(r for r in second.reads if r.source_id == "probe-cloud")
    assert runner.calls.count("cloud.yml") == 1, (
        "a missing workflow was asked about again"
    )
    assert cloud_second.read == "unread"
    assert cloud_second.reason == cloud_first.reason
    assert cloud_second.read_at == second_at
    tip.sha = "b" * 40
    cycle(_past_interval(second_at))
    assert runner.calls.count("cloud.yml") == 2, "a moved default branch was not a miss"
