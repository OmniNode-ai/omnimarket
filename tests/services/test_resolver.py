# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4: the installed tracker resolver must never silently substitute a stub."""

from pathlib import Path

import pytest
from omnibase_infra.adapters.project_tracker.local_stub_project_tracker import (
    LocalStubProjectTracker,
)
from omnibase_infra.enums.enum_project_tracker_backend import EnumProjectTrackerBackend
from omnibase_infra.errors import InfraAuthenticationError
from omnibase_infra.services.project_tracker.resolver import resolve_project_tracker

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("blank", ["", "   "])
def test_fails_loud_when_linear_has_no_key(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", blank)
    monkeypatch.setenv("LINEAR_TOKEN", blank)
    with pytest.raises(InfraAuthenticationError):
        resolve_project_tracker()


def test_fails_loud_contract_selects_stub_only_explicitly(tmp_path: Path) -> None:
    tracker = resolve_project_tracker(
        state_root=tmp_path, backend=EnumProjectTrackerBackend.LOCAL_STUB
    )
    assert isinstance(tracker, LocalStubProjectTracker)
