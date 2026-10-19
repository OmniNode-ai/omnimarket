# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the compact package read effect."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_compact_package_read_effect.handlers import (
    HandlerCompactPackageRead,
)
from omnimarket.nodes.node_compact_package_read_effect.models import (
    ModelCompactPackageReadRequest,
    ModelCompactPackageReadResult,
)

pytestmark = pytest.mark.unit

BOUNDARY = (
    '{"parentUuid": null, "type": "system", "subtype": "compact_boundary", '
    '"uuid": "783e1d6e-1111-2222-3333-444444444444"}'
)
REPLACEMENT = '{"type": "user", "uuid": "99990000-0000-0000-0000-000000000001"}'
PACKAGE = BOUNDARY + "\n" + REPLACEMENT + "\n"


def test_golden_chain_handle_returns_the_package(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        '{"type": "user", "uuid": "11110000-0000-0000-0000-000000000001"}\n' + PACKAGE,
        encoding="utf-8",
    )
    result = HandlerCompactPackageRead().handle(
        ModelCompactPackageReadRequest(transcript_path=str(path))
    )
    assert isinstance(result, ModelCompactPackageReadResult)
    assert result.found is True
    assert result.package == PACKAGE
    assert result.detail == "2 record(s) from the newest boundary"


def test_error_chain_a_request_without_a_transcript_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelCompactPackageReadRequest.model_validate({})
    assert {err["loc"][0] for err in caught.value.errors()} == {"transcript_path"}


def test_error_chain_a_transcript_with_no_boundary_returns_not_found(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": "user"}\n', encoding="utf-8")
    result = HandlerCompactPackageRead().handle(
        ModelCompactPackageReadRequest(
            transcript_path=str(path), attempts=1, pause_s=0.0
        )
    )
    assert result.found is False
    assert result.package == ""
    assert result.detail == "no compact_boundary record in the transcript"
