# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy agent's lineage-fence refusals parse at the reader (OMN-19270).

The agent refuses a command whose ref is a strict ancestor of the running build
(``superseded_by_running_build``) or has diverged from it off the tracking
branch (``divergent_ref``). A token this reader cannot parse drops the terminal
event, and the publish monitor then waits out its full timeout instead of
ending the wait on the refusal.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.events.runtime_deployment import (
    EnumDeployRejectionReason,
    ModelDeployRebuildRejected,
)


@pytest.mark.unit
@pytest.mark.parametrize("token", ["superseded_by_running_build", "divergent_ref"])
def test_a_lineage_refusal_parses_as_a_three_key_rejection(token: str) -> None:
    rejected = ModelDeployRebuildRejected(
        correlation_id=str(uuid4()), reason=token, scope="full"
    )

    assert rejected.reason is EnumDeployRejectionReason(token)
    assert rejected.reason is not EnumDeployRejectionReason.SUPERSEDED
    assert rejected.superseded_by_sha is None
    assert rejected.superseded_by_correlation_id is None


@pytest.mark.unit
def test_a_lineage_refusal_cannot_name_a_replacement_command() -> None:
    """Only ``superseded`` names a replacement; the running build is not a command."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelDeployRebuildRejected(
            correlation_id=str(uuid4()),
            reason="superseded_by_running_build",
            scope="full",
            superseded_by_sha="0" * 40,
            superseded_by_correlation_id=str(uuid4()),
        )
