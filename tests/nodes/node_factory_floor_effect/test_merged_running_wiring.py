# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor effect fills floor.merged_running from the count reader it is given, and names the cause when it cannot."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_merged_running_count import (
    ModelMergedRunningCountRequest,
    ModelMergedRunningCountResult,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorSection,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import load_contract
from omnimarket.nodes.node_merged_running_count_compute.handlers.handler_merged_running_count import (
    HandlerMergedRunningCount,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_contract_config import (
    load_contract_config,
)
from tests.nodes.node_factory_floor_effect.test_runtime_factory_floor import (
    CONTRACT,
    _argv,
)

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


class _Reader:
    def read(self, now: datetime) -> ModelMergedRunningCountResult:
        return HandlerMergedRunningCount().handle(
            ModelMergedRunningCountRequest(
                now=now, config=load_contract_config(), prs=(), hosts=()
            )
        )


class _Broken:
    def read(self, now: datetime) -> ModelMergedRunningCountResult:
        raise OSError("the PR watcher state is gone")


def _record(world: dict[str, Path], reader: Any) -> dict[str, Any]:
    handler = HandlerFactoryFloorEffect(merged_running=reader)
    request = ModelFactoryFloorRunRequest(
        plan_path_contains=("beta/plans/2026-10-",),
        now=NOW,
        ledger_rows_file=str(world["ledger"]),
        home=str(world["home"]),
        kb=str(world["kb"]),
    )
    result = handler.handle(request)
    document: dict[str, Any] = json.loads(Path(result.output_path).read_text())
    assert (
        Path(result.output_path)
        == world["home"] / load_contract(CONTRACT).runtime.output.relpath
    )
    return document


def test_merged_running_is_an_additive_optional_field_of_the_floor_section() -> None:
    field = ModelFloorSection.model_fields["merged_running"]
    assert field.is_required() is False
    assert field.default is None


def test_merged_running_is_filled_from_the_count_reader(
    world: dict[str, Path],
) -> None:
    document = _record(world, _Reader())
    section = document["floor"]["merged_running"]
    assert section["schema_id"] == "omnimarket.merged_running_count.v1"
    assert section["generated_at"].startswith("2026-10-31T12:00:00")
    assert document["sources"]["merged_running_cause"] == ""


def test_merged_running_is_null_with_a_named_cause_when_no_reader_is_bound(
    world: dict[str, Path],
) -> None:
    document = _record(world, None)
    assert document["floor"]["merged_running"] is None
    assert (
        document["sources"]["merged_running_cause"]
        == "no merged-running count reader is bound"
    )


def test_merged_running_reader_failure_leaves_the_floor_written_and_names_the_cause(
    world: dict[str, Path],
) -> None:
    document = _record(world, _Broken())
    assert document["floor"]["merged_running"] is None
    assert "the PR watcher state is gone" in document["sources"]["merged_running_cause"]
    assert document["lines"]


def test_merged_running_cli_binds_the_count_reader(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_factory_floor_effect import (
        runtime_factory_floor as runtime,
    )

    bound: list[object] = []
    monkeypatch.setattr(
        runtime, "bind_merged_running_reader", lambda: bound.append(1) or _Reader()
    )
    assert runtime.main(_argv(world, "--plan", "beta/plans/2026-10-")) == 0
    assert bound == [1]
    document = json.loads(
        (world["home"] / load_contract(CONTRACT).runtime.output.relpath).read_text()
    )
    assert document["floor"]["merged_running"] is not None
