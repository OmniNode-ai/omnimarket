# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Layer-2 probe feeds the lane at the rate the lane serves (OMN-18349).

Every test here was RED before the change it pins, against a fake broker and a
fake projection connection, so none of them needs a lane. The behaviours are the
three the 2026-09-14 run (34810939133) exposed, measured from its own
projection rows on the stability-test lane:

  1. the corpus was published all at once, and the third case onward were each
     cancelled at an exact 240.000s cadence having spent their whole execution
     budget behind abandoned work;
  2. a cancelled delegation projected a row that `evaluate_row` scored as a
     COMPLETED row with missing telemetry -- a description of a regression that
     was not happening;
  3. one such row (`0df23c72`) was superseded 12.9 seconds later by the real
     terminal carrying model and tokens, after the probe had already scored it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.delegation_golden import runner as runner_module
from tests.delegation_golden.corpus_loader import ModelCorpusCase, load_corpus


@dataclass
class FakeBroker:
    """Records what the probe published, and when relative to what it drained."""

    published: list[str] = field(default_factory=list)
    events: list[tuple[str, str]] = field(default_factory=list)

    def publish(self, case_id: str) -> None:
        self.published.append(case_id)
        self.events.append(("publish", case_id))

    def drain(self, case_id: str) -> None:
        self.events.append(("drain", case_id))


@dataclass
class FakeConn:
    """A projection whose rows appear only once the probe has waited for them."""

    rows: dict[str, dict[str, Any]]
    broker: FakeBroker | None = None
    closed: bool = False
    fetch_calls: int = 0

    async def fetch(self, _query: str, correlation_ids: list[str]) -> list[Any]:
        self.fetch_calls += 1
        found = []
        for correlation_id in correlation_ids:
            row = self.rows.get(correlation_id)
            if row is not None:
                found.append(row)
                if self.broker is not None:
                    self.broker.drain(str(row.get("case_id", correlation_id)))
        return found

    async def fetchrow(self, _query: str, correlation_id: str) -> Any:
        return self.rows.get(correlation_id)

    async def close(self) -> None:
        self.closed = True


def _completed_row(correlation_id: str, case_id: str) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "case_id": case_id,
        "timestamp": "2026-09-14T05:50:51.040527+00:00",
        "delegated_to": "Qwen3.6-35B-A3B",
        "model_name": "Qwen3.6-35B-A3B",
        "tokens_input": 61,
        "tokens_output": 668,
        "cost_usd": 0.0,
        "quality_gate_passed": True,
        "quality_gate_detail": "completed",
        "terminal_ok": True,
    }


# Verbatim from the stability-test lane, correlation id 68e4035d, 06:00:37.88Z.
BUDGET_TIMEOUT_DETAIL = (
    "delegation exceeded the handler execution budget of 240s and was "
    "cancelled; the consumer commits this terminal instead of being evicted "
    "mid-handle (OMN-15504)"
)


def _budget_timeout_row(correlation_id: str, case_id: str) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "case_id": case_id,
        "timestamp": "2026-09-14T06:00:37.876646+00:00",
        "delegated_to": "delegate-skill",
        "model_name": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "quality_gate_passed": False,
        "quality_gate_detail": BUDGET_TIMEOUT_DETAIL,
        "terminal_ok": False,
    }


@pytest.mark.unit
class TestLaneServingConcurrencyIsRead:
    """The pace is a contract fact, never a literal in the probe."""

    def test_entry_tier_concurrency_comes_from_the_overlay(self) -> None:
        import yaml

        raw = yaml.safe_load(
            runner_module._BIFROST_DELEGATION_CONFIG.read_text(encoding="utf-8")
        )
        declared = next(
            rule["max_concurrent_generations"]
            for rule in raw["saturation_policy"]["tiers"]
            if rule["tier"] == "local"
        )
        assert runner_module.lane_serving_concurrency() == declared

    def test_an_undeclared_concurrency_refuses_rather_than_defaulting(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falsification control: remove the declaration, get a refusal.

        A probe that fell back to a built-in number here would be guessing the
        one fact this function exists to stop it guessing.
        """
        import yaml

        raw = yaml.safe_load(
            runner_module._BIFROST_DELEGATION_CONFIG.read_text(encoding="utf-8")
        )
        for rule in raw["saturation_policy"]["tiers"]:
            rule.pop("max_concurrent_generations", None)
        stripped = tmp_path / "bifrost_delegation.yaml"
        stripped.write_text(yaml.safe_dump(raw), encoding="utf-8")
        monkeypatch.setattr(runner_module, "_BIFROST_DELEGATION_CONFIG", stripped)
        with pytest.raises(runner_module.LaneConcurrencyUndeclaredError):
            runner_module.lane_serving_concurrency()


@pytest.mark.unit
class TestPerCaseDeadlineReadsTheBoundThatBinds:
    """The deadline comes from the node that serves the probe, not another one."""

    def test_deadline_is_the_handler_execution_budget_plus_margin(self) -> None:
        assert (
            runner_module.per_case_timeout_s()
            == float(runner_module.declared_handler_budget_s())
            + runner_module.projection_margin_s()
        )

    def test_deadline_is_not_the_other_node_completion_bound(self) -> None:
        """The regression this replaces: patience taken from a different contract.

        ``node_delegation_orchestrator`` declares a 900s completion bound. It is
        a real bound for a real node, and it is not the node that terminalises a
        case published to the delegate-skill command topic.
        """
        from omnimarket.cloud.completion_bound import read_declared_completion_bound

        other_node_bound = float(read_declared_completion_bound().max_wall_seconds)
        assert runner_module.declared_handler_budget_s() < other_node_bound
        assert runner_module.per_case_timeout_s() < other_node_bound

    def test_explicit_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEX_E2E_POLL_TIMEOUT_S", "7")
        assert runner_module.per_case_timeout_s() == 7.0

    def test_ceiling_is_waves_times_the_per_case_wall(self) -> None:
        concurrency = runner_module.lane_serving_concurrency()
        per_wave = (
            runner_module.per_case_timeout_s() + runner_module.projection_margin_s()
        )
        waves = -(-9 // concurrency)
        assert runner_module.corpus_wall_clock_ceiling_s(9) == waves * per_wave


@pytest.mark.unit
class TestCorpusIsPublishedAtTheLaneRate:
    """No case is published while a previous wave is still outstanding."""

    def test_waves_are_sized_to_the_declared_concurrency(self) -> None:
        cases = list(load_corpus().integration_cases())
        waves = runner_module.corpus_waves(
            cases, runner_module.lane_serving_concurrency()
        )
        assert sum(len(wave) for wave in waves) == len(cases)
        assert all(
            len(wave) <= runner_module.lane_serving_concurrency() for wave in waves
        )
        assert [case.id for wave in waves for case in wave] == [
            case.id for case in cases
        ]

    def test_publish_and_drain_strictly_interleave(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect, as a sequence: nine publishes then one drain.

        RED before the change -- the old ``run_corpus`` emitted every publish
        before its first fetch, so this assertion saw
        ``publish x9, drain x9`` instead of the alternating order a
        concurrency-1 lane requires.
        """
        broker = FakeBroker()
        cases = list(load_corpus().integration_cases())
        rows_by_case = {case.id: _completed_row("", case.id) for case in cases}
        conn = FakeConn(rows={}, broker=broker)

        async def fake_publish(
            _topic: str, case: ModelCorpusCase, correlation_id: str
        ) -> None:
            broker.publish(case.id)
            row = dict(rows_by_case[case.id])
            row["correlation_id"] = correlation_id
            conn.rows[correlation_id] = row

        monkeypatch.setattr(runner_module, "publish_case", fake_publish)
        monkeypatch.setattr(runner_module, "_command_topic", lambda: "topic")

        async def fake_connect() -> Any:
            return conn

        monkeypatch.setattr(runner_module, "connect_lane_postgres", fake_connect)

        scoreboard = asyncio.run(runner_module.run_corpus())

        assert len(scoreboard.results) == len(cases)
        assert conn.closed is True
        concurrency = runner_module.lane_serving_concurrency()
        outstanding = 0
        peak = 0
        for kind, _case_id in broker.events:
            outstanding += 1 if kind == "publish" else -1
            peak = max(peak, outstanding)
        assert peak <= concurrency, (
            f"the probe had {peak} cases in flight against a lane that declares "
            f"{concurrency}; {broker.events}"
        )


@pytest.mark.unit
class TestCancelledDelegationsAreNamedAsThemselves:
    """A delegation the platform cancelled is not a completed row with no data."""

    def test_a_budget_timeout_row_is_terminal_timeout_not_completed(self) -> None:
        row = _budget_timeout_row("68e4035d", "I6")
        assert runner_module.is_budget_timeout_row(row) is True
        assert runner_module.row_terminal(row) == "timeout"

    def test_an_ordinary_row_still_reads_completed(self) -> None:
        """Positive control: the classifier is not simply answering 'timeout'."""
        row = _completed_row("4752582a", "I1")
        assert runner_module.is_budget_timeout_row(row) is False
        assert runner_module.row_terminal(row) == "completed"

    def test_the_failure_names_the_budget_not_the_token_counts(self) -> None:
        """The misreport: 'tokens: expected positive, got input=0 output=0'.

        That sentence describes a telemetry-drop regression (the OMN-13535
        shape). Five of the nine cases on run 34810939133 were reported that way
        and none of them was that defect: each was a delegation the platform had
        cancelled on its own execution budget before any attempt completed.
        """
        case = next(c for c in load_corpus().integration_cases() if c.id == "I9")
        failures = runner_module.evaluate_row(case, _budget_timeout_row("x", "I9"))
        assert failures, "a cancelled delegation is still a failing case"
        assert "delegation timeout" in failures[0]
        assert "handler execution budget" in failures[0]
        assert not any("tokens: expected positive" in f for f in failures), failures


@pytest.mark.unit
class TestSupersededRowsAreReadAtTheirFinalValue:
    """delegation_events is an upsert; the first row seen is not always the last."""

    def test_a_budget_timeout_row_is_given_its_settle_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verbatim from the lane: 0df23c72 cancelled 05:56:37.886Z, real 05:56:50.790Z.

        RED before the change -- the probe returned the first row it saw and
        scored the case against a row that no longer existed 13 seconds later.
        """
        correlation_id = "0df23c72-26de-44d4-92da-5720e2608b01"
        timeout_row = _budget_timeout_row(correlation_id, "I7")
        real_row = _completed_row(correlation_id, "I7")
        real_row["timestamp"] = "2026-09-14T05:56:50.790116+00:00"
        real_row["tokens_input"] = 77
        real_row["tokens_output"] = 1819
        real_row["quality_gate_detail"] = ""

        conn = FakeConn(rows={correlation_id: timeout_row})
        monkeypatch.setattr(runner_module, "POLL_INTERVAL_S", 0.0)

        async def drive() -> dict[str, Any]:
            async def supersede() -> None:
                await asyncio.sleep(0)
                conn.rows[correlation_id] = real_row

            settle = asyncio.ensure_future(
                runner_module.settle_row(conn, correlation_id, timeout_row, settle=5.0)
            )
            await supersede()
            return await settle

        settled = asyncio.run(drive())
        assert runner_module.row_terminal(settled) == "completed"
        assert settled["model_name"] == "Qwen3.6-35B-A3B"
        assert settled["tokens_output"] == 1819

    def test_a_row_that_is_never_superseded_returns_as_it_arrived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control: the settle window ends, it does not hang or invent."""
        correlation_id = "68e4035d"
        timeout_row = _budget_timeout_row(correlation_id, "I6")
        conn = FakeConn(rows={correlation_id: timeout_row})
        monkeypatch.setattr(runner_module, "POLL_INTERVAL_S", 0.0)
        settled = asyncio.run(
            runner_module.settle_row(conn, correlation_id, timeout_row, settle=0.05)
        )
        assert settled == timeout_row

    def test_a_completed_row_is_not_settled_at_all(self) -> None:
        """A settle window on every row would add one margin per case for nothing."""
        correlation_id = "4752582a"
        row = _completed_row(correlation_id, "I1")
        conn = FakeConn(rows={correlation_id: row})
        settled = asyncio.run(
            runner_module.settle_row(conn, correlation_id, row, settle=30.0)
        )
        assert settled is row
        assert conn.fetch_calls == 0
