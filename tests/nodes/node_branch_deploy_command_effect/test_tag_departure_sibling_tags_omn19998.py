# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tag-departure form and its sibling_tags in the signed rebuild command (OMN-19998).

The request's ``form`` picks the command's shape: the default ``pr`` form is today's command, built around a pull
request number, and keeps its meaning byte for byte; the explicit ``tag_departure`` form refuses a pull request
number and instead carries ``sibling_tags``, the sibling repos' dev tags the departure pins. These tests pin the
seam: a tag departure signs once and publishes once with the tags in the envelope and no ``open_pr``; every
malformed shape is refused by the request model before anything is signed; an ordinary pr-form command publishes
byte-identically to the committed signature vector the wire-seam test pins; and the publisher keeps no dedup, so
two sends of one correlation id publish two records. The control at the foot shows the byte-identity seam has
teeth: a handler that writes an empty ``sibling_tags`` into every envelope breaks it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.models.branch_deploy_command import (
    ModelBranchDeployCommandRequest,
    ModelBranchDeployCommandResult,
)
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
    _sign_envelope,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    SIGNING_ENV,
    SIGNING_KEY,
    FakeBus,
    matching_reader,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

WIRE_FILE = Path(__file__).resolve().parent / "agent_wire.json"
GOLDEN_CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000001")
SIBLING_TAGS = {
    "omnibase_core": "dev-20261112000000-0123456789ab",
    "omnibase_infra": "dev-20261113000000-89abcdef0123",
}


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)


def _wire() -> dict[str, Any]:
    return json.loads(WIRE_FILE.read_text())


def _fixture_bytes() -> bytes:
    """The bytes an ordinary pr-form command publishes: the committed signature vector, signed."""
    vector = _wire()["signature_vector"]
    return json.dumps(_sign_envelope(vector["envelope"], vector["secret"])).encode()


def _byte_identical_to_the_fixture(value: bytes) -> bool:
    return value == _fixture_bytes()


def _golden_request() -> ModelBranchDeployCommandRequest:
    """The ordinary pr-form request whose envelope the committed signature vector pins, cell for cell."""
    return request(
        correlation_id=GOLDEN_CORRELATION_ID,
        runtime_lane="dev",
        requested_by="user-a",
        rollback_actor="actor-a",
        rollback_reason="reason-a",
    )


def _tag_departure_request(**cells: Any) -> ModelBranchDeployCommandRequest:
    cells.setdefault("pr_number", None)
    cells.setdefault("sibling_tags", dict(SIBLING_TAGS))
    return request(mode="deploy", form="tag_departure", **cells)


async def _published(
    req: ModelBranchDeployCommandRequest,
) -> tuple[ModelBranchDeployCommandResult, FakeBus]:
    """Handle one request through the real handler; the bus holds what it published."""
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay())
    return await handler.handle(req), bus


# -- golden: the tag-departure form signs once, publishes once, and carries the tags -----------------------------


async def test_golden_tag_departure_publishes_the_sibling_tags() -> None:
    """A tag departure signs once and publishes once: the tags ride the envelope, no open_pr."""
    result, bus = await _published(_tag_departure_request())

    assert result.status == "published"
    assert len(bus.published) == 1
    ((topic, key, value),) = bus.published
    envelope = json.loads(value)
    assert envelope["sibling_tags"] == SIBLING_TAGS
    assert "open_pr" not in envelope
    body = {k: v for k, v in envelope.items() if k != "_signature"}
    signed = _sign_envelope(body, SIGNING_KEY)
    assert signed["_signature"] == envelope["_signature"]
    assert topic.endswith("deploy.rebuild-requested.v1")
    assert key == str(result.correlation_id).encode()


# -- errors: the request model refuses every malformed shape before anything is signed ---------------------------


def test_error_malformed_tag_value_is_refused_naming_the_value() -> None:
    for bad in (
        "main",
        "dev-20261113",
        "dev-20261113000000-ZZ123456789a",
        "dev-20261113000000-89abcdef012",
    ):
        with pytest.raises(ValidationError, match=re.escape(bad)):
            _tag_departure_request(sibling_tags={**SIBLING_TAGS, "omnibase_spi": bad})


def test_error_malformed_repo_key_is_refused_naming_the_key() -> None:
    for bad in ("OmniBase_SPI", "9repo", "repo-with-hyphen"):
        with pytest.raises(ValidationError, match=re.escape(bad)):
            _tag_departure_request(
                sibling_tags={**SIBLING_TAGS, bad: SIBLING_TAGS["omnibase_core"]}
            )


def test_error_tag_departure_with_a_pr_number_is_refused() -> None:
    with pytest.raises(ValidationError, match="refuses a pr_number"):
        _tag_departure_request(pr_number=5917)


def test_error_pr_form_with_sibling_tags_is_refused() -> None:
    with pytest.raises(ValidationError, match="form 'pr' refuses sibling_tags"):
        request(sibling_tags=dict(SIBLING_TAGS))


def test_error_pr_form_without_a_pr_number_is_refused() -> None:
    with pytest.raises(ValidationError, match="form 'pr' needs a pr_number"):
        request(pr_number=None)


def test_error_tag_departure_with_rollback_mode_is_refused() -> None:
    with pytest.raises(ValidationError, match="tag_departure' needs mode 'deploy'"):
        request(
            mode="rollback",
            form="tag_departure",
            pr_number=None,
            sibling_tags=dict(SIBLING_TAGS),
        )


def test_error_tag_departure_with_empty_sibling_tags_is_refused() -> None:
    with pytest.raises(ValidationError, match="non-empty sibling_tags"):
        request(mode="deploy", form="tag_departure", pr_number=None, sibling_tags={})


async def test_error_no_signing_key_refuses_without_records_or_key_read() -> None:
    """The missing-key refusal precedes the agent key check: zero records, no key read."""
    bus = FakeBus()
    reader = matching_reader()
    handler = HandlerBranchDeployCommand(
        event_bus=bus, overlay=overlay(), environ={}, key_check_reader=reader
    )
    result = await handler.handle(_tag_departure_request())

    assert result.status == "refused"
    assert bus.published == []
    assert reader.reads == 0


# -- byte-identity: an ordinary pr-form command publishes the committed fixture bytes ----------------------------


async def test_pr_form_command_publishes_the_committed_fixture_bytes() -> None:
    """An ordinary pr-form command publishes exactly the bytes the signature vector pins."""
    _result, bus = await _published(_golden_request())

    ((_topic, key, value),) = bus.published
    vector = _wire()["signature_vector"]
    assert _byte_identical_to_the_fixture(value)
    assert json.loads(value)["_signature"] == vector["signature"]
    assert "sibling_tags" not in json.loads(value)
    assert key == str(GOLDEN_CORRELATION_ID).encode()


# -- no dedup: two sends of one correlation id publish two records -----------------------------------------------


async def test_two_sends_with_one_correlation_id_publish_two_records() -> None:
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay())
    first = _tag_departure_request()
    second = _tag_departure_request()
    assert first.correlation_id == second.correlation_id

    assert (await handler.handle(first)).status == "published"
    assert (await handler.handle(second)).status == "published"

    assert len(bus.published) == 2
    ids = [json.loads(value)["correlation_id"] for _, _, value in bus.published]
    assert ids == [str(first.correlation_id)] * 2
    keys = [key for _, key, _ in bus.published]
    assert keys == [str(first.correlation_id).encode()] * 2


# -- failing control: a handler that always writes sibling_tags breaks the byte identity -------------------------


async def test_failing_control_always_sibling_tags_fails_byte_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler that writes an empty sibling_tags into every envelope no longer matches the fixture."""
    original = HandlerBranchDeployCommand._build_envelope

    def _always(
        build_self: HandlerBranchDeployCommand,
        build_request: ModelBranchDeployCommandRequest,
    ) -> dict[str, Any]:
        envelope = original(build_self, build_request)
        envelope["sibling_tags"] = {}
        return envelope

    monkeypatch.setattr(HandlerBranchDeployCommand, "_build_envelope", _always)
    _result, bus = await _published(_golden_request())

    assert not _byte_identical_to_the_fixture(bus.published[0][2])
