"""Golden chain tests for node_duplication_sweep.

All checks operate on real filesystem under tmp_path — no DB, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_duplication_sweep.handlers import handler_duplication_sweep
from omnimarket.nodes.node_duplication_sweep.handlers.handler_duplication_sweep import (
    _KAFKA_BOUNDARIES_YAML,
    DuplicationSweepRequest,
    NodeDuplicationSweep,
    _check_d1_drizzle_tables,
    _check_d2_kafka_topics,
    _check_d3_migration_prefixes,
    _check_d4_model_names,
    _migration_conflict_repos,
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
        self,
        event_bus: EventBusInmemory,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D2 should PASS when no topic is claimed by a conflicting producer."""
        topics_py = (
            tmp_path / "omniclaude" / "src" / "omniclaude" / "hooks" / "topics.py"
        )
        topics_py.parent.mkdir(parents=True)
        topics_py.write_text(
            'class TopicBase:\n    FOO = "onex.evt.omniclaude.foo.v1"\n'
        )
        manifest = tmp_path / "planted" / "kafka_boundaries.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "topics:\n"
            "  - topic_name: 'onex.evt.omniclaude.foo.v1'\n"
            "    producer_repo: 'omniclaude'\n"
        )

        monkeypatch.setattr(
            handler_duplication_sweep, "_KAFKA_BOUNDARIES_YAML", manifest
        )
        result = _check_d2_kafka_topics(str(tmp_path))
        assert result.status == "PASS"

    async def test_d2_conflict_detected(
        self,
        event_bus: EventBusInmemory,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D2 should FAIL when omniclaude claims a topic but the manifest names another producer."""
        topics_py = (
            tmp_path / "omniclaude" / "src" / "omniclaude" / "hooks" / "topics.py"
        )
        topics_py.parent.mkdir(parents=True)
        topics_py.write_text(
            'class TopicBase:\n    CONFLICT = "onex.evt.omniclaude.conflict.v1"\n'
        )
        manifest = tmp_path / "planted" / "kafka_boundaries.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "topics:\n"
            "  - topic_name: 'onex.evt.omniclaude.conflict.v1'\n    producer_repo: 'omniintelligence'\n"
        )

        monkeypatch.setattr(
            handler_duplication_sweep, "_KAFKA_BOUNDARIES_YAML", manifest
        )
        result = _check_d2_kafka_topics(str(tmp_path))
        assert result.status == "FAIL"
        assert result.finding_count >= 1

    async def test_d2_bundled_manifest_lives_beside_the_node(
        self, event_bus: EventBusInmemory
    ) -> None:
        """OMN-20949 AC1: the manifest ships beside node_duplication_sweep and parses."""
        assert _KAFKA_BOUNDARIES_YAML.name == "kafka_boundaries.yaml"
        assert _KAFKA_BOUNDARIES_YAML.parent.parent.name == "node_duplication_sweep"
        assert _KAFKA_BOUNDARIES_YAML.is_file()
        data = yaml.safe_load(_KAFKA_BOUNDARIES_YAML.read_text(encoding="utf-8"))
        entries = data["boundaries"]
        assert entries
        for entry in entries:
            assert entry["topic_name"].startswith("onex.")
            assert entry["producer_repo"]
            assert entry["consumer_repo"]

    async def test_d2_manifest_path_named_for_infra_is_the_one_d2_reads(
        self, event_bus: EventBusInmemory
    ) -> None:
        """OMN-20949 AC3: the repo-relative path omnibase_infra's Duplication
        Sweep reads is the manifest D2 reads, and D2 names no change-control path."""
        infra_path = Path(
            "src/omnimarket/nodes/node_duplication_sweep/contracts/kafka_boundaries.yaml"
        )
        repo_root = Path(__file__).resolve().parent.parent
        assert (repo_root / infra_path).resolve() == _KAFKA_BOUNDARIES_YAML.resolve()
        assert handler_duplication_sweep._KAFKA_BOUNDARIES_LABEL.endswith(
            infra_path.as_posix()
        )
        assert "onex_change_control" not in str(_KAFKA_BOUNDARIES_YAML)

    async def test_d2_bundled_manifest_lists_no_retired_change_control_boundary(
        self, event_bus: EventBusInmemory
    ) -> None:
        """OMN-20074: onex_change_control's contract-drift-detected boundary has
        a producer only and no subscriber anywhere, so the bundled manifest
        carries no boundary that names onex_change_control."""
        data = yaml.safe_load(_KAFKA_BOUNDARIES_YAML.read_text(encoding="utf-8"))
        entries = data["boundaries"]
        assert entries
        retired = [
            entry["topic_name"]
            for entry in entries
            if "onex_change_control" in (entry["producer_repo"], entry["consumer_repo"])
            or "contract-drift-detected" in entry["topic_name"]
        ]
        assert retired == []

    async def test_d2_reads_bundled_manifest_without_occ_checkout(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """OMN-20949 AC2: D2 reads the manifest beside the node, with no
        change-control checkout in the workspace."""
        topics_py = (
            tmp_path / "omniclaude" / "src" / "omniclaude" / "hooks" / "topics.py"
        )
        topics_py.parent.mkdir(parents=True)
        topics_py.write_text(
            'class TopicBase:\n    FOO = "onex.evt.omniclaude.foo.v1"\n'
        )
        assert not (tmp_path / "onex_change_control").exists()

        result = _check_d2_kafka_topics(str(tmp_path))

        assert result.status == "PASS", result.detail

    async def test_d2_default_manifest_is_the_node_constant(
        self,
        event_bus: EventBusInmemory,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OMN-20949 AC2: the default read path is the node's manifest constant,
        resolved at call time; a conflicting producer planted there FAILs."""
        topics_py = (
            tmp_path / "omniclaude" / "src" / "omniclaude" / "hooks" / "topics.py"
        )
        topics_py.parent.mkdir(parents=True)
        topics_py.write_text(
            'class TopicBase:\n    CONFLICT = "onex.evt.omniclaude.conflict.v1"\n'
        )
        manifest = tmp_path / "planted" / "kafka_boundaries.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "topics:\n"
            "  - topic_name: 'onex.evt.omniclaude.conflict.v1'\n"
            "    producer_repo: 'omniintelligence'\n"
        )
        monkeypatch.setattr(
            handler_duplication_sweep, "_KAFKA_BOUNDARIES_YAML", manifest
        )

        result = _check_d2_kafka_topics(str(tmp_path))

        assert result.status == "FAIL"
        assert [f.name for f in result.findings] == ["onex.evt.omniclaude.conflict.v1"]

    async def test_d3_reports_a_name_conflict_across_repos(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """D3 finds a table two repos create with different columns, in process."""
        for repo, columns in (
            ("repo_a", "\n  id INT,\n  a TEXT\n"),
            ("repo_b", "\n  id INT\n"),
        ):
            (tmp_path / repo / ".git").mkdir(parents=True)
            migration = tmp_path / repo / "migrations" / "001_init.sql"
            migration.parent.mkdir(parents=True)
            migration.write_text(f"CREATE TABLE d3_probe ({columns});\n")

        result = _check_d3_migration_prefixes(str(tmp_path))

        assert result.status == "FAIL"
        assert result.finding_count == 1
        assert result.findings[0].name == "NAME_CONFLICT: table `d3_probe`"
        assert sorted(result.findings[0].locations) == [
            "repo_a/migrations/001_init.sql",
            "repo_b/migrations/001_init.sql",
        ]

    async def test_d3_passes_when_each_table_is_created_once(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Positive control: distinct tables are not a conflict."""
        for repo in ("repo_a", "repo_b"):
            (tmp_path / repo / ".git").mkdir(parents=True)
            migration = tmp_path / repo / "migrations" / "001_init.sql"
            migration.parent.mkdir(parents=True)
            migration.write_text(f"CREATE TABLE {repo}_only (id INT);\n")

        result = _check_d3_migration_prefixes(str(tmp_path))

        assert result.status == "PASS"
        assert result.finding_count == 0

    async def test_d3_never_runs_a_change_control_checkout(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """D3 runs omnibase_core's migration-conflict handler, not a command or
        suppressions file found in a change-control clone beside the repos."""
        occ_root = tmp_path / "onex_change_control"
        command = occ_root / ".venv" / "bin" / "check-migration-conflicts"
        command.parent.mkdir(parents=True)
        command.write_text(
            "#!/usr/bin/env python3\nprint('NAME_CONFLICT from a change-control clone')\n"
        )
        command.chmod(0o755)
        (occ_root / "migration_conflict_suppressions.yaml").write_text(
            "suppressions:\n  - {table: d3_probe}\n"
        )
        for repo, columns in (
            ("repo_a", "\n  id INT,\n  a TEXT\n"),
            ("repo_b", "\n  id INT\n"),
        ):
            (tmp_path / repo / ".git").mkdir(parents=True)
            migration = tmp_path / repo / "migrations" / "001_init.sql"
            migration.parent.mkdir(parents=True)
            migration.write_text(f"CREATE TABLE d3_probe ({columns});\n")

        result = _check_d3_migration_prefixes(str(tmp_path))

        assert [f.name for f in result.findings] == ["NAME_CONFLICT: table `d3_probe`"]

    async def test_d3_repo_resolution_excludes_generated_worktrees(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """D3 should pass canonical repos instead of scanning generated worktrees."""
        for repo in ("omnimarket", "onex_change_control"):
            git_dir = tmp_path / repo / ".git"
            git_dir.mkdir(parents=True)
        generated_git_dir = (
            tmp_path / "omni_worktrees" / "OMN-1" / "omnimarket" / ".git"
        )
        generated_git_dir.mkdir(parents=True)

        assert _migration_conflict_repos(str(tmp_path)) == [
            "omnimarket",
            "onex_change_control",
        ]

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
