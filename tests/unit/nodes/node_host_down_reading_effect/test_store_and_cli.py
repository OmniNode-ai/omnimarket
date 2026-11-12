# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The jsonl store, the CLI verbs and the placement reader of the host-down reading (OMN-19998)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_down_compute.models.model_host_down import (
    EnumHostState,
)
from omnimarket.nodes.node_host_down_reading_effect import runtime_host_down as cli
from omnimarket.nodes.node_host_down_reading_effect.adapters.stores import (
    InMemoryDownStore,
    JsonlDownStore,
)
from omnimarket.nodes.node_host_down_reading_effect.models.model_host_down_reading import (
    ModelHostDownRecord,
)
from omnimarket.nodes.node_host_down_reading_effect.overlay import (
    NODE_NAME,
    ModelHostDownOverlay,
)

from .conftest import HOST_A, HOST_B, NOW

pytestmark = [pytest.mark.unit]


def _record(host: str, state: EnumHostState, *, at=NOW) -> ModelHostDownRecord:
    return ModelHostDownRecord(
        host_name=host,
        state=state,
        first_failed_at=at - timedelta(seconds=900)
        if state is not EnumHostState.UP
        else None,
        down_since=at - timedelta(seconds=900) if state is EnumHostState.DOWN else None,
        at=at,
    )


@pytest.mark.parametrize("make", [InMemoryDownStore, lambda: None])
def test_both_stores_read_the_latest_record_per_host(make, tmp_path: Path) -> None:
    store = make() or JsonlDownStore(tmp_path / "state.jsonl")
    store.write(_record(HOST_A, EnumHostState.DOWN))
    store.write(_record(HOST_B, EnumHostState.DOWN))
    store.write(_record(HOST_B, EnumHostState.UP, at=NOW + timedelta(seconds=60)))
    assert store.down_hosts(NOW + timedelta(seconds=60), 600) == (HOST_A,)
    assert store.latest(HOST_B).state is EnumHostState.UP  # type: ignore[union-attr]


def test_a_down_record_older_than_the_ttl_no_longer_counts_even_when_the_outage_is_old(
    tmp_path: Path,
) -> None:
    store = JsonlDownStore(tmp_path / "state.jsonl")
    store.write(_record(HOST_A, EnumHostState.DOWN))
    assert store.down_hosts(NOW + timedelta(seconds=600), 600) == (HOST_A,)
    assert store.down_hosts(NOW + timedelta(seconds=601), 600) == ()


def test_the_jsonl_store_skips_a_line_that_does_not_parse(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    store = JsonlDownStore(path)
    store.write(_record(HOST_A, EnumHostState.DOWN))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    assert store.down_hosts(NOW, 600) == (HOST_A,)


def _jsonl(overlay: ModelHostDownOverlay) -> ModelHostDownOverlay:
    document = overlay.model_dump(mode="json")
    document["profile"] = "jsonl"
    return ModelHostDownOverlay.model_validate(document)


def test_the_cli_down_and_status_read_what_the_store_holds(
    overlay: ModelHostDownOverlay, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded = _jsonl(overlay)
    environ = {loaded.emission.home_env: str(tmp_path)}
    JsonlDownStore(tmp_path / loaded.jsonl_state_relpath).write(
        _record(HOST_A, EnumHostState.DOWN, at=cli.datetime.now(cli.UTC))
    )
    assert cli.run(["down"], overlay=loaded, environ=environ) == 0
    assert capsys.readouterr().out.split() == [HOST_A]
    assert cli.run(["status", "--host", HOST_A], overlay=loaded, environ=environ) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "down"


def test_a_sweep_from_a_host_outside_the_observers_fails_with_the_typed_code(
    overlay: ModelHostDownOverlay, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded = _jsonl(overlay)
    environ = {loaded.emission.home_env: str(tmp_path), loaded.observer_env: "the-mac"}
    assert cli.run(["sweep"], overlay=loaded, environ=environ) == 1
    assert json.loads(capsys.readouterr().out)["failure_code"] == (
        loaded.codes.observer_not_allowed
    )


def test_a_missing_deployment_fact_exits_3(overlay: ModelHostDownOverlay) -> None:
    assert cli.run(["down"], overlay=_jsonl(overlay), environ={}) == 3


def test_no_overlay_selected_reads_as_no_host_down() -> None:
    assert cli.read_down_hosts({}) == ()


def test_the_placement_reader_returns_the_hosts_the_store_holds(
    overlay: ModelHostDownOverlay, tmp_path: Path
) -> None:
    loaded = _jsonl(overlay)
    node_dir = tmp_path / "roots" / NODE_NAME
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(loaded.model_dump(mode="json")), encoding="utf-8"
    )
    environ = {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "roots"),
        loaded.emission.home_env: str(tmp_path / "home"),
    }
    JsonlDownStore(tmp_path / "home" / loaded.jsonl_state_relpath).write(
        _record(HOST_B, EnumHostState.DOWN)
    )
    assert cli.read_down_hosts(environ, now=NOW) == (HOST_B,)
