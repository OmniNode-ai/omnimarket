# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effects runtime batches OCC companions by default (OMN-16336).

omnimarket#2940 shipped the per-ticket batch path with every default reading
``off``. The merge-sweep receipt repair builds its fix command without naming a
grouping, and the deploy-gate route passed none, so both kept minting one
companion per product PR whatever the publisher asked. These tests fail the
moment a runtime seam falls back to the per-PR path by default again. The
default is the repository batch window since the cross-ticket window build
(operator ruling 2026-09-27T10:32:50Z); ticket grouping stays scoped to
BATCH_READY_REPOS.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from unittest.mock import patch
from uuid import uuid4

import pytest

from omnimarket.events.occ_companion import (
    BATCH_READY_REPOS,
    EnumOccBatchMode,
    batch_ready,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
    handler_pr_lifecycle_fix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)


@pytest.mark.unit
def test_command_without_a_grouping_is_window_batched() -> None:
    """The merge-sweep receipt repair builds the command without the field."""
    command = ModelPrLifecycleFixCommand.model_validate(
        {
            "correlation_id": str(uuid4()),
            "pr_number": 42,
            "repo": "OmniNode-ai/omnimarket",
            "block_reason": EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
            "requested_at": "2026-09-27T10:00:00+00:00",
        }
    )
    assert command.occ_batch_mode is EnumOccBatchMode.WINDOW


@pytest.mark.unit
def test_emitter_and_adapter_seams_default_to_window() -> None:
    seams: list[Callable[..., object]] = [
        OccCompanionEmitter.autobind_evidence_source,
        OccCompanionEmitter.create_occ_contract,
        handler_pr_lifecycle_fix.ProtocolOccAutobindAdapter.autobind_evidence_source,
        handler_pr_lifecycle_fix._NoopOccAutobindAdapter.autobind_evidence_source,
    ]
    for seam in seams:
        default = inspect.signature(seam).parameters["batch_mode"].default
        assert default is EnumOccBatchMode.WINDOW, seam.__qualname__


@pytest.mark.unit
def test_the_private_core_has_no_grouping_default() -> None:
    """Every caller of the core names its grouping; none inherits one."""
    parameter = inspect.signature(OccCompanionEmitter._emit_companion_sync).parameters[
        "batch_mode"
    ]
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.unit
def test_batch_ready_repos_is_omnimarket_only_until_its_blockers_close() -> None:
    """Widening this set is a reviewed code change, never a configuration flip."""
    assert frozenset({"omninode-ai/omnimarket"}) == BATCH_READY_REPOS
    assert batch_ready("OmniNode-ai/omnimarket")
    assert not batch_ready("OmniNode-ai/omnibase_infra")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("repo", "ticket_lease_expected"),
    [("OmniNode-ai/omnibase_infra", False), ("OmniNode-ai/omnimarket", True)],
)
def test_ticket_grouping_batches_only_a_batch_ready_repo(
    repo: str, ticket_lease_expected: bool
) -> None:
    """The merge-sweep repair sends ticket for every repo; the runtime scopes it."""
    module = OccCompanionEmitter.__module__
    emitter = OccCompanionEmitter()
    product = {
        "number": 42,
        "body": "no evidence yet",
        "title": "fix(OMN-16336): something",
        "head": {"sha": "b" * 40, "ref": "jonah/omn-16336-x"},
        "base": {"repo": {"private": False}},
        "state": "open",
        "merged": False,
        "draft": False,
        "labels": [],
    }
    with (
        patch(f"{module}._resolve_github_token", return_value="token"),
        patch(f"{module}.rest_json", return_value=product),
        patch(f"{module}.acquire_occ_ticket_lease", return_value=False) as ticket,
        patch(f"{module}.acquire_occ_companion_lease", return_value=False) as head,
        patch.object(emitter, "_find_contending_companions", return_value=[]),
        patch.object(emitter, "_observe_pr_probe", return_value=('{"files":[]}', 0)),
        patch.object(
            emitter,
            "_derive_content_bound_check",
            return_value=(
                "gh api repos/x/y/contents/z?ref=" + "1" * 40,
                "a" * 40,
                1,
                (),
            ),
        ),
    ):
        result = emitter._emit_companion_sync(
            repo, 42, "OMN-16336", batch_mode=EnumOccBatchMode.TICKET
        )
    if ticket_lease_expected:
        assert result.startswith("skip:TICKET_LEASE_HELD"), result
        ticket.assert_called_once()
        head.assert_not_called()
    else:
        assert result.startswith("skip:LEASE_HELD"), result
        ticket.assert_not_called()
        head.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize(
    "repo", ["OmniNode-ai/omnibase_infra", "OmniNode-ai/omnimarket"]
)
def test_window_grouping_batches_every_repo(repo: str) -> None:
    """The window is keyed on the repository, so it needs no batch-ready scope."""
    module = OccCompanionEmitter.__module__
    emitter = OccCompanionEmitter()
    product = {
        "number": 42,
        "body": "no evidence yet",
        "title": "fix(OMN-16336): something",
        "head": {"sha": "b" * 40, "ref": "jonah/omn-16336-x"},
        "base": {"repo": {"private": False}},
        "state": "open",
        "merged": False,
        "draft": False,
        "labels": [],
    }
    with (
        patch(f"{module}._resolve_github_token", return_value="token"),
        patch(f"{module}.rest_json", return_value=product),
        patch(f"{module}.acquire_occ_window_lease", return_value=False) as window,
        patch(f"{module}.acquire_occ_ticket_lease", return_value=False) as ticket,
        patch(f"{module}.acquire_occ_companion_lease", return_value=False) as head,
        patch.object(emitter, "_find_contending_companions", return_value=[]),
        patch.object(emitter, "_observe_pr_probe", return_value=('{"files":[]}', 0)),
        patch.object(
            emitter,
            "_derive_content_bound_check",
            return_value=(
                "gh api repos/x/y/contents/z?ref=" + "1" * 40,
                "a" * 40,
                1,
                (),
            ),
        ),
    ):
        result = emitter._emit_companion_sync(
            repo, 42, "OMN-16336", batch_mode=EnumOccBatchMode.WINDOW
        )
    assert result.startswith("skip:WINDOW_LEASE_HELD"), result
    window.assert_called_once()
    assert window.call_args.kwargs["repo"] == repo
    ticket.assert_not_called()
    head.assert_not_called()
