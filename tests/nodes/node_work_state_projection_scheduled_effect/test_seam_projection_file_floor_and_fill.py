# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the projection file the scheduled node writes, the floor's reader and the lab fill's owner reader (OMN-22935).

Three pieces share one file and one model: node_work_state_projection_effect writes ``ModelWorkStateResult`` JSON at its
contract's output path, node_floor_simple_effect reads it at its own contract's ``work_state_relpath`` and the lab fill reads
it at its adapter's default path (lane fillorch, OMN-22854, builds the fill orchestrator that calls that reader; its branch
holds no change to either side yet). The tests fail when any of the three paths differs, when the written JSON stops being the
model the readers validate, and when a lane whose CLAIM names a ticket stops showing as that ticket's owner and as a lane the
floor draws.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.work_state_projection import ModelWorkStateResult
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    read_work_state,
    work_state_lanes,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import local_lab_fill_adapters
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalOwnerReader,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    RULES_EXAMPLE,
    SRC,
    schedule,
    tick,
)

pytestmark = pytest.mark.unit

FILL_MAX_AGE_S = 1800.0


def _contract(node: str) -> dict:
    return yaml.safe_load((SRC / node / "contract.yaml").read_text(encoding="utf-8"))


def _home(tmp_path: Path, *, claim_cells: str) -> tuple[Path, dict[str, str], datetime]:
    now = datetime.now(UTC).replace(microsecond=0)
    ledger = tmp_path / "ledger" / "rows.md"
    ledger.parent.mkdir(parents=True)
    stamp = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ledger.write_text(
        f"{stamp} | CLAIM | lane=seam-lane | {claim_cells} | scope text\n",
        encoding="utf-8",
    )
    linear = tmp_path / ".onex_state" / "linear-snapshot"
    linear.mkdir(parents=True)
    (linear / "linear-1.json").write_text(
        json.dumps(
            {
                "operator": "op",
                "issues": [
                    {
                        "id": "ZEP-7",
                        "title": "t",
                        "statusType": "started",
                        "project": "Zephyr",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    watcher = tmp_path / ".claude_scratch" / "pr-watcher"
    watcher.mkdir(parents=True)
    (watcher / "state.json").write_text(json.dumps({"prs": {}}), encoding="utf-8")
    env = {
        "OMNI_HOME": str(tmp_path),
        "ONEX_WORK_STATE_OVERLAY": str(RULES_EXAMPLE),
        _contract("node_work_state_projection_effect")["runtime"]["ledger"][
            "path_env"
        ]: str(ledger),
    }
    return tmp_path, env, now


def _publish(env: dict[str, str], now: datetime) -> None:
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources"), environ=env
    )
    event = handler.handle(tick(now))
    assert event is not None


def test_the_three_pieces_name_one_projection_file() -> None:
    written = _contract("node_work_state_projection_effect")["runtime"]["output"][
        "relpath"
    ]
    floor_reads = _contract("node_floor_simple_effect")["runtime"]["sources"][
        "work_state_relpath"
    ]
    fill_default = Path(local_lab_fill_adapters.__file__).read_text(encoding="utf-8")
    assert written == floor_reads
    assert f'/ "{written}"' in fill_default or f'"{written}"' in fill_default
    scheduled = _contract("node_work_state_projection_scheduled_effect")["runtime"]
    assert scheduled["projection_output"]["contract"] == (
        "node_work_state_projection_effect"
    )
    assert scheduled["projection_output"]["key"] == "runtime.output.relpath"


def test_golden_a_lane_whose_claim_names_a_ticket_shows_after_one_publish_to_both_readers(
    tmp_path, monkeypatch
) -> None:
    home, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    _publish(env, now)
    written = _contract("node_work_state_projection_effect")["runtime"]["output"][
        "relpath"
    ]
    projection = read_work_state(home / written)
    assert isinstance(projection, ModelWorkStateResult)
    lanes = {lane.lane: lane for lane in work_state_lanes(projection)}
    assert lanes["seam-lane"].ticket == "ZEP-7"
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.delenv("ONEX_WORK_STATE_PATH", raising=False)
    owners = LocalOwnerReader().work_state_owners("", FILL_MAX_AGE_S)
    assert owners == {"ZEP-7": ["seam-lane"]}


def test_error_the_same_ledger_without_the_ticket_cell_leaves_the_ticket_unowned(
    tmp_path, monkeypatch
) -> None:
    """The failing control of the golden above: take the cell away and the fill sees no owner."""
    home, env, now = _home(tmp_path, claim_cells="note=none")
    _publish(env, now)
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.delenv("ONEX_WORK_STATE_PATH", raising=False)
    assert LocalOwnerReader().work_state_owners("", FILL_MAX_AGE_S) == {}


def test_error_a_projection_the_readers_cannot_validate_is_refused_not_read_as_empty(
    tmp_path, monkeypatch
) -> None:
    home, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    _publish(env, now)
    written = _contract("node_work_state_projection_effect")["runtime"]["output"][
        "relpath"
    ]
    target = home / written
    document = json.loads(target.read_text(encoding="utf-8"))
    document.pop("counts")
    target.write_text(json.dumps(document), encoding="utf-8")
    assert read_work_state(target) is None
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.delenv("ONEX_WORK_STATE_PATH", raising=False)
    with pytest.raises(local_lab_fill_adapters.LabFillPortError, match="not readable"):
        LocalOwnerReader().work_state_owners("", FILL_MAX_AGE_S)
