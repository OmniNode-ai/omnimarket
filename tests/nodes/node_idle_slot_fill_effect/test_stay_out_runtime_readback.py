# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22017: real overlay files reach the generator's in-process ticket rule."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as handler,
)

pytestmark = pytest.mark.unit


def test_registry_changes_reach_the_real_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issues = [
        {
            "id": f"OMN-{number}",
            "title": title,
            "repo": repo,
            "status": "Todo",
            "labels": ["delegation"],
            "description": "- [ ] AC1: implement the change",
        }
        for number, title, repo in (
            (1, "Build a widget", "repo-one"),
            (2, "Tidy the parser", "repo-two"),
        )
    ]
    for index, blocked_repo in enumerate(("repo-one", "repo-two")):
        root = tmp_path / str(index)
        path = root / "idle_slot_fill" / "overlay.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "always_out_roles": [],
                    "lab_fill_stay_out": [
                        {
                            "name": "runtime-readback",
                            "ruling": "RULING 2026-01-02T03:04:05Z lane=example",
                            "repos": [blocked_repo],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
        items, skipped = handler.generate_backlog(
            issues,
            None,
            m4={},
            out_of_scope=set(),
            claimed=set(),
            open_pr=set(),
            readbacks={issue["id"]: "missing readback" for issue in issues},
            experiments=[
                {
                    "id": "design",
                    "title": "Measure the parser",
                    "repo": blocked_repo,
                    "design": "Measure the result",
                }
            ],
            sent={},
            rule=handler.omnimarket_ticket_rule(),
            today="2026-10-27",
        )
        assert [(item["kind"], item["repo"]) for item in items] == [
            ("readback", "repo-two" if index == 0 else "repo-one")
        ]
        assert skipped == {"stay-out": 2, "out-of-scope": 1}


def test_real_generator_refuses_an_overlay_without_the_stay_out_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "idle_slot_fill" / "overlay.yaml"
    path.parent.mkdir()
    path.write_text("always_out_roles: []\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError, match=r"idle_slot_fill\.lab_fill_stay_out"):
        handler.generate_backlog(
            [],
            None,
            m4={},
            out_of_scope=set(),
            claimed=set(),
            open_pr=set(),
            readbacks={},
            experiments=[],
            sent={},
            rule=handler.omnimarket_ticket_rule(),
            today="2026-10-27",
        )
