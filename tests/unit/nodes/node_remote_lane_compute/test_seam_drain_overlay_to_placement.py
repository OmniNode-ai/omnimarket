# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain verb's stored mark reaches the remote-lane placement node by the overlay alone (OMN-24489).

The runner places by running ``onex node node_remote_lane_compute``, which builds the handler with no arguments. This
seam test builds it the same way: the drain overlay is found under ONEX_SKILL_OVERLAY_ROOTS, the mark is the verb's own
(set through the verb's runtime entry on the jsonl profile), and the default handler refuses the marked host. It fails
when either side changes shape: the overlay keys the placement reads, the store's record, or the request key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.remote_lane_placement import (
    EnumRemoteLaneOutcome,
    ModelRemoteLaneHostReading,
    ModelRemoteLanePlacementRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import EXAMPLE_OVERLAY
from omnimarket.nodes.node_host_drain_effect.runtime_host_drain import main
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)

HOME_ENV = "EXAMPLE_FLOOR_HOME"


def _reading(name: str, rank_free: float) -> ModelRemoteLaneHostReading:
    return ModelRemoteLaneHostReading.model_validate(
        {
            "name": name,
            "engines": ("claude_sonnet",),
            "lane_slots": 2,
            "lane_cap": 4,
            "rank_free": rank_free,
        }
    )


def _request() -> ModelRemoteLanePlacementRequest:
    return ModelRemoteLanePlacementRequest(
        engine="claude_sonnet",
        readings=(_reading("example-host-a", 5.0), _reading("example-host-b", 1.0)),
    )


@pytest.fixture
def deployed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A drain overlay on the jsonl profile under an overlay root, its lane receipts in a temp directory."""
    raw = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    raw["profile"] = "jsonl"
    raw["lanes"] = {"receipts_dir": str(tmp_path / "lanes"), "timeout_seconds": 5}
    raw["emission"]["home_env"] = HOME_ENV
    root = tmp_path / "overlays" / "node_host_drain_effect"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    (tmp_path / "lanes").mkdir()
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    return tmp_path


def test_a_mark_set_by_the_verb_is_refused_by_the_default_handler_until_it_expires(
    deployed: Path,
) -> None:
    before = HandlerRemoteLanePlacement().handle(_request())
    assert before.host == "example-host-a"  # no mark: the freest host takes the lane

    assert main(["drain", "--host", "example-host-a", "--ttl-seconds", "3600"]) == 0
    marked = HandlerRemoteLanePlacement().handle(_request())
    assert marked.outcome == EnumRemoteLaneOutcome.ADMITTED
    assert marked.host == "example-host-b"
    assert {v.host: v.reason for v in marked.verdicts}["example-host-a"].startswith(
        "draining"
    )

    assert main(["undrain", "--host", "example-host-a"]) == 0
    assert HandlerRemoteLanePlacement().handle(_request()).host == "example-host-a"


def test_an_unreadable_drain_store_refuses_the_placement_never_reads_as_no_host_draining(
    deployed: Path,
) -> None:
    # The jsonl store path is a directory: reading it raises, and the placement is refused.
    store_path = deployed / "home" / ".onex_state" / "host-drain" / "state.jsonl"
    store_path.mkdir(parents=True)
    result = HandlerRemoteLanePlacement().handle(_request())
    assert result.host is None
    assert result.refusal is not None
    assert result.refusal.startswith("drain-state-unreadable")
