# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit + cross-boundary tests for scripts/trigger_rebuild_on_merge.py.

Seam under test (OMN-14702, completing the OMN-12573 omnimarket half): the CI
post-merge producer publishes the canonical node_redeploy_orchestrator start
command ``onex.cmd.omnimarket.redeploy-start.v1`` — NOT the direct
``onex.cmd.deploy.rebuild-requested.v1`` — with a base-branch->lane mapping and
the exact merge SHA, and no hardcoded ``origin/main``. node_redeploy_orchestrator
remains the sole emitter of the deploy-agent rebuild command downstream.

These tests drive the actual ``main`` CLI (the exact surface the GHA workflow
invokes) and inspect the candidate start-command envelope byte-for-byte, so the
workflow<->script seam is exercised end-to-end rather than as two independent
unit suites.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "trigger_rebuild_on_merge.py"

_REDEPLOY_START_TOPIC = "onex.cmd.omnimarket.redeploy-start.v1"
_RUNTIME_CHANGE_FILE = "src/omnimarket/nodes/node_runtime_sweep/handler.py"
_MERGE_SHA = "deadbeefcafef00d1234567890abcdef12345678"
_PUBLISH_CREDS_ENV = (
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_SASL_USERNAME",
    "KAFKA_SASL_PASSWORD",
    "DEPLOY_AGENT_HMAC_SECRET",
)


def _load_trigger_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "trigger_rebuild_on_merge", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["trigger_rebuild_on_merge"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def trigger_module() -> Any:
    return _load_trigger_module()


def _set_publish_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PUBLISH_CREDS_ENV:
        monkeypatch.setenv(name, "present")


def _clear_publish_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PUBLISH_CREDS_ENV:
        monkeypatch.delenv(name, raising=False)


def _record_publish(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """Patch the emit so tests can inspect exactly what would be published."""
    calls: list[dict[str, Any]] = []

    def _fake_publish(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(trigger_module, "publish_redeploy_start_event", _fake_publish)
    return calls


# --------------------------------------------------------------------------- #
# Canonical seam: topic + payload fields (byte-level, deterministic)           #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_redeploy_start_topic_is_canonical(trigger_module: Any) -> None:
    """CI publishes the node_redeploy start command, never rebuild-requested."""
    assert trigger_module.TOPIC == _REDEPLOY_START_TOPIC
    assert trigger_module.TOPIC != "onex.cmd.deploy.rebuild-requested.v1"


@pytest.mark.unit
def test_build_envelope_matches_infra_field_shape_and_signature(
    trigger_module: Any,
) -> None:
    """The signed envelope has exactly the canonical fields and a valid HMAC."""
    signed = trigger_module.build_redeploy_start_envelope(
        runtime_lane="dev",
        source_branch="dev",
        source_sha=_MERGE_SHA,
        correlation_id="corr-1",
        requested_by="gha/omnimarket/pr-42",
        hmac_secret="hmac-secret",
    )

    assert set(signed) == {
        "correlation_id",
        "requested_by",
        "runtime_lane",
        "source_branch",
        "source_sha",
        "requires_occ",
        "requires_readiness_gate",
        "requested_at",
        "_signature",
    }
    # No legacy rebuild-requested fields leaked through.
    assert "scope" not in signed
    assert "services" not in signed
    assert "git_ref" not in signed

    assert signed["runtime_lane"] == "dev"
    assert signed["source_branch"] == "dev"
    assert signed["source_sha"] == _MERGE_SHA
    assert signed["requires_occ"] is True
    assert signed["requires_readiness_gate"] is False  # dev

    # _signature is HMAC-SHA256 over the sorted-key compact JSON body.
    body = {k: v for k, v in signed.items() if k != "_signature"}
    expected = hmac.new(
        b"hmac-secret",
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert signed["_signature"] == expected


@pytest.mark.unit
def test_stability_lane_requires_readiness_gate(trigger_module: Any) -> None:
    signed = trigger_module.build_redeploy_start_envelope(
        runtime_lane="stability-test",
        source_branch="main",
        source_sha=_MERGE_SHA,
        correlation_id="corr-2",
        requested_by="gha/omnimarket/pr-7",
        hmac_secret="hmac-secret",
    )
    assert signed["requires_readiness_gate"] is True  # non-dev lanes gate


# --------------------------------------------------------------------------- #
# Lane mapping: dev->dev, main->stability-test, fail-closed, no prod           #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_lane_mapping_dev_and_main(trigger_module: Any) -> None:
    assert trigger_module.lane_for_base_branch("dev") == "dev"
    assert trigger_module.lane_for_base_branch("main") == "stability-test"


@pytest.mark.unit
def test_no_ci_event_can_select_prod(trigger_module: Any) -> None:
    """prod is absent from the mapping — no merge event may deploy prod."""
    assert "prod" not in trigger_module._BASE_BRANCH_LANES
    assert "prod" not in set(trigger_module._BASE_BRANCH_LANES.values())
    with pytest.raises(ValueError, match="No runtime lane mapping"):
        trigger_module.lane_for_base_branch("prod")


@pytest.mark.unit
def test_unmapped_branch_raises(trigger_module: Any) -> None:
    with pytest.raises(ValueError, match="No runtime lane mapping"):
        trigger_module.lane_for_base_branch("release/1.2")


# --------------------------------------------------------------------------- #
# CLI cross-boundary fixtures (drive main, the workflow's exact surface)       #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_docs_only_merge_is_deploy_neutral_no_event(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docs-only merge to dev => reason-coded neutral, no publish."""
    _clear_publish_env(monkeypatch)
    calls = _record_publish(trigger_module, monkeypatch)

    result = CliRunner().invoke(
        trigger_module.main,
        [
            "--changed-files",
            "README.md,docs/thing.md",
            "--base-branch",
            "dev",
            "--source-sha",
            _MERGE_SHA,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "No rebuild trigger" in result.output
    assert calls == []


@pytest.mark.unit
def test_runtime_dev_merge_emits_one_dev_lane_start_event_for_sha(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime merge to dev => exactly one dev-lane start event for the merge SHA."""
    _set_publish_env(monkeypatch)
    calls = _record_publish(trigger_module, monkeypatch)

    result = CliRunner().invoke(
        trigger_module.main,
        [
            "--changed-files",
            _RUNTIME_CHANGE_FILE,
            "--base-branch",
            "dev",
            "--source-sha",
            _MERGE_SHA,
            "--correlation-id",
            "corr-dev",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    kw = calls[0]
    assert kw["runtime_lane"] == "dev"
    assert kw["source_branch"] == "dev"
    assert kw["source_sha"] == _MERGE_SHA
    assert kw["correlation_id"] == "corr-dev"
    assert f"Published redeploy-start to {_REDEPLOY_START_TOPIC}" in result.output
    assert "runtime_lane=dev" in result.output


@pytest.mark.unit
def test_runtime_main_merge_emits_one_stability_test_event(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime merge to main => exactly one stability-test-lane start event."""
    _set_publish_env(monkeypatch)
    calls = _record_publish(trigger_module, monkeypatch)

    result = CliRunner().invoke(
        trigger_module.main,
        [
            "--changed-files",
            _RUNTIME_CHANGE_FILE,
            "--base-branch",
            "main",
            "--source-sha",
            _MERGE_SHA,
            "--correlation-id",
            "corr-main",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["runtime_lane"] == "stability-test"
    assert calls[0]["source_branch"] == "main"
    assert calls[0]["source_sha"] == _MERGE_SHA


@pytest.mark.unit
def test_unmapped_branch_fails_closed(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge on an unmapped base branch fails closed (never a default lane)."""
    _set_publish_env(monkeypatch)
    calls = _record_publish(trigger_module, monkeypatch)

    result = CliRunner().invoke(
        trigger_module.main,
        [
            "--changed-files",
            _RUNTIME_CHANGE_FILE,
            "--base-branch",
            "release/9.9",
            "--source-sha",
            _MERGE_SHA,
        ],
    )

    assert result.exit_code != 0, result.output
    assert calls == []


@pytest.mark.unit
def test_unmapped_branch_and_missing_secret_fails_closed(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmapped branch AND absent secrets both fail closed (no green skip)."""
    _clear_publish_env(monkeypatch)
    calls = _record_publish(trigger_module, monkeypatch)

    result = CliRunner().invoke(
        trigger_module.main,
        [
            "--changed-files",
            _RUNTIME_CHANGE_FILE,
            "--base-branch",
            "staging",
            "--source-sha",
            _MERGE_SHA,
        ],
    )

    assert result.exit_code != 0, result.output
    assert calls == []


@pytest.mark.unit
def test_runtime_dev_merge_missing_secret_fails_closed(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mapped branch but absent publish secrets => RED (RT-5), not green skip."""
    _clear_publish_env(monkeypatch)

    result = CliRunner().invoke(
        trigger_module.main,
        [
            "--changed-files",
            _RUNTIME_CHANGE_FILE,
            "--base-branch",
            "dev",
            "--source-sha",
            _MERGE_SHA,
        ],
    )

    assert result.exit_code != 0, result.output
    assert "Redeploy triggered" in result.output
    assert "skipping publish" not in result.output


@pytest.mark.unit
def test_dry_run_runtime_merge_does_not_publish(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_publish_env(monkeypatch)

    def _explode(**_kwargs: Any) -> int:
        raise AssertionError("dry-run must not publish")

    monkeypatch.setattr(trigger_module, "publish_redeploy_start_event", _explode)

    result = CliRunner().invoke(
        trigger_module.main,
        [
            "--changed-files",
            _RUNTIME_CHANGE_FILE,
            "--base-branch",
            "dev",
            "--source-sha",
            _MERGE_SHA,
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "runtime_lane=dev" in result.output
    assert f"source_sha={_MERGE_SHA}" in result.output


# --------------------------------------------------------------------------- #
# Completion monitor: correlation match, timeout receipt, failed status        #
# --------------------------------------------------------------------------- #


class _FakeMessage:
    def __init__(self, payload: dict[str, Any], error: object | None = None) -> None:
        self._payload = payload
        self._error = error

    def error(self) -> object | None:
        return self._error

    def value(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _FakeConsumer:
    last: _FakeConsumer | None = None

    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
        self.closed = False
        self.subscriptions: list[list[str]] = []
        self.messages = [
            _FakeMessage({"correlation_id": "other", "status": "success"}),
            _FakeMessage({"correlation_id": "corr-123", "status": "success"}),
        ]
        _FakeConsumer.last = self

    def subscribe(self, topics: list[str]) -> None:
        self.subscriptions.append(topics)

    def poll(self, _timeout: float) -> _FakeMessage | None:
        if self.messages:
            return self.messages.pop(0)
        return None

    def close(self) -> None:
        self.closed = True


@pytest.mark.unit
def test_wait_for_rebuild_completion_matches_correlation_and_closes(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_confluent = types.SimpleNamespace(Consumer=_FakeConsumer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    completion = trigger_module.wait_for_rebuild_completion(
        bootstrap_servers="broker:9092",
        username="user",
        password="secret",
        correlation_id="corr-123",
        timeout_seconds=5,
    )

    assert completion["correlation_id"] == "corr-123"
    assert completion["status"] == "success"
    assert _FakeConsumer.last is not None
    assert _FakeConsumer.last.closed is True
    assert _FakeConsumer.last.subscriptions == [
        ["onex.evt.deploy.rebuild-completed.v1"]
    ]
    assert _FakeConsumer.last.config["group.id"] == (
        "gha-runtime-rebuild-trigger-corr-123"
    )


@pytest.mark.unit
def test_completion_timeout_yields_failed_correlation_receipt(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completion timeout raises TimeoutError naming the correlation_id."""

    class _NoMessageConsumer(_FakeConsumer):
        def __init__(self, config: dict[str, object]) -> None:
            super().__init__(config)
            self.messages = []

    fake_confluent = types.SimpleNamespace(Consumer=_NoMessageConsumer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    with pytest.raises(TimeoutError, match="correlation_id=corr-123"):
        trigger_module.wait_for_rebuild_completion(
            bootstrap_servers="broker:9092",
            username="user",
            password="secret",
            correlation_id="corr-123",
            timeout_seconds=0,
        )

    assert _FakeConsumer.last is not None
    assert _FakeConsumer.last.closed is True


@pytest.mark.unit
def test_cli_completion_timeout_exits_red_with_correlation(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a completion timeout is a durable RED receipt for the run."""
    _set_publish_env(monkeypatch)
    monkeypatch.setattr(
        trigger_module, "publish_redeploy_start_event", lambda **_kwargs: 1
    )

    def _timeout(**kwargs: Any) -> dict[str, Any]:
        raise TimeoutError(f"Timed out ... correlation_id={kwargs['correlation_id']}")

    monkeypatch.setattr(trigger_module, "wait_for_rebuild_completion", _timeout)

    result = CliRunner().invoke(
        trigger_module.main,
        [
            "--changed-files",
            _RUNTIME_CHANGE_FILE,
            "--base-branch",
            "dev",
            "--source-sha",
            _MERGE_SHA,
            "--correlation-id",
            "corr-timeout",
            "--wait-for-completion",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "correlation_id=corr-timeout" in result.output


@pytest.mark.unit
def test_cli_wait_for_completion_fails_on_failed_completion(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_publish_env(monkeypatch)
    # publish_redeploy_start_event returns the delivered count; the caller asserts
    # it is >=1 (RT-5 fail-closed on zero output), so the mock returns 1.
    monkeypatch.setattr(
        trigger_module, "publish_redeploy_start_event", lambda **_kwargs: 1
    )
    monkeypatch.setattr(
        trigger_module,
        "wait_for_rebuild_completion",
        lambda **_kwargs: {
            "correlation_id": "corr-123",
            "status": "failed",
            "errors": ["bad deploy"],
        },
    )

    result = CliRunner().invoke(
        trigger_module.main,
        [
            "--changed-files",
            _RUNTIME_CHANGE_FILE,
            "--base-branch",
            "dev",
            "--source-sha",
            _MERGE_SHA,
            "--correlation-id",
            "corr-123",
            "--wait-for-completion",
        ],
    )

    assert result.exit_code == 1
    assert "Received rebuild-completed status=failed" in result.output
    assert "bad deploy" in result.output
