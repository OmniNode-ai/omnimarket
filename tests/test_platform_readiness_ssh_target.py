# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SSH-target resolution tests for node_platform_readiness — OMN-13921.

Defect class under enforcement: the handler previously resolved
``ONEX_INFRA_SSH_TARGET`` at import time with a silent ``""`` default, so every
remote readiness probe executed as ``ssh '' ...`` (empty host) and timed out.

These tests fail on recurrence:
  1. Missing/blank env var must raise a typed error naming the env var BEFORE
     any subprocess probe runs — no silent empty-host degradation.
  2. When the env var IS set, every ssh invocation must carry that exact
     non-empty host as argv[1] — never ``ssh ''``.
  3. Explicit request dimensions must not require the env var at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.nodes.node_platform_readiness.handlers import (
    handler_platform_readiness as hpr,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
    InfraSshTargetNotConfiguredError,
    ModelDimensionInput,
    ModelPlatformReadinessRequest,
    NodePlatformReadiness,
)

_ENV_VAR = "ONEX_INFRA_SSH_TARGET"
_TARGET = "user@testhost.example"


class _RecordingRun:
    """subprocess.run stand-in that records argv and returns a benign result."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(argv))

        class _Result:
            returncode = 1
            stdout = ""
            stderr = "recorded by test double"

        return _Result()


@pytest.mark.unit
class TestSshTargetFailFast:
    """Missing config must raise a typed error, never probe an empty host."""

    def test_unset_env_raises_typed_error_before_any_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
        recorder = _RecordingRun()
        monkeypatch.setattr(hpr.subprocess, "run", recorder)

        with pytest.raises(InfraSshTargetNotConfiguredError) as excinfo:
            NodePlatformReadiness().handle(ModelPlatformReadinessRequest())

        # Typed error names the missing key (DoD: fail fast naming the key).
        assert _ENV_VAR in str(excinfo.value)
        # Fail-fast means zero probes ran — not five ssh-'' timeouts.
        assert recorder.calls == []

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_env_raises_typed_error(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ) -> None:
        monkeypatch.setenv(_ENV_VAR, blank)
        recorder = _RecordingRun()
        monkeypatch.setattr(hpr.subprocess, "run", recorder)

        with pytest.raises(InfraSshTargetNotConfiguredError):
            NodePlatformReadiness().handle(ModelPlatformReadinessRequest())

        assert recorder.calls == []

    def test_no_module_level_env_snapshot(self) -> None:
        """Recurrence guard: the import-time ``_INFRA_SSH_TARGET`` snapshot is gone.

        The original defect froze the env var at import time with a ``""``
        default; any reintroduction of a module-level snapshot recreates it.
        """
        assert not hasattr(hpr, "_INFRA_SSH_TARGET")


@pytest.mark.unit
class TestSshTargetInjection:
    """When configured, every ssh probe must use the resolved non-empty host."""

    def test_all_ssh_probes_use_resolved_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_ENV_VAR, _TARGET)
        recorder = _RecordingRun()
        monkeypatch.setattr(hpr.subprocess, "run", recorder)

        result = NodePlatformReadiness().handle(ModelPlatformReadinessRequest())

        ssh_calls = [argv for argv in recorder.calls if argv and argv[0] == "ssh"]
        # 5 SSH-backed dimensions: docker_image_age, migration_watermark,
        # kafka_topic_coverage, quality_score_coverage, baselines_freshness.
        assert len(ssh_calls) == 5
        for argv in ssh_calls:
            assert argv[1] == _TARGET
            assert argv[1].strip() != ""  # the defect: ssh '' — must never recur

        # Non-vacuous: the run evaluated real collected dimensions.
        assert len(result.dimensions) == 7

    def test_target_is_resolved_at_call_time_not_import_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Changing the env var after import must affect the next run."""
        recorder = _RecordingRun()
        monkeypatch.setattr(hpr.subprocess, "run", recorder)

        monkeypatch.setenv(_ENV_VAR, "user@first.example")
        NodePlatformReadiness().handle(ModelPlatformReadinessRequest())
        monkeypatch.setenv(_ENV_VAR, "user@second.example")
        NodePlatformReadiness().handle(ModelPlatformReadinessRequest())

        hosts = {argv[1] for argv in recorder.calls if argv and argv[0] == "ssh"}
        assert hosts == {"user@first.example", "user@second.example"}


@pytest.mark.unit
class TestExplicitDimensionsBypassSshResolution:
    """Supplied dimensions need no SSH config — pure evaluation path."""

    def test_explicit_dimensions_do_not_require_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
        now = datetime.now(UTC)
        request = ModelPlatformReadinessRequest(
            dimensions=[
                ModelDimensionInput(
                    name="ci_health",
                    critical=True,
                    healthy=True,
                    last_checked=now,
                    details="green",
                )
            ],
            now=now,
        )

        result = NodePlatformReadiness().handle(request)

        assert result.overall == EnumReadinessStatus.PASS
