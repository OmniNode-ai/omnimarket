"""Golden chain tests for node_duplication_sweep.

All checks operate on real filesystem under tmp_path — no DB, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_duplication_sweep.handlers.handler_duplication_sweep import (
    DuplicationSweepRequest,
    NodeDuplicationSweep,
    _check_d1_drizzle_tables,
    _check_d2_kafka_topics,
    _check_d4_model_names,
)

CMD_TOPIC = "onex.cmd.omnimarket.duplication-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.duplication-sweep-completed.v1"


@pytest.mark.unit
class TestDuplicationSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_d1_no_duplicates(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """D1 should PASS when no table name appears in more than one schema file."""
        shared_dir = tmp_path / "omnidash" / "shared"
        shared_dir.mkdir(parents=True)
        (shared_dir / "intel-schema.ts").write_text(
            'export const foo = pgTable("foo_table", {});\n'
        )
        (shared_dir / "other-schema.ts").write_text(
            'export const bar = pgTable("bar_table", {});\n'
        )

        result = _check_d1_drizzle_tables(str(tmp_path))

        assert result.status == "PASS"
        assert result.finding_count == 0

    async def test_d1_duplicate_detected(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """D1 should FAIL when the same table name appears in two schema files."""
        shared_dir = tmp_path / "omnidash" / "shared"
        shared_dir.mkdir(parents=True)
        (shared_dir / "a-schema.ts").write_text('pgTable("dup_table", {});\n')
        (shared_dir / "b-schema.ts").write_text('pgTable("dup_table", {});\n')

        result = _check_d1_drizzle_tables(str(tmp_path))

        assert result.status == "FAIL"
        assert result.finding_count >= 1
        assert any(f.name == "dup_table" for f in result.findings)

    async def test_d1_missing_dir(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """D1 should WARN when omnidash/shared does not exist."""
        result = _check_d1_drizzle_tables(str(tmp_path))
        assert result.status == "WARN"

    async def test_d2_no_conflicts(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """D2 should PASS when no topic is claimed by a conflicting producer."""
        topics_py = (
            tmp_path / "omniclaude" / "src" / "omniclaude" / "hooks" / "topics.py"
        )
        topics_py.parent.mkdir(parents=True)
        topics_py.write_text(
            'class TopicBase:\n    FOO = "onex.evt.omniclaude.foo.v1"\n'
        )

        boundaries_dir = tmp_path / "onex_change_control" / "boundaries"
        boundaries_dir.mkdir(parents=True)
        (boundaries_dir / "kafka_boundaries.yaml").write_text(
            "topics:\n"
            "  - topic_name: 'onex.evt.omniclaude.foo.v1'\n"
            "    producer_repo: 'omniclaude'\n"
        )

        result = _check_d2_kafka_topics(str(tmp_path))
        assert result.status == "PASS"

    async def test_d2_conflict_detected(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """D2 should FAIL when omniclaude claims a topic but boundaries.yaml names another producer."""
        topics_py = (
            tmp_path / "omniclaude" / "src" / "omniclaude" / "hooks" / "topics.py"
        )
        topics_py.parent.mkdir(parents=True)
        topics_py.write_text(
            'class TopicBase:\n    CONFLICT = "onex.evt.omniclaude.conflict.v1"\n'
        )

        boundaries_dir = tmp_path / "onex_change_control" / "boundaries"
        boundaries_dir.mkdir(parents=True)
        (boundaries_dir / "kafka_boundaries.yaml").write_text(
            "topics:\n"
            "  - topic_name: 'onex.evt.omniclaude.conflict.v1'\n    producer_repo: 'omniintelligence'\n"
        )

        result = _check_d2_kafka_topics(str(tmp_path))
        assert result.status == "FAIL"
        assert result.finding_count >= 1

    async def test_d4_no_collisions(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """D4 should PASS when model names are unique across repos."""
        for repo in ("repo_a", "repo_b"):
            src = tmp_path / repo / "src" / repo
            src.mkdir(parents=True)
        (tmp_path / "repo_a" / "src" / "repo_a" / "models.py").write_text(
            "class ModelFoo(BaseModel):\n    pass\n"
        )
        (tmp_path / "repo_b" / "src" / "repo_b" / "models.py").write_text(
            "class ModelBar(BaseModel):\n    pass\n"
        )

        result = _check_d4_model_names(str(tmp_path))
        assert result.status == "PASS"

    async def test_d4_collision_detected(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """D4 should FAIL when the same ModelXxx name appears in two repos."""
        for repo in ("repo_a", "repo_b"):
            src = tmp_path / repo / "src" / repo
            src.mkdir(parents=True)
            (src / "models.py").write_text("class ModelShared(BaseModel):\n    pass\n")

        result = _check_d4_model_names(str(tmp_path))
        assert result.status == "FAIL"
        assert any(f.name == "ModelShared" for f in result.findings)

    async def test_handler_runs_all_checks(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler should run all checks by default and return an overall status."""
        handler = NodeDuplicationSweep()
        request = DuplicationSweepRequest(omni_home=str(tmp_path))
        result = handler.handle(request)

        assert len(result.check_results) == 4
        check_ids = {r.check_id for r in result.check_results}
        assert check_ids == {"D1", "D2", "D3", "D4"}
        assert result.overall_status in ("PASS", "FAIL")

    async def test_handler_selective_checks(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler should only run specified checks."""
        handler = NodeDuplicationSweep()
        request = DuplicationSweepRequest(omni_home=str(tmp_path), checks=["D1"])
        result = handler.handle(request)

        assert len(result.check_results) == 1
        assert result.check_results[0].check_id == "D1"

    async def test_event_bus_wiring(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler can publish completion event to EventBusInmemory."""
        handler = NodeDuplicationSweep()
        events_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            request = DuplicationSweepRequest(
                omni_home=payload.get("omni_home", str(tmp_path)),
                checks=["D1"],
            )
            result = handler.handle(request)
            evt = {"overall_status": result.overall_status}
            events_captured.append(evt)
            await event_bus.publish(EVT_TOPIC, key=None, value=json.dumps(evt).encode())

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-dup-sweep"
        )

        cmd_payload = json.dumps({"omni_home": str(tmp_path)}).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(events_captured) == 1
        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_overall_fail_when_any_check_fails(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """overall_status should be FAIL when at least one check is FAIL."""
        shared_dir = tmp_path / "omnidash" / "shared"
        shared_dir.mkdir(parents=True)
        (shared_dir / "a-schema.ts").write_text('pgTable("dup", {});\n')
        (shared_dir / "b-schema.ts").write_text('pgTable("dup", {});\n')

        handler = NodeDuplicationSweep()
        request = DuplicationSweepRequest(omni_home=str(tmp_path), checks=["D1"])
        result = handler.handle(request)

        assert result.overall_status == "FAIL"
