# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21305: a delegation refused during routing writes its one failed row.

A keyless ``onex delegate`` on a fresh HOME is refused before any rung runs: the
local dispatch port raises "No local model is declared" from
``refuse_undeclared_local_model``. The receipt records the refusal, but nothing
wrote a ``delegation_events`` row, so the Runs page had no failed run to show
(measured on the lab host 2026-10-21, recorded on OMN-19919, gate OMN-19919 AC-8).

* AC1 -- the refused run publishes exactly one typed failed terminal on the
  node's declared failure topic, and leaves exactly one row for its correlation
  id, with ``terminal_ok`` 0 and a non-null cause, and no model-call row (no
  call was made).
* AC2 -- the row is built from a terminal that passes the same construction guard
  a dispatched run's terminal does: no attempts, so its cause is
  ``refused_before_dispatch`` (the OMN-19004 invariant); a terminal claiming any
  other cause with no attempts is refused by that guard.

The refusal itself still reaches the caller unchanged: the message that names
both remedies is OMN-19442's contract and stays as it is.

Positive control: the same harness with a declared local model writes its row
with ``terminal_ok`` 1, so a store that is simply never written cannot pass.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire import EnumDelegationTerminalFailureCause
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.local_deployment.tenant_identity import read_local_tenant_identity
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
    _local_terminal_topic,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution as resolution_mod
from tests.test_omn19442_declared_local_model_serves_the_ladder import _LOOPBACK

pytestmark = pytest.mark.unit

_SERVED_MODEL = "customer-local-model"
_GOOD_DOCUMENT = (
    "### ANSWER\n"
    "The sky is blue because Rayleigh scattering by air molecules scatters "
    "short blue wavelengths of sunlight far more strongly than red ones."
)


class _RecordingEffect:
    """Stands in for the provider call and records every request it is handed."""

    def __init__(self) -> None:
        self.calls: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_DOCUMENT,
            tokens_in=12,
            tokens_out=30,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


@pytest.fixture
def clean_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """The shipped contracts, the customer's overlay file, and no key anywhere."""
    overlay = tmp_path / "home" / ".omninode" / "delegation" / "bifrost_overrides.yaml"
    overlay.parent.mkdir(parents=True)
    for key in (
        "BIFROST_CONTRACT_PATH",
        "DELEGATION_ROUTING_TIERS_PATH",
        "TASK_CLASS_CONTRACT_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    monkeypatch.setattr(resolution_mod, "_OVERLAY_PATH", overlay)
    monkeypatch.setattr(
        routing,
        "api_key_ref_available",
        lambda api_key_ref, **_k: not api_key_ref,
    )
    routing._load_bifrost_endpoints.cache_clear()
    yield overlay
    routing._load_bifrost_endpoints.cache_clear()


def _declare(overlay: Path, *backend_ids: str) -> None:
    lines = ["backends:"]
    for backend_id in backend_ids:
        lines += [
            f"  - backend_id: {backend_id}",
            f'    endpoint_url: "{_LOOPBACK}"',
            f'    model_name: "{_SERVED_MODEL}"',
        ]
    overlay.write_text("\n".join(lines) + "\n" if backend_ids else "backends: []\n")


def _port(tmp_path: Path, effect: _RecordingEffect) -> LocalDelegationDispatchPort:
    return LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )


def _install_tenant() -> str:
    """This install's own identity, as ``onex local init`` mints it on a fresh HOME.

    It is a customer tenant (a UUID, not the house), so the refusal applies, and
    it is recorded in the store, so its row can be written (OMN-18699).
    """
    identity = read_local_tenant_identity()
    assert identity is not None, "the session fixture mints the install identity"
    return str(identity.tenant_uuid)


def _dispatch(port: LocalDelegationDispatchPort, correlation_id: UUID) -> object:
    return asyncio.run(
        port.dispatch(
            prompt="say hello in one word",
            task_type="document",
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=_install_tenant(),
        )
    )


def _rows(db_path: Path, table: str, correlation_id: UUID) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if exists is None:
            return []
        return [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {table} WHERE correlation_id = ?",
                (str(correlation_id),),
            ).fetchall()
        ]
    finally:
        conn.close()


class _RecordingPublisher:
    """Stands in for the port's terminal publisher and records every publish."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> None:
        self.published.append(kwargs)


def test_ac1_a_routing_refusal_leaves_one_failed_row_with_its_cause(
    clean_install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(clean_install)
    effect = _RecordingEffect()
    correlation_id = uuid4()
    port = _port(tmp_path, effect)
    publisher = _RecordingPublisher()
    monkeypatch.setattr(port, "_terminal_publisher", publisher)

    with pytest.raises(ProtocolConfigurationError) as exc_info:
        _dispatch(port, correlation_id)

    # The refusal still reaches the caller, naming both remedies (OMN-19442).
    assert "No local model is declared" in str(exc_info.value)
    assert effect.calls == []

    # Exactly one typed delegation terminal is published, on the node's declared
    # failure topic, for this run.
    assert len(publisher.published) == 1, publisher.published
    (published,) = publisher.published
    assert published["topic"] == _local_terminal_topic(success=False)
    assert published["event_type"] == "delegate_skill.failed"
    assert published["correlation_id"] == str(correlation_id)
    terminal = ModelDelegateSkillTerminalProjection.from_payload(published["payload"])
    assert terminal.status == "failed"
    assert (
        terminal.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.REFUSED_BEFORE_DISPATCH
    )

    rows = _rows(tmp_path / "evidence.sqlite", "delegation_events", correlation_id)
    assert len(rows) == 1, rows
    (row,) = rows
    assert row["terminal_ok"] in (0, False)
    assert row["terminal_failure_cause"] == "refused_before_dispatch"
    assert row["tenant_id"] == _install_tenant()
    # No rung ran, so no model call is recorded for this run.
    assert _rows(tmp_path / "evidence.sqlite", "llm_call_metrics", correlation_id) == []


def test_ac2_the_refusal_row_comes_from_a_guarded_zero_attempt_terminal(
    clean_install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(clean_install)
    port = _port(tmp_path, _RecordingEffect())
    seen: list[ModelDelegateSkillTerminalProjection] = []
    project = port._projection_handler.project_delegate_skill_terminal

    def record(terminal: ModelDelegateSkillTerminalProjection, db: object) -> object:
        seen.append(terminal)
        return project(terminal, db)

    monkeypatch.setattr(
        port._projection_handler, "project_delegate_skill_terminal", record
    )
    with pytest.raises(ProtocolConfigurationError):
        _dispatch(port, uuid4())

    assert len(seen) == 1
    (terminal,) = seen
    assert terminal.status == "failed"
    assert tuple(terminal.attempts) == ()
    assert terminal.attempts_count == 0
    assert (
        terminal.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.REFUSED_BEFORE_DISPATCH
    )

    # The guard is live: the same terminal claiming any other cause with no
    # attempts is refused at construction (OMN-19004).
    claimed = terminal.model_dump(mode="json")
    claimed["terminal_failure_cause"] = (
        EnumDelegationTerminalFailureCause.PROVIDER_ERROR.value
    )
    with pytest.raises(ValueError, match="reserved for a request refused before"):
        ModelDelegateSkillTerminalProjection.model_validate(claimed)


def test_control_a_declared_local_model_writes_its_row_as_before(
    clean_install: Path, tmp_path: Path
) -> None:
    _declare(clean_install, "local-coder")
    effect = _RecordingEffect()
    correlation_id = uuid4()

    _dispatch(_port(tmp_path, effect), correlation_id)

    assert len(effect.calls) >= 1
    rows = _rows(tmp_path / "evidence.sqlite", "delegation_events", correlation_id)
    assert len(rows) == 1, rows
    assert rows[0]["terminal_ok"] in (1, True)
    # The same query finds the model-call row a dispatched run writes, so the
    # empty result in AC1 is a fact about the refusal, not a blind query.
    assert _rows(tmp_path / "evidence.sqlite", "llm_call_metrics", correlation_id)
