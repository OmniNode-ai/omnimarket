# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The state_home setting of the scheduled work-state projection (OMN-22935).

The runtime's OMNI_HOME may be unwritable by the runtime user (the lab's /app: every tick raised a
PermissionError and the last-run time never moved), so the schedule overlay names a state_home that the
file_sources profile uses as the run's home: the projection file and the source snapshots are created under
it and nothing is created under the process OMNI_HOME. A relative state_home is refused by the model, and a
failure with no state_home leaves the last-run time where it was so every tick retries.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.work_state_projection import ModelWorkStateResult
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    work_state_from_payload,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalOwnerReader,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    RULES_EXAMPLE,
    SRC,
    T0,
    schedule,
    tick,
)

pytestmark = pytest.mark.unit

EFFECT = "node_work_state_projection_effect"


def _contract(node: str) -> dict:
    return yaml.safe_load((SRC / node / "contract.yaml").read_text(encoding="utf-8"))


def _unwritable_home(tmp_path: Path) -> Path:
    """A home inside a regular file: no write under it can succeed, for any user including root."""
    blocked = tmp_path / "blocked"
    blocked.write_text("x")
    return blocked / "app"


def _seed_watcher(home: Path) -> None:
    """One source the run can read, so the run writes a snapshot and the contract's snapshots directory exists."""
    watcher = home / _contract(EFFECT)["runtime"]["watcher"]["relpath"]
    watcher.parent.mkdir(parents=True)
    watcher.write_text(json.dumps({"prs": {}}), encoding="utf-8")


def test_golden_state_home_receives_the_projection_when_the_runtime_home_is_unwritable(
    tmp_path: Path,
) -> None:
    env = {
        "OMNI_HOME": str(_unwritable_home(tmp_path)),
        "ONEX_WORK_STATE_OVERLAY": str(RULES_EXAMPLE),
    }
    state_home = tmp_path / "state"
    _seed_watcher(state_home)
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources", state_home=str(state_home)),
        environ=env,
    )
    event = handler.handle(tick(T0))
    assert event is not None
    runtime = _contract(EFFECT)["runtime"]
    projection = state_home / runtime["output"]["relpath"]
    assert projection.is_file()
    result = ModelWorkStateResult.model_validate_json(
        projection.read_text(encoding="utf-8")
    )
    assert isinstance(result, ModelWorkStateResult)
    assert (state_home / runtime["snapshots"]["relpath"]).is_dir()
    assert (tmp_path / "blocked").is_file()


def test_golden_without_state_home_the_registry_home_is_used(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _seed_watcher(home)
    env = {
        "OMNI_HOME": str(home),
        "ONEX_WORK_STATE_OVERLAY": str(RULES_EXAMPLE),
    }
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources"), environ=env
    )
    assert handler.handle(tick(T0)) is not None
    assert (home / _contract(EFFECT)["runtime"]["output"]["relpath"]).is_file()


def test_error_the_runtime_failure_without_state_home(tmp_path: Path) -> None:
    env = {
        "OMNI_HOME": str(_unwritable_home(tmp_path)),
        "ONEX_WORK_STATE_OVERLAY": str(RULES_EXAMPLE),
        "ONEX_REMOTE_LANE_DIR": str(tmp_path / "absent"),
    }
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources"), environ=env
    )
    with pytest.raises(OSError, match="Errno"):
        handler.handle(tick(T0))
    with pytest.raises(OSError, match="Errno"):
        handler.handle(tick(T0 + timedelta(seconds=1)))


def test_error_an_unwritable_state_home_still_raises_and_is_not_swallowed(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("x")
    env = {
        "OMNI_HOME": str(tmp_path / "home"),
        "ONEX_WORK_STATE_OVERLAY": str(RULES_EXAMPLE),
        "ONEX_REMOTE_LANE_DIR": str(tmp_path / "absent"),
    }
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources", state_home=str(blocked / "state")),
        environ=env,
    )
    with pytest.raises(OSError, match="Errno"):
        handler.handle(tick(T0))


def test_error_a_relative_state_home_is_refused() -> None:
    with pytest.raises(ValidationError, match="state_home must be an absolute path"):
        schedule(state_home="state")


def test_golden_state_home_unset_validates() -> None:
    assert schedule().state_home is None


def test_seam_the_fill_and_the_floor_read_what_the_state_home_run_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seam with the lab fill (reads OMNI_HOME-relative) and the floor (validates the payload): a reader whose home is the state home finds the file."""
    now = datetime.now(UTC).replace(microsecond=0)
    state_home = tmp_path / "state"
    _seed_watcher(state_home)
    linear = state_home / _contract(EFFECT)["runtime"]["linear"]["dir_relpath"]
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
    ledger = tmp_path / "ledger.md"
    stamp = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ledger.write_text(
        f"{stamp} | CLAIM | lane=seam-lane | ticket=ZEP-7 | scope text\n",
        encoding="utf-8",
    )
    ledger_env = _contract(EFFECT)["runtime"]["ledger"]["path_env"]
    env = {
        "OMNI_HOME": str(_unwritable_home(tmp_path)),
        "ONEX_WORK_STATE_OVERLAY": str(RULES_EXAMPLE),
        ledger_env: str(ledger),
    }
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources", state_home=str(state_home)),
        environ=env,
    )
    assert handler.handle(tick(now)) is not None
    written = state_home / _contract(EFFECT)["runtime"]["output"]["relpath"]
    projection = work_state_from_payload(
        json.loads(written.read_text(encoding="utf-8"))
    )
    assert isinstance(projection, ModelWorkStateResult)
    monkeypatch.setenv("OMNI_HOME", str(state_home))
    monkeypatch.delenv("ONEX_WORK_STATE_PATH", raising=False)
    owners = LocalOwnerReader().work_state_owners("", 1800.0)
    assert owners == {"ZEP-7": ["seam-lane"]}
