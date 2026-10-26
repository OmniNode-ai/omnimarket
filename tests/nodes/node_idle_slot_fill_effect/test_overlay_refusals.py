# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""With no overlay supplying a key, each idle_slot_fill key refuses naming itself (OMN-21409).

The private repository names, the cloud backlog declaration and the experiment designs have no packaged
fallback: the node reads them from the ``idle_slot_fill`` and ``repos`` overlays only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_idle_slot_fill_effect import cloud_backlog, overlay
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as handler,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models import (
    ModelBacklogSources,
    ModelIdleSlotFillRequest,
)
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import (
    IDLE_SLOT_FILL,
    REPOS,
    write_overlay,
)

pytestmark = pytest.mark.unit

NODE = Path(handler.__file__).resolve().parent.parent
LINES = ["MISSING lab-headroom:host-a | free Claude slots 3 | FIX: dispatch"]
HOST = {
    "name": "host-a",
    "local": False,
    "admission": {"claude_slots": 2, "codex_slots": 0, "probe_error": None},
}


def empty(tmp_path: Path) -> dict[str, str]:
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "empty")}


@pytest.mark.parametrize(
    ("read", "key"),
    [
        (overlay.mvp_repos, "idle_slot_fill.mvp_repos"),
        (overlay.process_fix_repos, "idle_slot_fill.process_fix_roles"),
        (overlay.always_out, "idle_slot_fill.always_out_roles"),
        (overlay.cloud_backlog, "idle_slot_fill.cloud_backlog"),
        (overlay.experiment_designs, "idle_slot_fill.experiment_designs"),
        (cloud_backlog.load_declaration, "idle_slot_fill.cloud_backlog"),
    ],
)
def test_an_empty_overlay_root_refuses_naming_the_key(
    tmp_path: Path, read, key: str
) -> None:
    with pytest.raises(NodeOverlayError, match=key.replace(".", r"\.")):
        read(empty(tmp_path))


@pytest.mark.parametrize(
    "key", sorted(k for k in IDLE_SLOT_FILL if k != "cloud_backlog")
)
def test_a_present_overlay_that_omits_a_key_refuses_naming_it(
    tmp_path: Path, key: str
) -> None:
    document = {k: v for k, v in IDLE_SLOT_FILL.items() if k != key}
    env = write_overlay(tmp_path, idle_slot_fill=document)
    reads = {
        "mvp_repos": overlay.mvp_repos,
        "process_fix_roles": overlay.process_fix_repos,
        "always_out_roles": overlay.always_out,
        "experiment_designs": overlay.experiment_designs,
    }
    with pytest.raises(NodeOverlayError, match=rf"idle_slot_fill\.{key}"):
        reads[key](env)


@pytest.mark.parametrize("role", ["private_data", "plugin_source", "deploy"])
def test_a_role_the_repos_overlay_omits_refuses_naming_the_role(
    tmp_path: Path, role: str
) -> None:
    env = write_overlay(tmp_path, repos={k: v for k, v in REPOS.items() if k != role})
    roles = {
        "private_data": overlay.process_fix_repos,
        "plugin_source": overlay.process_fix_repos,
        "deploy": overlay.always_out,
    }
    with pytest.raises(NodeOverlayError, match=rf"repos\.{role}"):
        roles[role](env)


def test_the_roles_resolve_through_the_repos_overlay() -> None:
    assert overlay.process_fix_repos() == {"repo-private-data", "repo-plugin-source"}
    assert overlay.always_out() == {"repo-deploy"}
    assert "omnimarket" in overlay.mvp_repos()


def test_the_handler_refuses_naming_the_key_when_no_overlay_supplies_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    request = ModelIdleSlotFillRequest(
        now="2026-10-18T21:30:00Z",
        hosts=[HOST],
        lines=[
            *LINES,
            "MISSING repo-status:omnimarket | shared_red_unowned: check pytest red on 4 PRs | FIX: dispatch",
        ],
        standing_tickets={"cause-red": "OMN-9101"},
    )
    with pytest.raises(NodeOverlayError, match=r"idle_slot_fill\.process_fix_roles"):
        handler.HandlerIdleSlotFill().handle(request)


def test_unread_experiment_designs_are_reported_not_swallowed(
    tmp_path: Path,
) -> None:
    env = empty(tmp_path)
    sources = handler.default_sources("2026-10-19T16:00:00Z", env)
    assert isinstance(sources, ModelBacklogSources)
    assert sources.experiments is not None
    _, note = handler.read_generated(sources, None, {}, "2026-10-19T16:00:00Z")
    assert "experiments unread" in note
    assert "idle_slot_fill.experiment_designs" in note
    with pytest.raises(NodeOverlayError) as caught:
        overlay.experiment_designs(env)
    assert len(str(caught.value)) > 160
    assert " ".join(str(caught.value).splitlines()) in note


def test_the_node_ships_no_private_json_and_no_internal_topic() -> None:
    shipped = sorted(p.name for p in NODE.rglob("*.json"))
    assert shipped == []
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    topics = [
        *(row["topic"] for row in contract["handler_routing"]["handlers"]),
        contract["terminal_event"],
        contract["runtime_dispatch"]["command_topic"],
    ]
    assert topics
    assert all(
        t.startswith(("onex.cmd.omnimarket.", "onex.evt.omnimarket.")) for t in topics
    )
    assert "omniclaude" not in (NODE / "contract.yaml").read_text(encoding="utf-8")
