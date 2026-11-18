# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the dev signed-in read node (OMN-24345).

One run against the fakes signs the user in and reads once: the call order is the password grant, then the read with the
user's bearer and the overlay's correlation id and limit. The password moves to the Keycloak form and to nothing else, the
token moves to the read's bearer and to nothing else -- not the result's dump, repr or str, nor the logs. A probe answers
ready and calls nothing. The data state of the answer is carried through whichever of the three it is.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from omnimarket.nodes.node_dev_signed_in_read_effect.handlers import (
    ModelDevSignedInReadCommand,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.models import (
    EnumDevSignedInReadOutcome,
)

from ._support import PASSWORD, PASSWORD_LOCATION, USER_TOKEN, build, seeded_store
from .fakes import FakeKeycloakAndApi

pytestmark = pytest.mark.unit


def test_run_signs_in_then_reads_once(caplog: pytest.LogCaptureFixture) -> None:
    transport = FakeKeycloakAndApi()
    store = seeded_store()
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(
            build(transport, store).handle(
                ModelDevSignedInReadCommand(lane="dev", write=True)
            )
        )

    assert result.outcome is EnumDevSignedInReadOutcome.READ_ACCEPTED
    assert result.ok is True
    assert result.http_status == 200
    assert result.data_state == "not_found"
    assert result.user_name == "dev-probe-user"
    assert [r.method for r in transport.sent] == ["POST", "GET"]
    assert store.calls == [PASSWORD_LOCATION]

    grant = transport.grant_forms[0]
    assert grant["grant_type"] == "password"
    assert grant["client_id"] == "omnidash-spa"
    assert grant["username"] == "dev-probe-user"
    assert grant["password"] == PASSWORD

    read = transport.sent[1]
    assert read.bearer is not None
    assert read.bearer.get_secret_value() == USER_TOKEN
    assert read.query == {"correlation_id": "dev-signed-in-read-probe", "limit": "1"}

    leaked = [result.model_dump_json(), repr(result), str(result), caplog.text]
    for text in leaked:
        assert PASSWORD not in text
        assert USER_TOKEN not in text


@pytest.mark.parametrize("state", ["found", "not_found", "projection_absent"])
def test_every_declared_data_state_is_carried(state: str) -> None:
    transport = FakeKeycloakAndApi(data_state=state)
    result = asyncio.run(
        build(transport, seeded_store()).handle(
            ModelDevSignedInReadCommand(lane="dev", write=True)
        )
    )
    assert result.outcome is EnumDevSignedInReadOutcome.READ_ACCEPTED
    assert result.data_state == state


def test_probe_answers_ready_and_calls_nothing() -> None:
    transport = FakeKeycloakAndApi()
    result = asyncio.run(
        build(transport, seeded_store()).handle(
            ModelDevSignedInReadCommand(lane="dev", write=False)
        )
    )
    assert result.outcome is EnumDevSignedInReadOutcome.READY
    assert result.ok is True
    assert transport.sent == []
