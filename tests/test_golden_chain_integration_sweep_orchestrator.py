# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-9334 reason="test fixture — uses .201 lab endpoint as integration-sweep test input; not a runtime default or shipping connection string"
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.validation.runtime_sha_match import CHECK_TYPE_RUNTIME_SHA_MATCH

from omnimarket.nodes.node_integration_sweep_orchestrator.handlers.handler_integration_sweep_orchestrator import (
    HandlerIntegrationSweepOrchestrator,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes import (
    probe_container_health,
    probe_db_tables,
    probe_github_ci,
    probe_golden_chain,
    probe_kafka_topics,
    probe_projection_api,
    probe_runtime_health,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_request import (
    ModelIntegrationSweepOrchestratorRequest,
)


def test_integration_sweep_writes_drift_artifact(tmp_path: Path) -> None:
    result = HandlerIntegrationSweepOrchestrator().handle(
        ModelIntegrationSweepOrchestratorRequest(
            scope="explicit",
            tickets=["OMN-10409"],
            artifact_root=str(tmp_path),
            artifact_date="2026-04-30",
            run_surface_probes=False,
        )
    )

    artifact_path = Path(result.artifact_path)
    assert result.status == "recorded"
    assert result.artifact_written is True
    assert result.ticket_count == 1
    assert artifact_path == tmp_path / "drift" / "integration" / "2026-04-30.yaml"
    assert artifact_path.is_file()

    artifact = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    assert artifact["artifact_type"] == "ModelIntegrationRecord"
    assert artifact["tickets"] == ["OMN-10409"]
    assert artifact["status"] == "recorded"


def test_integration_sweep_writes_runtime_sha_receipt_for_stale_runtime(
    tmp_path: Path,
) -> None:
    merge_sha = "abc123def456"  # pragma: allowlist secret
    stale_sha = "deadbeef0000"  # pragma: allowlist secret
    _write_runtime_sha_contract(tmp_path / "contracts", "OMN-9334", merge_sha)

    result = HandlerIntegrationSweepOrchestrator(
        runtime_sha_handler=_StubRuntimeShaHandler(
            ticket_id="OMN-9334",
            evidence_item_id="dod-runtime-sha",
            merge_sha=merge_sha,
            deployed_sha=stale_sha,
        )
    ).handle(
        ModelIntegrationSweepOrchestratorRequest(
            scope="explicit",
            tickets=["OMN-9334"],
            artifact_root=str(tmp_path),
            artifact_date="2026-04-30",
            run_surface_probes=False,
        )
    )

    receipt_path = (
        tmp_path
        / "drift"
        / "dod_receipts"
        / "OMN-9334"
        / "dod-runtime-sha"
        / "runtime_sha_match.yaml"
    )
    artifact = yaml.safe_load(Path(result.artifact_path).read_text(encoding="utf-8"))
    receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))

    assert result.status == "blocked"
    assert result.details["runtime_sha_stale"] == "1"
    assert artifact["status"] == "blocked"
    assert artifact["runtime_sha_match"][0]["status"] == "FAIL"
    assert receipt["check_type"] == CHECK_TYPE_RUNTIME_SHA_MATCH
    assert receipt["check_value"] == merge_sha
    assert receipt["status"] == "FAIL"


def test_integration_sweep_dry_run_does_not_write(tmp_path: Path) -> None:
    result = HandlerIntegrationSweepOrchestrator().handle(
        ModelIntegrationSweepOrchestratorRequest(
            tickets=["OMN-10409"],
            artifact_root=str(tmp_path),
            artifact_date="2026-04-30",
            dry_run=True,
        )
    )

    assert result.artifact_written is False
    assert not Path(result.artifact_path).exists()


def test_contract_declares_node_as_implemented() -> None:
    contract_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_integration_sweep_orchestrator"
        / "contract.yaml"
    )
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    assert raw.get("node_not_implemented") is not True
    assert raw["terminal_event"] == "onex.evt.omnimarket.integration-sweep-completed.v1"


# --- Surface probe unit tests ---


@pytest.mark.unit
def test_probe_runtime_health_pass() -> None:
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = '{"status": "ok"}'
    mock_result.stderr = ""

    url = "http://192.168.86.201:18085"  # onex-allow-internal-ip: test fixture
    with patch(
        "omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes.subprocess.run",
        return_value=mock_result,
    ):
        result = probe_runtime_health(url)

    assert result["surface"] == "RUNTIME_HEALTH"
    assert result["status"] == "pass"
    assert "response" in result["details"]


@pytest.mark.unit
def test_probe_runtime_health_fail_on_nonzero_exit() -> None:
    mock_result = MagicMock()
    mock_result.returncode = 7
    mock_result.stdout = ""
    mock_result.stderr = "Connection refused"

    url = "http://192.168.86.201:18085"  # onex-allow-internal-ip: test fixture
    with patch(
        "omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes.subprocess.run",
        return_value=mock_result,
    ):
        result = probe_runtime_health(url)

    assert result["surface"] == "RUNTIME_HEALTH"
    assert result["status"] == "fail"


@pytest.mark.unit
def test_probe_runtime_health_error_on_exception() -> None:
    url = "http://192.168.86.201:18085"  # onex-allow-internal-ip: test fixture
    with patch(
        "omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes.subprocess.run",
        side_effect=TimeoutError("timed out"),
    ):
        result = probe_runtime_health(url)

    assert result["surface"] == "RUNTIME_HEALTH"
    assert result["status"] == "error"
    assert "error" in result["details"]


@pytest.mark.unit
def test_probe_container_health_pass() -> None:
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "omnibase-runtime\tUp 2 hours\nomnibase-postgres\tUp 2 hours\n"
    mock_result.stderr = ""

    host = "192.168.86.201"  # onex-allow-internal-ip: test fixture
    run_mock = MagicMock(return_value=mock_result)
    with patch(
        "omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes.subprocess.run",
        run_mock,
    ):
        result = probe_container_health(host)

    args = run_mock.call_args.args[0]
    assert args == [
        "ssh",
        f"jonah@{host}",
        "docker ps --format '{{.Names}}\t{{.Status}}'",
    ]
    assert result["surface"] == "CONTAINER_HEALTH"
    assert result["status"] == "pass"
    assert result["details"]["total_containers"] == 2
    assert result["details"]["running"] == 2
    assert result["details"]["unhealthy"] == 0


@pytest.mark.unit
def test_probe_container_health_fail_on_unhealthy() -> None:
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "omnibase-runtime\tUp 2 hours (unhealthy)\n"
    mock_result.stderr = ""

    host = "192.168.86.201"  # onex-allow-internal-ip: test fixture
    with patch(
        "omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes.subprocess.run",
        return_value=mock_result,
    ):
        result = probe_container_health(host)

    assert result["surface"] == "CONTAINER_HEALTH"
    assert result["status"] == "fail"
    assert result["details"]["unhealthy"] == 1


@pytest.mark.unit
def test_probe_container_health_error_on_exception() -> None:
    host = "192.168.86.201"  # onex-allow-internal-ip: test fixture
    with patch(
        "omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes.subprocess.run",
        side_effect=TimeoutError("ssh timeout"),
    ):
        result = probe_container_health(host)

    assert result["surface"] == "CONTAINER_HEALTH"
    assert result["status"] == "error"
    assert "error" in result["details"]


@pytest.mark.unit
def test_probe_github_ci_pass() -> None:
    runs = [
        {"conclusion": "success", "name": "CI", "status": "completed"},
        {"conclusion": "success", "name": "CI", "status": "completed"},
    ]
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(runs)
    mock_result.stderr = ""

    with patch(
        "omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes.subprocess.run",
        return_value=mock_result,
    ):
        result = probe_github_ci("omnimarket")

    assert result["surface"] == "GITHUB_CI"
    assert result["status"] == "pass"
    assert result["details"]["pass"] == 2
    assert result["details"]["fail"] == 0


@pytest.mark.unit
def test_probe_github_ci_fail_on_failure_runs() -> None:
    runs = [
        {"conclusion": "failure", "name": "CI", "status": "completed"},
        {"conclusion": "success", "name": "CI", "status": "completed"},
    ]
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(runs)
    mock_result.stderr = ""

    with patch(
        "omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes.subprocess.run",
        return_value=mock_result,
    ):
        result = probe_github_ci("omnimarket")

    assert result["surface"] == "GITHUB_CI"
    assert result["status"] == "fail"
    assert result["details"]["fail"] == 1


@pytest.mark.unit
def test_probe_github_ci_error_on_exception() -> None:
    with patch(
        "omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes.subprocess.run",
        side_effect=FileNotFoundError("gh not found"),
    ):
        result = probe_github_ci("omnimarket")

    assert result["surface"] == "GITHUB_CI"
    assert result["status"] == "error"
    assert "error" in result["details"]


@pytest.mark.unit
def test_handler_surface_probes_written_to_artifact(tmp_path: Path) -> None:
    """Verify surface probe results appear in the written artifact YAML."""
    mock_probes = [
        {"surface": "RUNTIME_HEALTH", "status": "pass", "details": {"response": "ok"}},
        {"surface": "CONTAINER_HEALTH", "status": "pass", "details": {"running": 2}},
        {"surface": "GITHUB_CI", "status": "pass", "details": {"pass": 3, "fail": 0}},
    ]

    with patch(
        "omnimarket.nodes.node_integration_sweep_orchestrator.handlers.handler_integration_sweep_orchestrator.HandlerIntegrationSweepOrchestrator._run_surface_probes",
        return_value=mock_probes,
    ):
        result = HandlerIntegrationSweepOrchestrator().handle(
            ModelIntegrationSweepOrchestratorRequest(
                scope="explicit",
                tickets=[],
                artifact_root=str(tmp_path),
                artifact_date="2026-05-25",
                run_surface_probes=True,
            )
        )

    artifact = yaml.safe_load(Path(result.artifact_path).read_text(encoding="utf-8"))
    assert len(artifact["surfaces"]) == 3
    surface_names = [s["surface"] for s in artifact["surfaces"]]
    assert "RUNTIME_HEALTH" in surface_names
    assert "CONTAINER_HEALTH" in surface_names
    assert "GITHUB_CI" in surface_names
    assert result.details["surface_probe_count"] == "3"
    assert len(result.surfaces) == 3


# --- Infrastructure-surface probe unit tests (KAFKA / DB / PROJECTION / GOLDEN_CHAIN) ---

_PROBES_RUN = (
    "omnimarket.nodes.node_integration_sweep_orchestrator."
    "handlers.surface_probes.subprocess.run"
)
_HOST = "192.168.86.201"  # onex-allow-internal-ip: test fixture


@pytest.mark.unit
def test_probe_kafka_topics_pass() -> None:
    def _run(argv: list[str], **_kw: object) -> MagicMock:
        out = MagicMock()
        out.returncode = 0
        out.stderr = ""
        joined = " ".join(argv)
        if "topic list" in joined:
            out.stdout = "NAME\nonex.cmd.foo.v1\n"
        else:
            out.stdout = "BROKER GROUP\n0 grp-foo\n"
        return out

    with patch(_PROBES_RUN, side_effect=_run):
        result = probe_kafka_topics(_HOST, "redpanda", ["onex.cmd.foo.v1"], ["grp-foo"])

    assert result["surface"] == "KAFKA"
    assert result["status"] == "pass"
    assert result["details"]["topics_missing"] == []
    assert result["details"]["consumer_groups_missing"] == []


@pytest.mark.unit
def test_probe_kafka_topics_fail_on_missing_topic() -> None:
    def _run(argv: list[str], **_kw: object) -> MagicMock:
        out = MagicMock()
        out.returncode = 0
        out.stderr = ""
        joined = " ".join(argv)
        out.stdout = "NAME\n" if "topic list" in joined else "BROKER GROUP\n0 grp-foo\n"
        return out

    with patch(_PROBES_RUN, side_effect=_run):
        result = probe_kafka_topics(
            _HOST, "redpanda", ["onex.cmd.missing.v1"], ["grp-foo"]
        )

    assert result["status"] == "fail"
    assert result["details"]["topics_missing"] == ["onex.cmd.missing.v1"]


@pytest.mark.unit
def test_probe_kafka_topics_error_on_exception() -> None:
    with patch(_PROBES_RUN, side_effect=TimeoutError("ssh timeout")):
        result = probe_kafka_topics(_HOST, "redpanda", ["t"], ["g"])
    assert result["status"] == "error"
    assert "error" in result["details"]


@pytest.mark.unit
def test_probe_db_tables_pass() -> None:
    def _run(argv: list[str], **_kw: object) -> MagicMock:
        out = MagicMock()
        out.returncode = 0
        out.stderr = ""
        joined = " ".join(argv)
        if "to_regclass" in joined:
            out.stdout = "public.session_outcomes\n"
        else:
            out.stdout = "3\n"
        return out

    with patch(_PROBES_RUN, side_effect=_run):
        result = probe_db_tables(
            _HOST, "pg", "postgres", "omnidash_analytics", ["session_outcomes"]
        )

    assert result["surface"] == "DB"
    assert result["status"] == "pass"
    assert result["details"]["tables_absent"] == []
    assert result["details"]["tables_empty"] == []
    assert result["details"]["tables"][0]["row_count"] == 3


@pytest.mark.unit
def test_probe_db_tables_fail_on_empty_table() -> None:
    def _run(argv: list[str], **_kw: object) -> MagicMock:
        out = MagicMock()
        out.returncode = 0
        out.stderr = ""
        joined = " ".join(argv)
        out.stdout = "public.session_outcomes\n" if "to_regclass" in joined else "0\n"
        return out

    with patch(_PROBES_RUN, side_effect=_run):
        result = probe_db_tables(
            _HOST, "pg", "postgres", "omnidash_analytics", ["session_outcomes"]
        )

    assert result["status"] == "fail"
    assert result["details"]["tables_empty"] == ["session_outcomes"]


@pytest.mark.unit
def test_probe_db_tables_fail_on_absent_table() -> None:
    def _run(argv: list[str], **_kw: object) -> MagicMock:
        out = MagicMock()
        out.returncode = 0
        out.stderr = ""
        out.stdout = "NULL\n"  # to_regclass NULL => absent
        return out

    with patch(_PROBES_RUN, side_effect=_run):
        result = probe_db_tables(_HOST, "pg", "postgres", "db", ["ghost_table"])

    assert result["status"] == "fail"
    assert result["details"]["tables_absent"] == ["ghost_table"]


@pytest.mark.unit
def test_probe_db_tables_error_on_exception() -> None:
    with patch(_PROBES_RUN, side_effect=TimeoutError("ssh timeout")):
        result = probe_db_tables(_HOST, "pg", "postgres", "db", ["t"])
    assert result["status"] == "error"
    assert "error" in result["details"]


@pytest.mark.unit
def test_probe_projection_api_pass() -> None:
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = "200"
    mock.stderr = ""
    url = "http://192.168.86.201:3002"  # onex-allow-internal-ip: test fixture
    with patch(_PROBES_RUN, return_value=mock):
        result = probe_projection_api(url, ["onex.evt.foo.v1"])
    assert result["surface"] == "PROJECTION"
    assert result["status"] == "pass"
    assert result["details"]["topics_failed"] == []


@pytest.mark.unit
def test_probe_projection_api_fail_on_non_200() -> None:
    mock = MagicMock()
    mock.returncode = 22
    mock.stdout = "404"
    mock.stderr = ""
    url = "http://192.168.86.201:3002"  # onex-allow-internal-ip: test fixture
    with patch(_PROBES_RUN, return_value=mock):
        result = probe_projection_api(url, ["onex.evt.missing.v1"])
    assert result["status"] == "fail"
    assert result["details"]["topics_failed"] == ["onex.evt.missing.v1"]


@pytest.mark.unit
def test_probe_golden_chain_pass() -> None:
    def _run(argv: list[str], **_kw: object) -> MagicMock:
        out = MagicMock()
        out.returncode = 0
        out.stderr = ""
        joined = " ".join(argv)
        if "topic list" in joined:
            out.stdout = "NAME\nonex.cmd.foo.v1\n"
        elif "group list" in joined:
            out.stdout = "BROKER GROUP\n0 grp-foo\n"
        elif "to_regclass" in joined:
            out.stdout = "public.tail_table\n"
        elif "count(*)" in joined:
            out.stdout = "7\n"
        else:
            out.stdout = ""
        return out

    with patch(_PROBES_RUN, side_effect=_run):
        result = probe_golden_chain(
            runtime_host=_HOST,
            redpanda_container="redpanda",
            postgres_container="pg",
            postgres_user="postgres",
            chain_name="routing",
            command_topic="onex.cmd.foo.v1",
            consumer_group="grp-foo",
            tail_database="omnidash_analytics",
            tail_table="tail_table",
        )

    assert result["surface"] == "GOLDEN_CHAIN"
    assert result["status"] == "pass"
    assert result["details"]["tail_row_count"] == 7
    assert result["details"]["tail_has_rows"] is True


@pytest.mark.unit
def test_probe_golden_chain_fail_on_empty_tail() -> None:
    def _run(argv: list[str], **_kw: object) -> MagicMock:
        out = MagicMock()
        out.returncode = 0
        out.stderr = ""
        joined = " ".join(argv)
        if "topic list" in joined:
            out.stdout = "NAME\nonex.cmd.foo.v1\n"
        elif "group list" in joined:
            out.stdout = "BROKER GROUP\n0 grp-foo\n"
        elif "to_regclass" in joined:
            out.stdout = "public.tail_table\n"
        elif "count(*)" in joined:
            out.stdout = "0\n"
        else:
            out.stdout = ""
        return out

    with patch(_PROBES_RUN, side_effect=_run):
        result = probe_golden_chain(
            runtime_host=_HOST,
            redpanda_container="redpanda",
            postgres_container="pg",
            postgres_user="postgres",
            chain_name="routing",
            command_topic="onex.cmd.foo.v1",
            consumer_group="grp-foo",
            tail_database="omnidash_analytics",
            tail_table="tail_table",
        )

    assert result["status"] == "fail"
    assert result["details"]["tail_has_rows"] is False


class _StubRuntimeShaHandler:
    def __init__(
        self,
        *,
        ticket_id: str,
        evidence_item_id: str,
        merge_sha: str,
        deployed_sha: str,
    ) -> None:
        self._ticket_id = ticket_id
        self._evidence_item_id = evidence_item_id
        self._merge_sha = merge_sha
        self._deployed_sha = deployed_sha

    def handle(self, request: object) -> ModelDodReceipt:
        match = self._deployed_sha == self._merge_sha
        return ModelDodReceipt(
            schema_version="1.0.0",
            ticket_id=self._ticket_id,
            evidence_item_id=self._evidence_item_id,
            check_type=CHECK_TYPE_RUNTIME_SHA_MATCH,
            check_value=self._merge_sha,
            status=EnumReceiptStatus.PASS if match else EnumReceiptStatus.FAIL,
            run_timestamp=datetime.now(tz=UTC),
            commit_sha=self._deployed_sha,
            runner="integration-sweep-verifier",
            verifier="integration-sweep-test-verifier",
            probe_command="ssh 192.168.86.201 git -C /data/omninode/omni_home/omnimarket rev-parse HEAD",  # onex-allow-internal-ip: test fixture
            probe_stdout=f"{self._deployed_sha}\n",
            actual_output=json.dumps(
                {
                    "runtime_host": "192.168.86.201",  # onex-allow-internal-ip: test fixture
                    "deployed_sha": self._deployed_sha,
                    "merge_sha": self._merge_sha,
                    "match": match,
                }
            ),
        )


def _write_runtime_sha_contract(
    contracts_dir: Path, ticket_id: str, merge_sha: str
) -> None:
    contracts_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticket_id": ticket_id,
        "title": "Runtime SHA gate",
        "dod_evidence": [
            {
                "id": "dod-runtime-sha",
                "description": "Runtime SHA matches merge SHA",
                "checks": [
                    {
                        "check_type": CHECK_TYPE_RUNTIME_SHA_MATCH,
                        "check_value": merge_sha,
                    }
                ],
            }
        ],
    }
    (contracts_dir / f"{ticket_id}.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=True),
        encoding="utf-8",
    )
