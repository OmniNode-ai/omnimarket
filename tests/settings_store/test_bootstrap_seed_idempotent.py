# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC6 (Task 21): the bootstrap seeds the slice overlay documents through the write path, idempotently, and renders a bindings-only file."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from omnimarket.handlers.overlay_store.bootstrap_file import (
    BOOTSTRAP_BINDINGS,
    BootstrapInputMissingError,
    parse_bootstrap_file,
    render_bootstrap_file,
)
from omnimarket.handlers.overlay_store.settings_seed import (
    ModelSeedResult,
    SeedDisagreementError,
    seed_defaults,
)
from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore

NOW = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
INTAKE = {name: f"value-of-{name}" for name in BOOTSTRAP_BINDINGS}
DEFAULTS: dict[str, dict[str, JsonValue]] = {
    "node_floor_fill_orchestrator": {"floor_fill": {"sent_window_minutes": 30}},
    "lab_hosts": {"hosts": []},
}


class _Pipeline:
    """The projection's write path over the table; nothing writes Valkey (the table is the record, OMN-24028)."""

    def __init__(self, *, table_drops: bool = False) -> None:
        self.table = InMemoryOverlayStore()
        self.writes = 0
        self._drops = table_drops

    async def write(self, record: Mapping[str, JsonValue]) -> None:
        self.writes += 1
        if not self._drops:
            await self.table.write_version(record)


async def _seed(pipe: _Pipeline, intake: Mapping[str, str] = INTAKE) -> ModelSeedResult:
    return await seed_defaults(
        intake,
        DEFAULTS,
        table=pipe.table,
        write=pipe.write,
        source_ref="settings-defaults@test",
        loaded_by="bootstrap",
        now=NOW,
    )


async def test_bootstrap_seed_idempotent_first_run_writes_both_overlays() -> None:
    pipe = _Pipeline()
    result = await _seed(pipe)
    assert sorted(result.written) == ["lab_hosts", "node_floor_fill_orchestrator"]
    assert pipe.writes == 2


async def test_bootstrap_seed_idempotent_a_second_run_writes_nothing() -> None:
    pipe = _Pipeline()
    await _seed(pipe)
    result = await _seed(pipe)
    assert result.written == ()
    assert sorted(result.unchanged) == ["lab_hosts", "node_floor_fill_orchestrator"]
    assert pipe.writes == 2


async def test_bootstrap_seed_idempotent_the_table_holds_each_default_after_one_run() -> (
    None
):
    pipe = _Pipeline()
    await _seed(pipe)
    for overlay_id, document in DEFAULTS.items():
        in_table = await pipe.table.read_current(overlay_id)
        assert in_table is not None
        assert in_table["document"] == document


async def test_bootstrap_seed_idempotent_a_table_that_lacks_the_record_is_a_disagreement() -> (
    None
):
    with pytest.raises(SeedDisagreementError, match="lab_hosts"):
        await _seed(_Pipeline(table_drops=True))


@pytest.mark.parametrize("missing", BOOTSTRAP_BINDINGS)
async def test_bootstrap_seed_idempotent_a_missing_input_refuses_by_name_and_writes_nothing(
    missing: str,
) -> None:
    pipe = _Pipeline()
    intake = {k: v for k, v in INTAKE.items() if k != missing}
    with pytest.raises(BootstrapInputMissingError, match=missing):
        await _seed(pipe, intake)
    assert pipe.writes == 0


def test_bootstrap_seed_idempotent_the_host_file_holds_the_binding_names_and_no_other() -> (
    None
):
    parsed = parse_bootstrap_file(
        render_bootstrap_file({**INTAKE, "SOME_SETTING": "9", "A_PASSWORD": "x"})
    )
    assert sorted(parsed) == sorted(BOOTSTRAP_BINDINGS)


def test_bootstrap_seed_idempotent_a_setting_planted_in_a_host_file_refuses() -> None:
    text = render_bootstrap_file(INTAKE) + "sent_window_minutes=30\n"
    with pytest.raises(BootstrapInputMissingError, match="sent_window_minutes"):
        parse_bootstrap_file(text)
