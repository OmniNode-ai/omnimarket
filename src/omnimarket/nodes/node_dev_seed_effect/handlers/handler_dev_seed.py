# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dev and demo seed: labelled fixture delegations through the real projection.

OMN-19970. The dashboard pages need rows before the real delegation chain is
complete, and the operator ruling of 2026-09-28T15:01:30Z allows fixture data
only through the real producers and projections, labelled, never mocked in a UI.

Two paths, one projection (spec amendment 1):

* Local store (Mode 1). ``onex delegate`` has no bus hop: it builds a
  delegate-skill terminal and calls
  ``HandlerProjectionDelegation.project_delegate_skill_terminal`` directly
  (``port_local_delegation_dispatch._project_evidence``). :meth:`seed_local`
  calls that same entry point with ``data_source="fixture"``. It is not a
  direct database write.
* A lane's broker. :meth:`wire_messages` returns the same terminals as envelope
  bytes carrying ``metadata.tags["data_source"] = "fixture"``. The ``onex seed``
  shim publishes them on the delegate-skill completed topic, and the standalone
  projection runner reads the tag through ``envelope_data_source``.

Correlation ids are ``uuid5`` of (fixture-set version, run key), and the
projection upserts on ``correlation_id``, so seeding twice writes the same rows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_dev_seed_effect.models.model_dev_seed_fixture_set import (
    ModelDevSeedFixtureRun,
    ModelDevSeedFixtureSet,
)
from omnimarket.nodes.node_dev_seed_effect.models.model_dev_seed_request import (
    ModelDevSeedRequest,
)
from omnimarket.nodes.node_dev_seed_effect.models.model_dev_seed_result import (
    ModelDevSeedResult,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.envelope import DATA_SOURCE_FIXTURE, DATA_SOURCE_TAG
from omnimarket.projection.protocol_database import DatabaseAdapter
from omnimarket.projection.snapshot_publisher import ModelSnapshotDeltaMessage

FIXTURE_SET_RESOURCE: Final[str] = "fixtures/fixture_set_v1.yaml"
#: Tag naming the fixture set on every published envelope, beside the label.
FIXTURE_SET_VERSION_TAG: Final[str] = "fixture_set_version"
_ID_NAMESPACE: Final[str] = "omnimarket.dev-seed"


class _NoRepublishPublisher:
    """Republishes nothing, for the reason the local port's publisher does.

    The projection's default publisher is the ambient Kafka broker. A seeded
    local store is not a lane's store, so its rows must not be served on a
    lane's snapshot topic (OMN-19193, ``_LocalEvidenceNoRepublishPublisher``).
    """

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        return True


def fixture_correlation_id(fixture_set_version: int, key: str) -> UUID:
    """The deterministic id a fixture run is written under."""
    return uuid5(NAMESPACE_URL, f"{_ID_NAMESPACE}/v{fixture_set_version}/{key}")


class HandlerDevSeed:
    """Projects the declared fixture set, every row labelled ``fixture``."""

    def load_fixture_set(self) -> ModelDevSeedFixtureSet:
        raw = (
            files("omnimarket.nodes.node_dev_seed_effect")
            .joinpath(FIXTURE_SET_RESOURCE)
            .read_text(encoding="utf-8")
        )
        return ModelDevSeedFixtureSet.model_validate(yaml.safe_load(raw))

    def terminal_payloads(
        self, request: ModelDevSeedRequest
    ) -> list[dict[str, object]]:
        """The fixture set as delegate-skill terminal payloads, oldest first."""
        fixture_set = self.load_fixture_set()
        seed_time = request.seed_time(datetime.now(tz=UTC))
        return [
            self._payload(fixture_set.fixture_set_version, run, request, seed_time)
            for run in sorted(fixture_set.runs, key=lambda r: -r.days_ago)
        ]

    def seed_local(
        self,
        db: DatabaseAdapter,
        *,
        tenant_id: str | None,
        now: datetime | None = None,
    ) -> ModelDevSeedResult:
        """Project every fixture run into ``db`` through the real projection."""
        request = ModelDevSeedRequest(tenant_id=tenant_id, now=now)
        projection = HandlerProjectionDelegation(publisher=_NoRepublishPublisher())
        payloads = self.terminal_payloads(request)
        written = 0
        for payload in payloads:
            terminal = ModelDelegateSkillTerminalProjection.from_payload(payload)
            result = projection.project_delegate_skill_terminal(
                terminal, db, data_source=DATA_SOURCE_FIXTURE
            )
            written += result.rows_upserted
        return ModelDevSeedResult(
            fixture_set_version=self.load_fixture_set().fixture_set_version,
            rows_projected=written,
            correlation_ids=tuple(str(p["correlation_id"]) for p in payloads),
        )

    def wire_messages(self, request: ModelDevSeedRequest) -> list[tuple[bytes, bytes]]:
        """(key, value) pairs for the delegate-skill completed topic.

        The value is the envelope shape ``unwrap_envelope`` reads: the terminal
        under ``payload``, and the fixture label under ``metadata.tags``.
        """
        version = self.load_fixture_set().fixture_set_version
        messages: list[tuple[bytes, bytes]] = []
        for payload in self.terminal_payloads(request):
            envelope: dict[str, object] = {
                "payload": payload,
                "correlation_id": payload["correlation_id"],
                "envelope_timestamp": payload["emitted_at"],
                "metadata": {
                    "tags": {
                        DATA_SOURCE_TAG: DATA_SOURCE_FIXTURE,
                        FIXTURE_SET_VERSION_TAG: str(version),
                    }
                },
            }
            if request.tenant_id:
                envelope["tenant_id"] = request.tenant_id
            messages.append(
                (
                    str(payload["correlation_id"]).encode("utf-8"),
                    json.dumps(envelope).encode("utf-8"),
                )
            )
        return messages

    def handle(self, request: ModelDevSeedRequest) -> ModelDevSeedResult:
        """Contract entry point: report the set a seed of ``request`` would write.

        The effect itself runs through :meth:`seed_local` (a store) or the
        ``onex seed`` shim (a broker), which own the transport.
        """
        payloads = self.terminal_payloads(request)
        return ModelDevSeedResult(
            fixture_set_version=self.load_fixture_set().fixture_set_version,
            rows_projected=0,
            correlation_ids=tuple(str(p["correlation_id"]) for p in payloads),
        )

    @staticmethod
    def _payload(
        version: int,
        run: ModelDevSeedFixtureRun,
        request: ModelDevSeedRequest,
        seed_time: datetime,
    ) -> dict[str, object]:
        completed = run.status == "completed"
        payload: dict[str, object] = {
            "status": run.status,
            "correlation_id": str(fixture_correlation_id(version, run.key)),
            "task_type": run.task_type,
            "provider": run.provider,
            "model_name": run.model_name,
            "response": "" if not completed else f"[fixture {run.key}]",
            "quality_gate_passed": completed,
            "quality_gates_failed": [] if completed else [run.error_message],
            "error_message": run.error_message,
            "emitted_at": (seed_time - timedelta(days=run.days_ago)).isoformat(),
            "metrics": {
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "total_tokens": run.input_tokens + run.output_tokens,
                "latency_ms": run.latency_ms,
                "cost_usd": float(run.cost_usd),
                "cost_savings_usd": float(run.cost_savings_usd),
            },
        }
        if request.tenant_id:
            payload["tenant_id"] = request.tenant_id
        return payload
