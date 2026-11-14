# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain of node_entry_point_registration_compute: what the reviewed list refuses (OMN-24354)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_entry_point_registration_compute import (
    HandlerEntryPointRegistration,
)
from omnimarket.nodes.node_entry_point_registration_compute.models import (
    ModelEntryPointRegistrationConfig,
    ModelEntryPointRegistrationRequest,
)


def _config(*entries: dict[str, str]) -> ModelEntryPointRegistrationConfig:
    return ModelEntryPointRegistrationConfig.model_validate(
        {"reviewed_unregistered_nodes": list(entries)}
    )


def test_unlisted_unregistered_node_is_refused() -> None:
    verdict = HandlerEntryPointRegistration(_config()).handle(
        ModelEntryPointRegistrationRequest(
            node_name="node_c", entry_points=frozenset({"node_a"})
        )
    )
    assert (verdict.registered, verdict.reviewed_reason, verdict.passes) == (
        False,
        None,
        False,
    )


def test_named_break_the_lifecycle_alone_never_excuses_a_node() -> None:
    """The request carries no lifecycle: an experimental or deprecated contract
    cannot reach the verdict, so only the reviewed list can excuse a node."""
    fields = set(ModelEntryPointRegistrationRequest.model_fields)
    assert fields == {"node_name", "entry_points"}
    with pytest.raises(ValidationError):
        ModelEntryPointRegistrationRequest.model_validate(
            {"node_name": "node_c", "entry_points": [], "lifecycle": "experimental"}
        )


@pytest.mark.parametrize(
    "entries",
    [
        ({"node": "node_a", "reason": " "},),
        ({"node": "", "reason": "why"},),
        ({"node": "node_a", "reason": "why"}, {"node": "node_a", "reason": "again"}),
    ],
)
def test_malformed_reviewed_list_is_refused(
    entries: tuple[dict[str, str], ...],
) -> None:
    with pytest.raises(ValidationError):
        _config(*entries)


def test_stale_entries_name_a_gone_directory_and_a_newly_registered_node() -> None:
    handler = HandlerEntryPointRegistration(
        _config(
            {"node": "node_gone", "reason": "why"},
            {"node": "node_now_registered", "reason": "why"},
            {"node": "node_still_unregistered", "reason": "why"},
        )
    )
    assert handler.stale_entries(
        node_dirs=frozenset({"node_now_registered", "node_still_unregistered"}),
        entry_points=frozenset({"node_now_registered"}),
    ) == ("node_gone", "node_now_registered")
