# SPDX-License-Identifier: MIT
"""Unit and integration tests for the service_catalog probe module."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import (
    ValidationError,
)

from omnimarket.nodes.node_handoff_effect.service_catalog import (
    _SERVICE_PROBES,
    ServiceCatalogSnapshot,
    ServiceProbeResult,
    _run_ssh_probe,
    probe_services,
)

_SSH_TARGET = "jonah@192.168.86.201"  # onex-allow-internal-ip OMN-11267 reason="test fixture for read-only service catalog probe"

_HEALTHY_RESPONSES: dict[str, str] = {
    "postgres": "omnibase_postgres|Up 3 hours",
    "kafka": "redpanda|Up 3 hours",
    "valkey": "valkey|Up 3 hours",
    "infisical": "infisical|Up 2 hours",
    "omninode-runtime-effects": "healthy",
    "deploy-agent.service": "active",
}


def _make_mock_run_ssh(responses: dict[str, str]) -> Any:
    """Return a mock _run_ssh_probe that dispatches by service name."""

    def _mock(
        ssh_target: str,
        remote_cmd: str,
        timeout_s: int,
    ) -> tuple[str, str | None]:
        for name, stdout in responses.items():
            if name in remote_cmd or any(
                name == svc_name and remote_cmd == cmd
                for svc_name, cmd, _match, _exact in _SERVICE_PROBES
                if svc_name == name
            ):
                return stdout, None
        return "", "unknown probe"

    return _mock


def _probe_with_fixed_outputs(
    outputs: dict[str, tuple[str, str | None]],
) -> ServiceCatalogSnapshot:
    """Helper: probe_services with SSH responses driven by service name."""
    custom_probes = list(_SERVICE_PROBES)

    def _mock(
        ssh_target: str, remote_cmd: str, timeout_s: int
    ) -> tuple[str, str | None]:
        for name, cmd, _match, _exact in custom_probes:
            if cmd == remote_cmd:
                return outputs.get(name, ("", "not configured"))
        return "", "unknown"

    with patch(
        "omnimarket.nodes.node_handoff_effect.service_catalog._run_ssh_probe",
        side_effect=_mock,
    ):
        return probe_services(_SSH_TARGET)


@pytest.mark.unit
class TestServiceProbeResult:
    def test_frozen_model(self) -> None:
        result = ServiceProbeResult(
            name="postgres",
            status="Up 3h",
            healthy=True,
            probe_timestamp=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            result.name = "other"  # type: ignore[misc]

    def test_error_defaults_to_none(self) -> None:
        result = ServiceProbeResult(
            name="postgres",
            status="Up",
            healthy=True,
            probe_timestamp=datetime.now(UTC),
        )
        assert result.error is None


@pytest.mark.unit
class TestServiceCatalogSnapshot:
    def _make_result(self, name: str, healthy: bool) -> ServiceProbeResult:
        return ServiceProbeResult(
            name=name,
            status="Up" if healthy else "Exited",
            healthy=healthy,
            probe_timestamp=datetime.now(UTC),
        )

    def test_is_fully_healthy_all_up(self) -> None:
        snapshot = ServiceCatalogSnapshot(
            snapshot_timestamp=datetime.now(UTC),
            services=(
                self._make_result("postgres", True),
                self._make_result("kafka", True),
            ),
        )
        assert snapshot.is_fully_healthy() is True

    def test_is_fully_healthy_one_down(self) -> None:
        snapshot = ServiceCatalogSnapshot(
            snapshot_timestamp=datetime.now(UTC),
            services=(
                self._make_result("postgres", True),
                self._make_result("kafka", False),
            ),
        )
        assert snapshot.is_fully_healthy() is False

    def test_is_fully_healthy_empty(self) -> None:
        snapshot = ServiceCatalogSnapshot(
            snapshot_timestamp=datetime.now(UTC),
            services=(),
        )
        assert snapshot.is_fully_healthy() is True

    def test_get_existing_service(self) -> None:
        r = self._make_result("postgres", True)
        snapshot = ServiceCatalogSnapshot(
            snapshot_timestamp=datetime.now(UTC),
            services=(r,),
        )
        assert snapshot.get("postgres") is r

    def test_get_missing_service(self) -> None:
        snapshot = ServiceCatalogSnapshot(
            snapshot_timestamp=datetime.now(UTC),
            services=(),
        )
        assert snapshot.get("postgres") is None

    def test_frozen_model(self) -> None:
        snapshot = ServiceCatalogSnapshot(
            snapshot_timestamp=datetime.now(UTC),
            services=(),
        )
        with pytest.raises(ValidationError):
            snapshot.services = ()  # type: ignore[misc]


@pytest.mark.unit
class TestRunSshProbe:
    def test_success_returns_stdout(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "omnibase_postgres|Up 3 hours\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            output, error = _run_ssh_probe(_SSH_TARGET, "docker ps ...", 5)

        assert output == "omnibase_postgres|Up 3 hours"
        assert error is None

    def test_nonzero_exit_with_no_stdout_returns_error(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "connection refused"

        with patch("subprocess.run", return_value=mock_result):
            output, error = _run_ssh_probe(_SSH_TARGET, "docker ps ...", 5)

        assert output == ""
        assert error is not None
        assert "connection refused" in error

    def test_timeout_returns_error(self) -> None:
        import subprocess as sp

        with patch("subprocess.run", side_effect=sp.TimeoutExpired("ssh", 5)):
            output, error = _run_ssh_probe(_SSH_TARGET, "docker ps ...", 5)

        assert output == ""
        assert error is not None
        assert "timed out" in error

    def test_os_error_returns_error(self) -> None:
        with patch("subprocess.run", side_effect=OSError("no route to host")):
            output, error = _run_ssh_probe(_SSH_TARGET, "docker ps ...", 5)

        assert output == ""
        assert error is not None
        assert "no route to host" in error


@pytest.mark.unit
class TestProbeServices:
    def test_all_healthy_snapshot(self) -> None:
        outputs = {
            name: (_HEALTHY_RESPONSES[name], None) for name in _HEALTHY_RESPONSES
        }
        snapshot = _probe_with_fixed_outputs(outputs)

        assert snapshot.is_fully_healthy() is True
        assert len(snapshot.services) == len(_SERVICE_PROBES)

    def test_one_failed_probe_does_not_skip_others(self) -> None:
        outputs: dict[str, tuple[str, str | None]] = {
            name: (_HEALTHY_RESPONSES[name], None) for name in _HEALTHY_RESPONSES
        }
        outputs["kafka"] = ("", "timed out after 5s")

        snapshot = _probe_with_fixed_outputs(outputs)

        assert len(snapshot.services) == len(_SERVICE_PROBES)
        kafka = snapshot.get("kafka")
        assert kafka is not None
        assert kafka.healthy is False
        assert kafka.error == "timed out after 5s"

        postgres = snapshot.get("postgres")
        assert postgres is not None
        assert postgres.healthy is True

    def test_all_failed_snapshot_not_healthy(self) -> None:
        outputs = dict.fromkeys(
            _HEALTHY_RESPONSES,
            (
                "",
                "ssh: connect to host 192.168.86.201",  # onex-allow-internal-ip OMN-11267 reason="simulated SSH error fixture for service catalog probe"
            ),
        )
        snapshot = _probe_with_fixed_outputs(outputs)

        assert snapshot.is_fully_healthy() is False
        for svc in snapshot.services:
            assert svc.healthy is False

    def test_custom_service_probes(self) -> None:
        custom_probes = [("test-svc", "echo hello", "hello", True)]

        with patch(
            "omnimarket.nodes.node_handoff_effect.service_catalog._run_ssh_probe",
            return_value=("hello", None),
        ):
            snapshot = probe_services(_SSH_TARGET, service_probes=custom_probes)

        assert len(snapshot.services) == 1
        assert snapshot.services[0].name == "test-svc"
        assert snapshot.services[0].healthy is True

    def test_probe_timestamp_is_utc(self) -> None:
        outputs = {
            name: (_HEALTHY_RESPONSES[name], None) for name in _HEALTHY_RESPONSES
        }
        snapshot = _probe_with_fixed_outputs(outputs)

        assert snapshot.snapshot_timestamp.tzinfo is not None
        for svc in snapshot.services:
            assert svc.probe_timestamp.tzinfo is not None

    def test_postgres_healthy_on_up_status(self) -> None:
        outputs: dict[str, tuple[str, str | None]] = {
            name: (_HEALTHY_RESPONSES[name], None) for name in _HEALTHY_RESPONSES
        }
        snapshot = _probe_with_fixed_outputs(outputs)
        pg = snapshot.get("postgres")
        assert pg is not None
        assert pg.healthy is True

    def test_runtime_effects_healthy_on_healthy_status(self) -> None:
        outputs: dict[str, tuple[str, str | None]] = {
            name: (_HEALTHY_RESPONSES[name], None) for name in _HEALTHY_RESPONSES
        }
        snapshot = _probe_with_fixed_outputs(outputs)
        svc = snapshot.get("omninode-runtime-effects")
        assert svc is not None
        assert svc.healthy is True

    def test_deploy_agent_healthy_on_active(self) -> None:
        outputs: dict[str, tuple[str, str | None]] = {
            name: (_HEALTHY_RESPONSES[name], None) for name in _HEALTHY_RESPONSES
        }
        snapshot = _probe_with_fixed_outputs(outputs)
        svc = snapshot.get("deploy-agent.service")
        assert svc is not None
        assert svc.healthy is True

    def test_deploy_agent_unhealthy_on_inactive(self) -> None:
        outputs: dict[str, tuple[str, str | None]] = {
            name: (_HEALTHY_RESPONSES[name], None) for name in _HEALTHY_RESPONSES
        }
        outputs["deploy-agent.service"] = ("inactive", None)
        snapshot = _probe_with_fixed_outputs(outputs)
        svc = snapshot.get("deploy-agent.service")
        assert svc is not None
        assert svc.healthy is False


@pytest.mark.integration
class TestProbeServicesLive:
    """Integration tests that hit live .201 infra. Skip if SSH is unreachable."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_ssh(self) -> None:
        import subprocess as sp

        result = sp.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=3",
                "-o",
                "BatchMode=yes",
                _SSH_TARGET,
                "echo ping",
            ],
            capture_output=True,
            timeout=6,
        )
        if result.returncode != 0:
            pytest.skip("SSH to .201 not reachable")

    def test_live_snapshot_returns_all_services(self) -> None:
        snapshot = probe_services(_SSH_TARGET, timeout_s=10)
        service_names = {s.name for s in snapshot.services}
        expected = {name for name, _, _m, _e in _SERVICE_PROBES}
        assert expected == service_names

    def test_live_snapshot_has_valid_timestamps(self) -> None:
        snapshot = probe_services(_SSH_TARGET, timeout_s=10)
        assert snapshot.snapshot_timestamp.tzinfo is not None
        for svc in snapshot.services:
            assert svc.probe_timestamp.tzinfo is not None
