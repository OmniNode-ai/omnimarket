"""Golden chain tests for node_environment_health_scanner.

Covers: model instantiation, all 7 probers, handler wiring, and EventBusInmemory round-trip.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_environment_health_scanner.handlers.handler_environment_health_scanner import (
    EnumHealthFindingSeverity,
    EnumSubsystem,
    EnvironmentHealthRequest,
    EnvironmentHealthResult,
    ModelHealthFinding,
    ModelSubsystemResult,
    NodeEnvironmentHealthScanner,
)
from omnimarket.nodes.node_environment_health_scanner.handlers.prober_containers import (
    _parse_docker_ps_json,
    probe_containers,
)
from omnimarket.nodes.node_environment_health_scanner.handlers.prober_emit_daemon import (
    probe_emit_daemon,
)
from omnimarket.nodes.node_environment_health_scanner.handlers.prober_entry_points import (
    _try_import_handler,
    probe_entry_points,
)
from omnimarket.nodes.node_environment_health_scanner.handlers.prober_hooks import (
    probe_hooks,
)
from omnimarket.nodes.node_environment_health_scanner.handlers.prober_kafka import (
    _parse_rpk_group_list,
    _parse_rpk_topic_list,
    probe_kafka,
)
from omnimarket.nodes.node_environment_health_scanner.handlers.prober_model_endpoints import (
    ModelEndpointSpec,
    probe_model_endpoints,
)
from omnimarket.nodes.node_environment_health_scanner.handlers.prober_projections import (
    ModelProjectionSpec,
    probe_projections,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)

CMD_TOPIC = "onex.cmd.omnimarket.environment-health-scan-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.environment-health-scan-completed.v1"


# ---------------------------------------------------------------------------
# Task 2: Model instantiation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_health_finding_fields() -> None:
    f = ModelHealthFinding(
        subsystem=EnumSubsystem.KAFKA,
        severity=EnumHealthFindingSeverity.FAIL,
        subject="onex.evt.foo.v1",
        message="topic has no consumer group",
        evidence="rpk topic describe returned empty groups",
    )
    assert f.subsystem == EnumSubsystem.KAFKA
    assert f.severity == EnumHealthFindingSeverity.FAIL


@pytest.mark.unit
def test_model_subsystem_result_fields() -> None:
    r = ModelSubsystemResult(
        subsystem=EnumSubsystem.HOOKS,
        status=EnumReadinessStatus.WARN,
        check_count=5,
        findings=[],
        evidence_source="$ONEX_STATE_DIR/hooks/logs/violations.log",
    )
    assert r.status == EnumReadinessStatus.WARN
    assert r.check_count == 5


@pytest.mark.unit
def test_environment_health_request_defaults() -> None:
    req = EnvironmentHealthRequest()
    assert req.subsystems == []


@pytest.mark.unit
def test_environment_health_result_overall_pass() -> None:
    result = EnvironmentHealthResult(
        subsystem_results=[],
        findings=[],
        overall=EnumReadinessStatus.PASS,
    )
    assert result.overall == EnumReadinessStatus.PASS


# ---------------------------------------------------------------------------
# Task 3: Prober 1 — Emit daemon
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_emit_daemon_missing_socket(tmp_path: Path) -> None:
    result = probe_emit_daemon(
        socket_path=str(tmp_path / "nonexistent.sock"),
        log_dir=str(tmp_path),
        ssh_target=None,
    )
    assert result.status.value in ("WARN", "FAIL")
    assert any("socket" in f.message.lower() for f in result.findings)


@pytest.mark.unit
def test_emit_daemon_socket_exists(tmp_path: Path) -> None:
    sock = tmp_path / "emit.sock"
    sock.touch()
    result = probe_emit_daemon(
        socket_path=str(sock),
        log_dir=str(tmp_path),
        ssh_target=None,
    )
    # Socket exists but no logs -> WARN, not FAIL
    assert result.status.value != "FAIL" or any(
        f.subject == "socket" for f in result.findings
    )


# ---------------------------------------------------------------------------
# Task 4: Prober 2 — Hooks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hooks_no_log_dir(tmp_path: Path) -> None:
    result = probe_hooks(log_dir=str(tmp_path / "nonexistent"))
    assert result.status.value in ("WARN", "FAIL")


@pytest.mark.unit
def test_hooks_clean_log(tmp_path: Path) -> None:
    log_dir = tmp_path / "hooks" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "violations.log").write_text("[]")
    result = probe_hooks(log_dir=str(log_dir))
    assert result.status.value == "PASS"
    assert result.check_count >= 1


@pytest.mark.unit
def test_hooks_high_error_rate(tmp_path: Path) -> None:
    log_dir = tmp_path / "hooks" / "logs"
    log_dir.mkdir(parents=True)
    summary = {"pre_tool_use": {"total": 100, "errors": 10}}
    (log_dir / "violations_summary.json").write_text(json.dumps(summary))
    result = probe_hooks(log_dir=str(log_dir))
    assert result.status.value in ("WARN", "FAIL")
    assert any("pre_tool_use" in f.subject for f in result.findings)


# ---------------------------------------------------------------------------
# Task 5: Prober 3 — Kafka
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_rpk_topic_list_parses_names() -> None:
    raw = "onex.evt.foo.v1\nonex.cmd.bar.v1\n"
    topics = _parse_rpk_topic_list(raw)
    assert "onex.evt.foo.v1" in topics
    assert "onex.cmd.bar.v1" in topics


@pytest.mark.unit
def test_parse_rpk_group_list_parses_groups() -> None:
    raw = "GROUP                              COORDINATOR  MEMBERS\nlocal.runtime.foo.consume.v1   0            1\n"
    groups = _parse_rpk_group_list(raw)
    assert "local.runtime.foo.consume.v1" in groups


@pytest.mark.unit
def test_probe_kafka_missing_topics_flagged() -> None:
    result = probe_kafka(
        declared_topics=["onex.evt.missing.v1"],
        existing_topics=[],
        consumer_groups=[],
        ssh_target=None,
    )
    assert result.status.value == "FAIL"
    assert any("onex.evt.missing.v1" in f.subject for f in result.findings)


@pytest.mark.unit
def test_probe_kafka_topic_exists_no_consumer_warn() -> None:
    result = probe_kafka(
        declared_topics=["onex.evt.orphan.v1"],
        existing_topics=["onex.evt.orphan.v1"],
        consumer_groups=[],
        ssh_target=None,
    )
    assert result.status.value in ("WARN", "FAIL")
    assert any("consumer" in f.message.lower() for f in result.findings)


@pytest.mark.unit
def test_probe_kafka_healthy() -> None:
    # Consumer group name contains the topic fragment "evt-good" (matching the transform logic)
    result = probe_kafka(
        declared_topics=["onex.evt.good.v1"],
        existing_topics=["onex.evt.good.v1"],
        consumer_groups=["local.runtime.evt-good.consume.v1"],
        ssh_target=None,
    )
    assert result.status.value == "PASS"


# ---------------------------------------------------------------------------
# Task 6: Prober 4 — Containers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_docker_ps_json_healthy() -> None:
    rows = [
        {
            "Names": "omninode-runtime",
            "Status": "Up 2 hours (healthy)",
            "State": "running",
        }
    ]
    parsed = _parse_docker_ps_json(json.dumps(rows))
    assert parsed[0]["name"] == "omninode-runtime"
    assert parsed[0]["healthy"] is True
    assert parsed[0]["running"] is True


@pytest.mark.unit
def test_probe_containers_missing_container_fail() -> None:
    result = probe_containers(
        expected_containers=["omninode-runtime"],
        running_containers=[],
        ssh_target=None,
    )
    assert result.status.value == "FAIL"
    assert any("omninode-runtime" in f.subject for f in result.findings)


@pytest.mark.unit
def test_probe_containers_unhealthy_warn() -> None:
    result = probe_containers(
        expected_containers=["omninode-runtime"],
        running_containers=[
            {
                "name": "omninode-runtime",
                "healthy": False,
                "running": True,
                "restart_count": 0,
            }
        ],
        ssh_target=None,
    )
    assert result.status.value in ("WARN", "FAIL")
    assert any("unhealthy" in f.message.lower() for f in result.findings)


@pytest.mark.unit
def test_probe_containers_all_healthy() -> None:
    result = probe_containers(
        expected_containers=["omninode-runtime"],
        running_containers=[
            {
                "name": "omninode-runtime",
                "healthy": True,
                "running": True,
                "restart_count": 0,
            }
        ],
        ssh_target=None,
    )
    assert result.status.value == "PASS"


# ---------------------------------------------------------------------------
# Task 7: Prober 5 — Projections
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_projection_staleness_fresh() -> None:
    now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    result = probe_projections(
        specs=[
            ModelProjectionSpec(
                table_name="registration_projections",
                max_freshness_seconds=3600,
                row_count=42,
                last_updated=datetime(2026, 4, 10, 11, 30, 0, tzinfo=UTC),
            )
        ],
        now=now,
    )
    assert result.status.value == "PASS"


@pytest.mark.unit
def test_projection_staleness_stale_warn() -> None:
    now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    result = probe_projections(
        specs=[
            ModelProjectionSpec(
                table_name="registration_projections",
                max_freshness_seconds=3600,
                row_count=10,
                last_updated=datetime(2026, 4, 10, 9, 0, 0, tzinfo=UTC),
            )
        ],
        now=now,
    )
    assert result.status.value in ("WARN", "FAIL")
    assert any("registration_projections" in f.subject for f in result.findings)


@pytest.mark.unit
def test_projection_empty_table_warn() -> None:
    now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    result = probe_projections(
        specs=[
            ModelProjectionSpec(
                table_name="session_outcomes",
                max_freshness_seconds=86400,
                row_count=0,
                last_updated=None,
            )
        ],
        now=now,
    )
    assert result.status.value in ("WARN", "FAIL")


# ---------------------------------------------------------------------------
# Task 8: Prober 6 — Entry-points
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_try_import_handler_valid() -> None:
    ok, error = _try_import_handler(
        "omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep",
        "NodeRuntimeSweep",
    )
    assert ok is True
    assert error == ""


@pytest.mark.unit
def test_try_import_handler_bad_module() -> None:
    ok, error = _try_import_handler("omnimarket.does_not_exist", "SomeClass")
    assert ok is False
    assert "ModuleNotFoundError" in error or "No module" in error


@pytest.mark.unit
def test_probe_entry_points_detects_broken() -> None:
    result = probe_entry_points(
        entry_point_specs=[
            {
                "name": "node_broken",
                "module": "omnimarket.nonexistent",
                "class": "Handler",
            },
        ]
    )
    assert result.status.value == "FAIL"
    assert any("node_broken" in f.subject for f in result.findings)


@pytest.mark.unit
def test_probe_entry_points_all_clean() -> None:
    result = probe_entry_points(
        entry_point_specs=[
            {
                "name": "node_runtime_sweep",
                "module": "omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep",
                "class": "NodeRuntimeSweep",
            },
        ]
    )
    assert result.status.value == "PASS"
    assert result.check_count == 1


# ---------------------------------------------------------------------------
# Task 9: Prober 7 — Model endpoints
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_probe_model_endpoints_no_specs() -> None:
    result = probe_model_endpoints(specs=[])
    assert result.status.value == "PASS"
    assert result.check_count == 0
    assert result.valid_zero is True


@pytest.mark.unit
def test_probe_model_endpoints_unreachable() -> None:
    result = probe_model_endpoints(
        specs=[ModelEndpointSpec(env_var="LLM_TEST_URL", url="http://127.0.0.1:19999")],
        timeout_seconds=1,
    )
    assert result.status.value == "FAIL"
    assert any("LLM_TEST_URL" in f.subject for f in result.findings)


@pytest.mark.unit
def test_probe_model_endpoints_mock_success() -> None:
    class MockResponse:
        status_code = 200

        def json(self) -> dict:
            return {"data": [{"id": "qwen3-coder-30b"}]}

    with patch("httpx.get", return_value=MockResponse()):
        result = probe_model_endpoints(
            specs=[ModelEndpointSpec(env_var="LLM_CODE_URL", url="http://fake:8000")],
        )
    assert result.status.value == "PASS"
    assert result.check_count == 1


# ---------------------------------------------------------------------------
# Task 10: Handler wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handler_returns_result_structure() -> None:
    handler = NodeEnvironmentHealthScanner()
    req = EnvironmentHealthRequest(
        subsystems=["entry_points"],
        omni_home="",
        ssh_target=None,
    )
    result = handler.handle(req)
    assert result.overall in (
        EnumReadinessStatus.PASS,
        EnumReadinessStatus.WARN,
        EnumReadinessStatus.FAIL,
    )
    assert isinstance(result.subsystem_results, list)
    assert len(result.subsystem_results) >= 1


@pytest.mark.unit
def test_handler_overall_fail_when_any_subsystem_fails() -> None:
    handler = NodeEnvironmentHealthScanner()
    fail_result = ModelSubsystemResult(
        subsystem=EnumSubsystem.KAFKA,
        status=EnumReadinessStatus.FAIL,
        check_count=1,
        findings=[
            ModelHealthFinding(
                subsystem=EnumSubsystem.KAFKA,
                severity=EnumHealthFindingSeverity.FAIL,
                subject="test.topic",
                message="topic missing",
                evidence="test",
            )
        ],
    )
    overall = handler._aggregate_overall([fail_result])
    assert overall == EnumReadinessStatus.FAIL


# ---------------------------------------------------------------------------
# Task 12: EventBusInmemory golden chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_event_bus_wiring() -> None:
    event_bus = EventBusInmemory()
    handler = NodeEnvironmentHealthScanner()
    captured: list[dict] = []

    async def on_command(message: object) -> None:
        payload = json.loads(message.value)
        req = EnvironmentHealthRequest(
            subsystems=payload.get("subsystems", ["entry_points"]),
            omni_home=payload.get("omni_home", ""),
            ssh_target=None,
        )
        result = handler.handle(req)
        out = {
            "overall": result.overall.value,
            "subsystem_count": len(result.subsystem_results),
        }
        captured.append(out)
        await event_bus.publish(EVT_TOPIC, key=None, value=json.dumps(out).encode())

    await event_bus.start()
    await event_bus.subscribe(CMD_TOPIC, on_message=on_command, group_id="test-scanner")
    cmd = json.dumps({"subsystems": ["entry_points"]}).encode()
    await event_bus.publish(CMD_TOPIC, key=None, value=cmd)

    assert len(captured) == 1
    assert captured[0]["overall"] in ("PASS", "WARN", "FAIL")

    history = await event_bus.get_event_history(topic=EVT_TOPIC)
    assert len(history) == 1
    await event_bus.close()
