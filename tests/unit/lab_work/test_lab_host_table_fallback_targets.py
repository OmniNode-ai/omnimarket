# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab host row may list ``fallback_targets``.

The launcher's lab host table gained ``fallback_targets`` on its h101 and h053 rows; the typed row
model forbade the field, so lab placement refused the whole table ("could_not_check"). The fixture
below copies the keys and shape of those two rows as recorded; addresses and ssh names are placeholders.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_lab_placement_compute.handlers import HandlerLabPoolPlace
from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_placement import (
    load_host_table,
)
from omnimarket.nodes.node_lab_placement_compute.models import (
    EnumPlacementDecision,
    ModelLabPoolPlaceRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    ModelHostCapacityAdvertisement,
)

pytestmark = pytest.mark.unit

# The h101 and h053 rows as the launcher's table carried them, `fallback_targets` written inline.
RECORDED_TABLE = """\
bus_lane: dev
hosts:
  - name: h101
    target: host-101.example.invalid
    fallback_targets: [alias-101]
    codex: true
    cap: 12
    tenant_id: 00000000-0000-0000-0000-000000000000
    ledger_bus_lane: dev
    ledger_bus_python: .onex-dispatch-venv/bin/python
  - name: h053
    target: host-053
    fallback_targets: [alias-053]
    codex: true
    cap: 7
    unit_cores: 1.25
    penalty: 0.0
    tenant_id: 00000000-0000-0000-0000-000000000000
    ledger_bus_lane: dev
    ledger_bus_python: .onex-dispatch-venv/bin/python
    parity_python: .onex-dispatch-venv/bin/python
    clones:
      - repo: OmniNode-ai/omnimarket
        ref: dev
        path: $OMNI_HOME/omnimarket
      - repo: OmniNode-ai/placeholder-repo
        ref: main
        path: /srv/placeholder/placeholder-repo
"""

NOW = datetime(2026, 11, 2, 15, 0, 0, tzinfo=UTC)


def _recorded_table_file(tmp_path: Path) -> Path:
    path = tmp_path / "lab_run_hosts.yaml"
    path.write_text(RECORDED_TABLE, encoding="utf-8")
    return path


def test_lab_host_table_fallback_targets(tmp_path: Path) -> None:
    """The recorded table validates through the table model the placement reader uses."""
    table = load_host_table({"ONEX_LAB_RUN_HOSTS": str(_recorded_table_file(tmp_path))})
    by_name = {row.name: row for row in table.hosts}
    assert by_name["h101"].fallback_targets == ("alias-101",)
    assert by_name["h053"].fallback_targets == ("alias-053",)
    assert by_name["h053"].clones[0].repo == "OmniNode-ai/omnimarket"


def test_lab_placement_places_a_unit_on_a_row_with_fallback_targets(
    tmp_path: Path,
) -> None:
    table = load_host_table({"ONEX_LAB_RUN_HOSTS": str(_recorded_table_file(tmp_path))})
    ad = ModelHostCapacityAdvertisement(
        host_name="h101",
        cores=12,
        load1=0.5,
        mem_available_bytes=32 * 1024**3,
        tools=["git", "uv"],
        running_units=0,
        max_units=1,
        rank_penalty=0.0,
        advertised_at=NOW - timedelta(seconds=1),
        cadence_seconds=10,
    )
    result = HandlerLabPoolPlace(environ={}).handle(
        ModelLabPoolPlaceRequest(advertisements=(ad,), evaluated_at=NOW, table=table)
    )
    assert result.decision is EnumPlacementDecision.PLACED
    assert result.host_name == "h101"


def test_fallback_targets_default_is_empty() -> None:
    assert ModelLabHostRow(name="h1", target="host-1").fallback_targets == ()


def test_fallback_targets_accepts_names_in_order() -> None:
    row = ModelLabHostRow(name="h1", target="host-1", fallback_targets=("a", "b"))
    assert row.fallback_targets == ("a", "b")
    table = ModelLabHostsTable.model_validate(
        {"hosts": [{"name": "h1", "target": "host-1", "fallback_targets": ["a", "b"]}]}
    )
    assert table.hosts[0].fallback_targets == ("a", "b")


def test_fallback_targets_refuses_the_target_itself() -> None:
    with pytest.raises(ValidationError, match="repeats target or itself"):
        ModelLabHostRow(name="h1", target="host-1", fallback_targets=("host-1",))


def test_fallback_targets_refuses_a_repeated_name() -> None:
    with pytest.raises(ValidationError, match="repeats target or itself"):
        ModelLabHostRow(name="h1", target="host-1", fallback_targets=("a", "a"))


def test_fallback_targets_refuses_an_empty_name() -> None:
    with pytest.raises(ValidationError):
        ModelLabHostRow(name="h1", target="host-1", fallback_targets=("a", " "))
