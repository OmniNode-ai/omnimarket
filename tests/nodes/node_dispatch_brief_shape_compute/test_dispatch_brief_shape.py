# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch refusals use the caller's brief readers before any placement."""

from types import ModuleType

import pytest

from omnimarket.nodes.node_dispatch_brief_shape_compute import (
    NodeDispatchBriefShapeCompute,
)
from omnimarket.nodes.node_dispatch_brief_shape_compute.handlers import (
    handler_dispatch_brief_shape as handler,
)
from omnimarket.nodes.node_dispatch_brief_shape_compute.models.model_dispatch_brief_shape import (
    ModelDispatchBriefShapeRequest,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def readers(monkeypatch):
    module = ModuleType("fixture_brief_readers")
    module.RULES_BLOCK_HEAD = "# Standing rules"
    module.H_CLAIM = "claim"
    module.unfenced_prose = lambda text: "\n".join(
        line for line in text.splitlines() if not line.startswith(">")
    )
    module.premise_text = lambda text: text.split(module.RULES_BLOCK_HEAD)[0]
    module.split_parts = lambda _text: ({}, ())
    module.has_contract_step = lambda _text, _ticket: False
    module.lab_mint_problems = lambda text: (
        ["mint forbidden"] if text.startswith("Mint ") else []
    )
    monkeypatch.setattr(handler, "load_lane_brief", lambda _path: module)
    return module


@pytest.mark.parametrize(
    ("brief", "lane", "prs", "want"),
    [
        ("Report the open PRs.", "lane-a", (), ()),
        (
            "# Lane a\n# Standing rules\nAuthority: verify",
            "lane-a",
            (),
            ("brief-refused",),
        ),
        ("Fix the red.\n# Standing rules\nAuthority: verify", "lane-a", (), ()),
        ("Mint a ticket for follow-up.", "lane-a", (), ("mint-refused",)),
        ("Carry it.", "pf-drive-repo-a-9", (), ("driver-no-pr",)),
        ("Carry it.", "pf-drive-repo-a-9", ("repo-a#9",), ()),
    ],
)
def test_the_node_reads_the_brief_with_the_callers_readers(
    readers, brief, lane, prs, want
):
    result = NodeDispatchBriefShapeCompute().handle(
        ModelDispatchBriefShapeRequest(brief, lane, prs, "unused.py")
    )
    assert tuple(status for status, _ in result.refusals) == want


@pytest.mark.parametrize(
    ("ticket", "repo", "extra", "want"),
    [
        ("OMN-17427", "repo-a", "", True),
        ("OMN-17427", None, "", False),
        ("OMN-7", "repo-a", "", False),
        ("OMN-17427", "repo-a", "Umbrella evidence: lane_intake_budget", False),
        ("OMN-17427", "repo-a", "Umbrella evidence: <reason>", True),
        ("OMN-17427", "repo-a", "> Umbrella evidence: lane_intake_budget", True),
    ],
)
def test_umbrella_ticket_needs_a_child_or_real_evidence(
    readers, ticket, repo, extra, want
):
    request = ModelDispatchBriefShapeRequest(
        "Fix the red.\n" + extra, "lane-a", (), "unused.py", ticket, repo
    )
    result = NodeDispatchBriefShapeCompute().handle(request)
    assert bool(result.refusals) is want


@pytest.mark.parametrize(
    ("lane", "target"),
    [
        ("pf-drive-repo-a-909-1450", "--pr repo-a#909"),
        ("pf-drive-omnimarket-4070", "--pr omnimarket#4070"),
        ("pf-drive-something", "--pr <repo>#<n>"),
    ],
)
def test_driver_refusal_names_the_missing_target(lane, target):
    assert target in handler.driver_problem(lane, ())


def test_reader_module_loads_from_the_explicit_path(tmp_path):
    path = tmp_path / "readers.py"
    path.write_text("RULES_BLOCK_HEAD = 'fixture-head'\n")
    module = handler.load_lane_brief.__wrapped__(str(path))
    assert module.RULES_BLOCK_HEAD == "fixture-head"
