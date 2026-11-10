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
    # Shape tests must not inherit the launching host's live WIP or destination policy.
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    module = ModuleType("fixture_brief_readers")
    module.RULES_BLOCK_HEAD = "# Standing rules"
    module.H_CLAIM = "claim"
    module.unfenced_prose = lambda text: "\n".join(
        line for line in text.splitlines() if not line.startswith(">")
    )
    module.premise_text = lambda text: text.split(module.RULES_BLOCK_HEAD)[0]
    module.H_3A = "## 3a. Constraints"
    module.without_ticket_text = lambda text: text
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
        ("OMN-17427", "repo-a", "Umbrella evidence: headless_lane_cannot_mint", False),
        ("OMN-17427", "repo-a", "Umbrella evidence: <reason>", True),
        ("OMN-17427", "repo-a", "> Umbrella evidence: headless_lane_cannot_mint", True),
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


@pytest.mark.parametrize(
    ("repo", "refusal", "want"),
    [
        ("repo-a", "REMOTE-LANE-WIP-REFUSED lane=lane-a REFUSE", ("wip-refused",)),
        ("repo-a", None, ()),
        (None, "REMOTE-LANE-WIP-REFUSED lane=lane-a REFUSE", ()),
    ],
)
def test_a_lane_that_may_open_a_pr_waits_when_the_wip_admission_refuses(
    readers, monkeypatch, repo, refusal, want
):
    asked = []
    monkeypatch.setattr(
        handler,
        "dispatch_wip_refusal",
        lambda **facts: asked.append(facts) or refusal,
    )
    result = NodeDispatchBriefShapeCompute().handle(
        ModelDispatchBriefShapeRequest(
            "Fix it.", "lane-a", ("repo-a#4",), "unused.py", "OMN-5", repo
        )
    )
    assert tuple(status for status, _ in result.refusals) == want
    assert bool(asked) is bool(repo)
    if asked:
        assert asked[0]["prs"] == ("repo-a#4",)
        assert asked[0]["repo"] == "repo-a"


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ("src/pkg/handlers/new.py", True),
        ("tests/test_new.py", True),
        ("config/onex/overlays/demo/overlay.yaml", False),
        ("config/migration/destinations.json", False),
    ],
)
def test_recorded_frozen_destinations_are_refused_before_placement(
    readers, monkeypatch, tmp_path, path, refused
):
    """Replay the recorded source/test refusal gap with two data-only controls."""
    policy = tmp_path / "brief_destinations" / "overlay.yaml"
    policy.parent.mkdir()
    policy.write_text(
        "frozen_repositories: [frozen-repo]\nmigration_repository: omnimarket\n"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    result = NodeDispatchBriefShapeCompute().handle(
        ModelDispatchBriefShapeRequest(
            f"Create the declared file.\ncreates: frozen-repo@main:{path}\n",
            "lane-a",
            (),
            "unused.py",
        )
    )
    assert bool(result.refusals) is refused
    if refused:
        status, reason = result.refusals[0]
        assert status == "frozen-destination-refused"
        assert path in reason
        assert "move" in reason.lower()
        assert "omnimarket" in reason


@pytest.fixture
def destination_policy(monkeypatch, tmp_path):
    policy = tmp_path / "brief_destinations" / "overlay.yaml"
    policy.parent.mkdir()
    policy.write_text(
        "frozen_repositories: [frozen-repo, archived-repo]\n"
        "migration_repository: omnimarket\n"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    return policy


@pytest.mark.parametrize(
    ("task", "want"),
    [
        ("edits: frozen-repo@main:src/pkg/handlers/existing.py", True),
        ("edits: archived-repo@main:tests/test_existing.py", True),
        ("creates: frozen-repo@main:tests/", True),
        ("creates: frozen-repo@main:`tests/test_new.py`", True),
        ("creates: frozen-repo@main:config/demo.py.yaml", False),
        ("Move omnimarket/src/pkg/old.py to frozen-repo/src/pkg/new.py.", True),
        ("creates: frozen-repo@main:scripts/run.sh", True),
        ("creates: omnimarket@dev:src/omnimarket/handlers/new.py", False),
        ("edits: fix-only-repo@main:tests/test_existing.py", False),
        ("evidence: frozen-repo@main:tests/test_existing.py", False),
        ("deletes: frozen-repo@main:src/pkg/handlers/old.py", False),
        ("removes: frozen-repo@main:tests/test_old.py", False),
        ("Edit frozen-repo/src/pkg/handlers/existing.py.", True),
        ("Update archived-repo/tests/test_existing.py.", True),
        ("Edit frozen-repo@main:tests/test_existing.py.", True),
        ("Read frozen-repo/src/pkg/handlers/existing.py.", False),
        ("Do not edit frozen-repo/src/pkg/handlers/existing.py.", False),
        (
            "Move frozen-repo/src/pkg/handlers/old.py to omnimarket/src/omnimarket/handlers/new.py and edit it there.",
            False,
        ),
    ],
)
def test_frozen_policy_distinguishes_writes_reads_removals_and_migration(
    readers, destination_policy, task, want
):
    result = NodeDispatchBriefShapeCompute().handle(
        ModelDispatchBriefShapeRequest(task, "lane-a", (), "unused.py")
    )
    assert bool(result.refusals) is want


@pytest.mark.parametrize(
    "path", ["src/pkg/new.py", "src/tests/new.py", "tests/src/new.py"]
)
def test_relative_task_paths_use_the_dispatch_repository(
    readers, destination_policy, monkeypatch, path
):
    monkeypatch.setattr(handler, "dispatch_wip_refusal", lambda **_facts: None)
    result = NodeDispatchBriefShapeCompute().handle(
        ModelDispatchBriefShapeRequest(
            f"Edit {path}.", "lane-a", (), "unused.py", repo="frozen-repo"
        )
    )
    assert result.refusals[0][0] == "frozen-destination-refused"


def test_destination_gate_uses_the_callers_authored_prose_reader(readers):
    readers.premise_text = lambda _brief: "Report the open PRs."
    result = NodeDispatchBriefShapeCompute().handle(
        ModelDispatchBriefShapeRequest(
            "creates: frozen-repo@main:tests/test_example.py", "lane-a", (), "unused.py"
        )
    )
    assert result.refusals == ()


@pytest.mark.parametrize(
    "overlay",
    [
        None,
        "{}",
        "frozen_repositories: []\nmigration_repository: omnimarket",
        "frozen_repositories: ['']\nmigration_repository: omnimarket",
    ],
)
def test_unread_destination_policy_refuses_instead_of_admitting(
    readers, monkeypatch, tmp_path, overlay
):
    if overlay is not None:
        path = tmp_path / "brief_destinations" / "overlay.yaml"
        path.parent.mkdir()
        path.write_text(overlay)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    result = NodeDispatchBriefShapeCompute().handle(
        ModelDispatchBriefShapeRequest(
            "creates: frozen-repo@main:tests/test_new.py", "lane-a", (), "unused.py"
        )
    )
    assert result.refusals[0][0] == "destination-policy-unavailable"
    assert "brief_destinations" in result.refusals[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "refused"), [("tests/test_new.py", True), ("config/demo.yaml", False)]
)
async def test_destination_operation_runs_through_the_canonical_runtime_bus(
    readers, destination_policy, tmp_path, path, refused
):
    from uuid import uuid4

    from omnimarket.adapters.codex.local_runtime_dispatch import LocalRuntimeDispatch
    from omnimarket.adapters.codex.runtime_client import (
        ModelDispatchBusCommand,
        default_command_topic,
        default_response_topic,
    )
    from omnimarket.nodes.node_dispatch_brief_shape_compute.models.model_dispatch_brief_shape import (
        ModelBriefDestinationRequest,
        ModelBriefDestinationResult,
    )

    runtime = LocalRuntimeDispatch(
        adapter_command_topic=default_command_topic(), state_root=tmp_path / "runtime"
    )
    terminal, evidence = await runtime.dispatch(
        ModelDispatchBusCommand(
            command_name="node_dispatch_brief_shape_compute.check_destinations",
            requester="brief-destination-test",
            correlation_id=uuid4(),
            payload=ModelBriefDestinationRequest(
                authored_prose=f"creates: frozen-repo@main:{path}"
            ).model_dump(mode="json"),
            response_topic=default_response_topic(),
            timeout_seconds=30,
        )
    )
    assert terminal.status == "completed", terminal.error_message
    assert evidence.runtime_observation.status == "OBSERVED"
    assert evidence.adapter_dispatch_binding.node_contract.status == "RESOLVED"
    result = ModelBriefDestinationResult.model_validate(terminal.payload)
    assert bool(result.refusals) is refused
    if refused:
        assert result.refusals[0][0] == "frozen-destination-refused"


@pytest.mark.parametrize(
    "path", ["../tests/test_new.py", "/tests/test_new.py", "src/../tests/new.py"]
)
def test_malformed_declared_destinations_are_not_admitted(
    readers, destination_policy, path
):
    result = NodeDispatchBriefShapeCompute().handle(
        ModelDispatchBriefShapeRequest(
            f"creates: frozen-repo@main:{path}", "lane-a", (), "unused.py"
        )
    )
    assert result.refusals[0][0] == "destination-unreadable"


@pytest.mark.parametrize(
    ("path", "refused"), [("tests/test_new.py", True), ("config/map.json", False)]
)
def test_node_exposes_the_declared_destination_operation(
    destination_policy, path, refused
):
    from omnimarket.nodes.node_dispatch_brief_shape_compute.models.model_dispatch_brief_shape import (
        ModelBriefDestinationRequest,
    )

    result = NodeDispatchBriefShapeCompute().check_destinations(
        ModelBriefDestinationRequest(authored_prose=f"creates: frozen-repo@main:{path}")
    )
    assert bool(result.refusals) is refused
    if refused:
        assert result.refusals[0][0] == "frozen-destination-refused"
