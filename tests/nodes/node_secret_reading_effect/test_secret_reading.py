# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The piece 5 reading: verdicts, the revoked-scope and planted-dummy controls, rows, backends."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.model_secret_reading import (
    EnumSecretProbeOutcome,
    ModelSecretHandle,
    ModelSecretReadingCommand,
)
from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
    ModelSecretStoreResult,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    SecretStoreEffectProbeStore,
)
from omnimarket.nodes.node_secret_reading_effect.overlay import (
    ModelSecretReadingOverlay,
)
from tests.nodes.node_secret_reading_effect.secret_reading_support import (
    DECIDER_VALUE,
    DUMMY_SURFACE,
    GATE_VALUE,
    LOG_SURFACE,
    handler,
    overlay,
    store,
    surfaces,
)

pytestmark = pytest.mark.unit

HANDLE = ModelSecretHandle(consumer="gate", folder="/shared/env", key="GATE_PASSWORD")


def _command(write: bool = True) -> ModelSecretReadingCommand:
    return ModelSecretReadingCommand(correlation_id="c-1", write=write)


def test_every_consumer_resolving_reads_working_and_writes_one_row() -> None:
    ledger = InMemoryLedger()
    result = handler(store(), ledger).handle(_command())
    assert result.status == "completed"
    assert result.verdict is EnumReadingVerdict.WORKING
    assert [p.outcome for p in result.probes] == [EnumSecretProbeOutcome.RESOLVED] * 2
    assert result.ledger == "written"
    assert len(ledger.lines) == 1
    row = ledger.lines[0]
    assert "| STATUS | lane=secret-reading | src=secret-reading |" in row
    assert "verdict=WORKING" in row
    assert "piece=5" in row
    assert "failed_consumers=none" in row
    assert "tag=operational" in row
    assert "planted_dummy=found" in row
    assert "exposures=0" in row


def test_revoking_one_consumers_scope_flips_the_reading_to_not_working() -> None:
    """The plan's failing control: revoke one consumer's scope in a test store."""
    test_store = store()
    test_store.revoke("decider")
    ledger = InMemoryLedger()
    result = handler(test_store, ledger).handle(_command())
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert "failed_consumers=decider:refused" in ledger.lines[0]
    assert "verdict=NOT_WORKING" in ledger.lines[0]


def test_a_missing_key_and_an_empty_key_read_not_working() -> None:
    from omnimarket.nodes.node_secret_reading_effect.adapters import (
        InMemorySecretProbeStore,
    )

    test_store = InMemorySecretProbeStore({("/shared/env", "GATE_PASSWORD"): ""})
    result = handler(test_store, None).handle(_command(write=False))
    assert [p.outcome for p in result.probes] == [
        EnumSecretProbeOutcome.EMPTY,
        EnumSecretProbeOutcome.NOT_FOUND,
    ]
    assert result.verdict is EnumReadingVerdict.NOT_WORKING


def test_a_store_that_is_down_reads_not_working_and_source_unreadable() -> None:
    test_store = store()
    test_store.down = True
    result = handler(test_store, None).handle(_command(write=False))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.outcome is not None
    assert result.outcome.value == "source-unreadable"


def test_a_scanner_that_misses_the_planted_dummy_reads_not_working() -> None:
    """The scan's control: a blind scanner must not read as a clean one."""
    blind = surfaces()
    blind.texts[DUMMY_SURFACE] = "the dummy is gone\n"
    result = handler(store(), None, blind).handle(_command(write=False))
    assert result.scan is not None
    assert result.scan.dummy_found is False
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert "planted-dummy-missed" in result.reasons


def test_a_resolved_value_found_on_a_surface_reads_not_working() -> None:
    leaked = surfaces()
    leaked.texts[LOG_SURFACE] = f"oops {DECIDER_VALUE} printed\n"
    ledger = InMemoryLedger()
    result = handler(store(), ledger, leaked).handle(_command())
    assert result.scan is not None
    assert result.scan.exposures == ("decider",)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert "exposures=1" in ledger.lines[0]
    assert DECIDER_VALUE not in ledger.lines[0]


def test_an_unreadable_surface_reads_not_working() -> None:
    broken = surfaces()
    broken.unreadable.add(LOG_SURFACE)
    result = handler(store(), None, broken).handle(_command(write=False))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.outcome is not None
    assert result.outcome.value == "source-unreadable"


def test_an_exposure_awaiting_rotation_keeps_the_reading_not_working() -> None:
    waiting = overlay(
        open_exposures=[
            {
                "name": "broker-password",
                "recorded_at": "2026-11-06T12:00:00Z",
                "awaiting": "operator rotation consent",
            }
        ]
    )
    ledger = InMemoryLedger()
    result = handler(store(), ledger, test_overlay=waiting).handle(_command())
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert "open_exposures=1" in ledger.lines[0]


def test_a_value_never_reaches_a_row_an_event_or_a_result() -> None:
    from omnimarket.nodes.node_secret_reading_effect.handlers.handler_secret_reading import (
        events_of,
        load_topics,
    )

    ledger = InMemoryLedger()
    result = handler(store(), ledger).handle(_command())
    text = (
        "".join(ledger.lines)
        + result.model_dump_json()
        + repr(events_of(result, load_topics()))
    )
    assert GATE_VALUE not in text
    assert DECIDER_VALUE not in text


def test_an_undeclared_consumer_is_a_typed_failure() -> None:
    command = ModelSecretReadingCommand(correlation_id="c-1", consumers=("nobody",))
    result = handler(store(), InMemoryLedger()).handle(command)
    assert result.status == "failed"
    assert result.failure == "consumers-not-declared:nobody"


def test_a_writing_run_with_no_appender_is_a_typed_failure() -> None:
    result = handler(store(), None).handle(_command())
    assert result.status == "failed"
    assert result.failure == "no-appender-for-a-writing-run"


def test_one_named_consumer_probes_only_that_consumer() -> None:
    command = ModelSecretReadingCommand(correlation_id="c-1", consumers=("gate",))
    result = handler(store(), None).handle(command)
    assert [p.consumer for p in result.probes] == ["gate"]


@pytest.mark.parametrize(
    ("store_outcome", "value", "expected"),
    [
        (EnumSecretStoreOutcome.RESOLVED, "x", EnumSecretProbeOutcome.RESOLVED),
        (EnumSecretStoreOutcome.RESOLVED, "", EnumSecretProbeOutcome.EMPTY),
        (EnumSecretStoreOutcome.NOT_FOUND, None, EnumSecretProbeOutcome.NOT_FOUND),
        (EnumSecretStoreOutcome.REFUSED, None, EnumSecretProbeOutcome.REFUSED),
        (EnumSecretStoreOutcome.UNREACHABLE, None, EnumSecretProbeOutcome.UNREACHABLE),
        (
            EnumSecretStoreOutcome.NOT_CONFIGURED,
            None,
            EnumSecretProbeOutcome.NOT_CONFIGURED,
        ),
        (EnumSecretStoreOutcome.INVALID_REQUEST, None, EnumSecretProbeOutcome.FAILED),
    ],
)
def test_the_store_effect_backend_keeps_the_outcome_only(
    store_outcome: EnumSecretStoreOutcome,
    value: str | None,
    expected: EnumSecretProbeOutcome,
) -> None:
    seen: list[ModelSecretStoreRequest] = []

    def resolve(request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
        seen.append(request)
        return ModelSecretStoreResult(
            operation=EnumSecretStoreOperation.RESOLVE,
            outcome=store_outcome,
            value=SecretStr(value) if value is not None else None,
        )

    resolved = SecretStoreEffectProbeStore(resolve).resolve(HANDLE)
    assert resolved.outcome is expected
    assert "value" not in resolved.model_dump()
    assert seen[0].operation is EnumSecretStoreOperation.RESOLVE
    assert (seen[0].folder, seen[0].key) == ("/shared/env", "GATE_PASSWORD")


def test_the_store_effect_backend_never_raises() -> None:
    def resolve(request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
        raise OSError("down")

    outcome = SecretStoreEffectProbeStore(resolve).resolve(HANDLE).outcome
    assert outcome is EnumSecretProbeOutcome.UNREACHABLE


def test_a_consumer_declared_twice_refuses_the_overlay() -> None:
    document = overlay().model_dump(mode="json")
    document["consumers"].append(document["consumers"][0])
    with pytest.raises(ValidationError):
        ModelSecretReadingOverlay.model_validate(document)


def test_the_shipped_example_overlay_loads() -> None:
    from pathlib import Path

    import yaml

    import omnimarket.nodes.node_secret_reading_effect as node

    path = Path(node.__file__).parent / "overlay.example.yaml"
    loaded = ModelSecretReadingOverlay.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    assert loaded.store.secret_store is not None
    assert loaded.store.backend_name == "secret-store-effect"
