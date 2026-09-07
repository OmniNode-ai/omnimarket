# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17985 -- a tenant-identity refusal must quarantine, not wedge a partition.

WHAT HAPPENED, from the pod's own log (omninode_infra probe run 34073692873,
2026-09-07T01:40Z, dev-system `i-06169517a92b45f86`, namespace `onex-dev`)::

    RECOVERABLE error projecting onex.evt.omnibase-infra.delegation-failed.v1
    (offset uncommitted, will retry): OMN-16804: no canonical UUID for verified
    tenant slug 'operator-ledger-probe'. tenant_registry_mirror holds no row for
    it and it predates no legacy mapping. ...
    Consumer attempt 7/10 failed: ... Retrying in 30.0s
    ...
    Consumer attempt 10/10 failed: ...
    Consumer failed after 10 retries

    lastState.terminated reason=Completed exitCode=0

Two defects, both pinned below.

**1. The classification contradicted its own class docstring.**
``TenantRegistryResolutionError``'s docstring states plainly that "the
projection runner classifies it POISON and routes the event to quarantine ...
Quarantine is the correct terminal state for an unattributable event." It was
never listed in ``_POISON_TYPES``. It subclasses ``ValueError``, and only
pydantic's ``ValidationError`` is a POISON type, so it fell through to the
``RECOVERABLE`` default: offset uncommitted, message re-read, identical refusal,
forever. The same latent gap covered ``UnmappedTenantIdentityError`` and
``TenantRequiredError`` -- both likewise properties of the EVENT's identity that
no amount of retrying can change.

Consequence: the partition never advanced. Every delegation event behind that
offset was blocked, `public.delegation_events`, `delegation_shadow_comparisons`
and `generation_events` all held 0 rows for 14+ hours, and each re-read tore the
consumer down and rejoined the group -- ~1,600 group generations in sixteen
minutes, which a reader mistook for healthy membership.

**2. Giving up exited 0.** After ``MAX_RETRY_ATTEMPTS`` the run loop logged
"Consumer failed after N retries" and RETURNED, so the process exited **0** and
the kubelet recorded ``reason=Completed exitCode=0``. A fatal give-up was
indistinguishable from a clean shutdown in every surface that reads the
termination record. `attempts` also never reset, so ten TRANSIENT blips across a
whole process lifetime were equally fatal.

``TenantContextMissingError`` is deliberately NOT reclassified: it is the READ
side, raised when the ``app.tenant_id`` GUC is unset. That is a configuration
fault that self-heals when the seam is set, not a property of the event.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import pytest

from omnimarket.projection.error_classification import (
    ProjectionErrorClass,
    classify_projection_error,
)
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    ModelProjectionRuntimeBinding,
)
from omnimarket.projection.tenant_isolation import (
    TenantContextMissingError,
    TenantRequiredError,
    UnmappedTenantIdentityError,
)
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
)

pytestmark = pytest.mark.unit

# The refusal text the live pod logged, kept verbatim so the fix is anchored to
# the event that actually wedged the partition rather than to a paraphrase.
LIVE_REFUSAL = (
    "OMN-16804: no canonical UUID for verified tenant slug "
    "'operator-ledger-probe'. tenant_registry_mirror holds no row for it and "
    "it predates no legacy mapping."
)


class TestTenantIdentityRefusalsArePoison:
    def test_registry_resolution_error_is_poison(self) -> None:
        """The exact exception that wedged onex-dev for 14+ hours."""
        assert (
            classify_projection_error(TenantRegistryResolutionError(LIVE_REFUSAL))
            is ProjectionErrorClass.POISON
        )

    def test_unmapped_tenant_identity_error_is_poison(self) -> None:
        assert (
            classify_projection_error(UnmappedTenantIdentityError("no mapping"))
            is ProjectionErrorClass.POISON
        )

    def test_tenant_required_error_is_poison(self) -> None:
        assert (
            classify_projection_error(TenantRequiredError("no tenant_id on event"))
            is ProjectionErrorClass.POISON
        )

    def test_read_side_tenant_context_missing_stays_recoverable(self) -> None:
        """NEGATIVE CONTROL: the read-side GUC fault is config, not the event.

        Without this, "make the tenant errors poison" would be indistinguishable
        from "make every ValueError poison", which would quarantine real events
        on a fixable misconfiguration.
        """
        assert (
            classify_projection_error(TenantContextMissingError("GUC unset"))
            is ProjectionErrorClass.RECOVERABLE
        )

    def test_a_plain_value_error_is_still_recoverable(self) -> None:
        """NEGATIVE CONTROL: the new entries are named types, not `ValueError`."""
        assert (
            classify_projection_error(ValueError("something unexpected"))
            is ProjectionErrorClass.RECOVERABLE
        )

    def test_migration_gap_is_still_recoverable(self) -> None:
        """POSITIVE CONTROL for the untouched policy (OMN-13634).

        A not-yet-applied migration must still be retried until the schema
        catches up -- never quarantined as malformed.
        """
        assert (
            classify_projection_error(
                asyncpg.exceptions.UndefinedColumnError('column "x" does not exist')
            )
            is ProjectionErrorClass.RECOVERABLE
        )


class _RecordingConsumer:
    def __init__(self) -> None:
        self.commits: list[dict[Any, int]] = []

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commits.append(offsets)


class _Msg:
    def __init__(
        self, *, topic: str, partition: int, offset: int, value: bytes
    ) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.value = value


class _RefusingRunner(BaseProjectionRunner):
    """A runner whose project_event raises the live tenant-resolution refusal."""

    POISON_DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-malformed.v1"

    def __init__(self, *, raises: BaseException) -> None:
        super().__init__(
            runtime_binding=ModelProjectionRuntimeBinding(
                kafka_bootstrap_servers="redpanda.test:9092",
                database_url="postgresql://p:x@db.test:5432/projections",
            )
        )
        self._raises = raises
        self.dlq_published: list[tuple[str, bytes]] = []
        self._consumer = _RecordingConsumer()  # type: ignore[assignment]

    @property
    def topics(self) -> list[str]:
        return ["onex.evt.omnibase-infra.delegation-failed.v1"]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        raise self._raises

    @property
    def poison_dlq_topics(self) -> list[str]:
        return [self.POISON_DLQ_TOPIC]

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        self.dlq_published.append((topic, value))

    async def _update_watermark(self, projection_name: str, offset: int) -> None:
        return None


def _live_msg(offset: int = 17) -> _Msg:
    return _Msg(
        topic="onex.evt.omnibase-infra.delegation-failed.v1",
        partition=0,
        offset=offset,
        value=b'{"payload": {"correlation_id": "c-wedged"}}',
    )


class TestTheWedgedPartitionAdvances:
    @pytest.mark.asyncio
    async def test_the_live_refusal_dlqs_and_advances_the_offset(self) -> None:
        """The whole point: the partition must not stay wedged."""
        runner = _RefusingRunner(raises=TenantRegistryResolutionError(LIVE_REFUSAL))

        await runner._handle_message(_live_msg(offset=17))

        commits = runner._consumer.commits  # type: ignore[attr-defined]
        assert list(commits[0].values()) == [18], (
            "the offset must advance -- an unattributable event that no retry can "
            "resolve wedged nine partitions and held three tables at zero rows"
        )
        assert len(runner.dlq_published) == 1
        topic, value = runner.dlq_published[0]
        assert topic == _RefusingRunner.POISON_DLQ_TOPIC
        envelope = json.loads(value.decode("utf-8"))
        assert envelope["correlation_id"] == "c-wedged", (
            "the event stays replayable by correlation_id once the registry "
            "catches up -- quarantined, not dropped"
        )
        assert "operator-ledger-probe" in envelope["failure_reason"], (
            "the typed refusal must survive onto the DLQ, so a reader can tell "
            "an identity refusal from a malformed payload"
        )

    @pytest.mark.asyncio
    async def test_a_migration_gap_still_holds_the_offset(self) -> None:
        """POSITIVE CONTROL: the untouched RECOVERABLE policy still applies."""
        runner = _RefusingRunner(
            raises=asyncpg.exceptions.UndefinedColumnError('column "x" missing')
        )
        with pytest.raises(asyncpg.exceptions.UndefinedColumnError):
            await runner._handle_message(_live_msg(offset=17))

        assert runner._consumer.commits == []  # type: ignore[attr-defined]
        assert runner.dlq_published == []


# ---------------------------------------------------------------------------
# Defect 2: giving up exited 0, so a fatal give-up was indistinguishable from a
# clean shutdown in every surface that reads the kubelet's termination record.
# ---------------------------------------------------------------------------


class _AlwaysFailingConsumer:
    """Stands in for AIOKafkaConsumer; every session fails to start."""

    started = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def start(self) -> None:
        type(self).started += 1
        raise ConnectionError("broker unreachable")

    async def stop(self) -> None:
        return None


class _OneGoodSessionConsumer:
    """Starts, yields one message, then fails -- i.e. a session that PROGRESSED."""

    sessions = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def start(self) -> None:
        type(self).sessions += 1

    async def stop(self) -> None:
        return None

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        raise ConnectionError("broker dropped mid-session")


class _RunLoopRunner(BaseProjectionRunner):
    def __init__(self) -> None:
        super().__init__(
            runtime_binding=ModelProjectionRuntimeBinding(
                kafka_bootstrap_servers="redpanda.test:9092",
                database_url="postgresql://p:x@db.test:5432/projections",
            )
        )

    @property
    def topics(self) -> list[str]:
        return ["onex.evt.omnibase-infra.delegation-failed.v1"]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        return True


def _neutralize_io(runner: BaseProjectionRunner, monkeypatch: Any) -> None:
    """Remove every side effect `run()` has other than the retry loop itself."""
    from unittest.mock import AsyncMock

    runner._db = AsyncMock()  # type: ignore[assignment]
    monkeypatch.setattr(runner, "_start_health_server_if_configured", lambda: None)
    monkeypatch.setattr(runner, "_stop_health_server", lambda: None)


class TestGivingUpIsNotACleanExit:
    @pytest.mark.asyncio
    async def test_exhausting_the_retries_raises_instead_of_returning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`reason=Completed exitCode=0` is what made 13 hours of crashing invisible.

        Run 34073692873 read `lastState.terminated reason=Completed exitCode=0`
        off the crash-looping pod: the process gave up and returned, so Python
        exited 0 and the kubelet recorded a clean completion.
        """
        from omnimarket.projection import runner as runner_module

        monkeypatch.setattr(runner_module, "AIOKafkaConsumer", _AlwaysFailingConsumer)
        monkeypatch.setattr(runner_module, "MAX_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr(runner_module, "RETRY_BASE_DELAY", 0.0)
        monkeypatch.setattr(runner_module, "RETRY_MAX_DELAY", 0.0)
        monkeypatch.setattr(
            runner_module,
            "build_aiokafka_auth_kwargs_from_env",
            lambda: {},
            raising=False,
        )

        runner = _RunLoopRunner()
        _neutralize_io(runner, monkeypatch)

        with pytest.raises(runner_module.ProjectionConsumerExhaustedError):
            await runner.run()

    @pytest.mark.asyncio
    async def test_a_requested_shutdown_still_returns_cleanly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEGATIVE CONTROL: a real shutdown must NOT become a non-zero exit.

        Without this, "make give-up fatal" would be indistinguishable from
        "make every exit fatal", and every SIGTERM would look like a crash.
        """
        from omnimarket.projection import runner as runner_module

        monkeypatch.setattr(runner_module, "AIOKafkaConsumer", _AlwaysFailingConsumer)
        monkeypatch.setattr(
            runner_module,
            "build_aiokafka_auth_kwargs_from_env",
            lambda: {},
            raising=False,
        )

        runner = _RunLoopRunner()
        _neutralize_io(runner, monkeypatch)
        runner._shutdown_requested = True

        await runner.run()
