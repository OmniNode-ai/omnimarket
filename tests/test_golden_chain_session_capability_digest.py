# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the session-start capability digest."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_session_capability_digest_effect.handlers import (
    HandlerSessionCapabilityDigest,
)
from omnimarket.nodes.node_session_capability_digest_effect.models import (
    EnumSessionCapabilityDigestOutcome,
    ModelSessionCapabilityDigestRequest,
    ModelSessionCapabilityDigestResult,
)

pytestmark = pytest.mark.unit


def test_golden_chain_session_start_to_digest_text(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    (root / "repo").mkdir(parents=True)
    capability = tmp_path / "state" / "capability"
    capability.mkdir(parents=True)
    digest = capability / "digest.txt"
    digest.write_text(
        "CAPABILITIES generated_at=2026-10-17T22:00:00Z\n", encoding="utf-8"
    )
    os.utime(digest, (1_800_000_000.0 - 120, 1_800_000_000.0 - 120))
    result = HandlerSessionCapabilityDigest().handle(
        ModelSessionCapabilityDigestRequest(
            cwd=str(root / "repo"),
            registry_roots=str(root),
            state_dir=str(tmp_path / "state"),
            now_epoch=1_800_000_000.0,
        )
    )
    assert isinstance(result, ModelSessionCapabilityDigestResult)
    assert result.outcome is EnumSessionCapabilityDigestOutcome.DIGEST
    assert result.output == "CAPABILITIES generated_at=2026-10-17T22:00:00Z"
    assert result.age_minutes == 2


def test_error_chain_malformed_request_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelSessionCapabilityDigestRequest.model_validate({"uid": -1, "extra": 1})
    fields = {err["loc"][0] for err in caught.value.errors()}
    assert {"cwd", "uid", "extra"} <= fields


def test_error_chain_unreadable_state_is_named_not_raised(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    result = HandlerSessionCapabilityDigest().handle(
        ModelSessionCapabilityDigestRequest(
            cwd=str(root),
            registry_roots=str(root),
            state_dir=str(tmp_path / "no-such-state"),
            uid=501,
        )
    )
    assert result.outcome is EnumSessionCapabilityDigestOutcome.STALE
    assert result.age_minutes is None
    assert result.output.splitlines()[0] == "CAPABILITY DIGEST STALE age=unknown"
