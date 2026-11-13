# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_runner_group_exposure_effect: a refused organization read names the permission it lacked
(OMN-23580).

The permission's NAME comes from the overlay, never from code. Golden chain: a clean read with a
minted token names none. Error chain: a 403 group read names the overlay's permission in the result
and in the written row's reasons; a missing stored token names none and makes no call. The shape
follows test_token_mint.py (same local HTTP fixture, minter fake and overlay installer)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.reading_text import reason_token
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.models.runner_group_exposure import EnumExposureReadFailure
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_probe import (
    HandlerExposureProbe,
    load_topics,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_reading_writer import (
    HandlerExposureReadingWriter,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_probe import (
    ModelExposureProbeRequest,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    NODE_NAME,
    ModelExposureReadingOverlay,
)
from tests.node_overlay_support import install_node_overlay
from tests.nodes.node_runner_group_exposure_effect.test_exposure_probe import (
    NOW,
    SETTINGS,
    TOKEN_ENV,
    TOKEN_VALUE,
    Scenario,
    _Handler,
    _Server,
)
from tests.nodes.node_runner_group_exposure_effect.test_exposure_reading_writer import (
    _Appender,
    _command,
    _overlay_fields,
)
from tests.nodes.node_runner_group_exposure_effect.test_token_mint import (
    MINT,
    MINTED,
    _Minter,
)

pytestmark = pytest.mark.unit

#: The permission the test overlay declares (the same synthetic one the examples carry).
PERMISSION = "example_org_permission_read"
#: A different permission, for the failing control.
OTHER_PERMISSION = "example_other_permission_read"


@pytest.fixture
def server() -> Iterator[_Server]:
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.scenario = Scenario()
    httpd.seen_auth = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _handler(
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    minter: _Minter | None,
    scenario: Scenario | None = None,
    *,
    permission: str = PERMISSION,
    mint: dict[str, Any] | None = MINT,
) -> HandlerExposureProbe:
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    # the stored token, as the unit has it: present, so only a refusal can stop the read
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    server.scenario = scenario if scenario is not None else Scenario()
    source = {
        "backend": "github",
        "api_base": f"http://127.0.0.1:{server.server_address[1]}",
        "org": "example-org",
        "group_id": 7,
        "group_labels": ["example-private-group"],
        "token_env": TOKEN_ENV,
        "token_mint": mint,
        "org_read_permission": permission,
        "max_calls": 60,
        "timeout_seconds": 5,
    }
    install_node_overlay(
        monkeypatch, tmp_path, NODE_NAME, {"settings": SETTINGS, "source": source}
    )
    return HandlerExposureProbe(token_mint=minter)


def _request() -> ModelExposureProbeRequest:
    return ModelExposureProbeRequest(correlation_id="permission-1", now=NOW)


# -- golden chain ------------------------------------------------------------------------------


def test_golden_chain_a_clean_read_with_a_minted_token_names_no_permission(
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minter = _Minter()
    result = _handler(server, tmp_path, monkeypatch, minter).handle(_request())
    assert result.reading.verdict is EnumReadingVerdict.WORKING
    assert result.missing_permission is None
    topic, _ = HandlerExposureProbe.event_for(result)
    assert topic == load_topics()["probe_completed"]
    assert set(server.seen_auth) == {f"Bearer {MINTED}"}


# -- named failing control ---------------------------------------------------------------------


def test_failing_control_the_permission_named_is_the_one_the_overlay_declares(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    minter = _Minter()
    result = _handler(
        server,
        tmp_path,
        monkeypatch,
        minter,
        Scenario(group_status=403),
        permission=OTHER_PERMISSION,
    ).handle(_request())
    # a hard-coded permission would name the declared one elsewhere and fail here
    assert result.missing_permission == OTHER_PERMISSION


# -- error chain -------------------------------------------------------------------------------


def test_error_chain_a_refused_group_read_names_the_declared_permission(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    minter = _Minter()
    result = _handler(
        server, tmp_path, monkeypatch, minter, Scenario(group_status=403)
    ).handle(_request())
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reading.read_failure is EnumExposureReadFailure.ORG_READ_DENIED
    assert result.missing_permission == PERMISSION
    topic, _ = HandlerExposureProbe.event_for(result)
    assert topic == load_topics()["probe_failed"]


def test_error_chain_a_missing_stored_token_names_no_permission_and_makes_no_call(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _handler(server, tmp_path, monkeypatch, None, mint=None)
    monkeypatch.delenv(TOKEN_ENV)
    result = handler.handle(_request())
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reading.read_failure is EnumExposureReadFailure.ORG_READ_DENIED
    assert result.missing_permission is None
    assert result.calls == 0
    assert server.seen_auth == []


# -- the written row ---------------------------------------------------------------------------


def _reading_overlay(
    server: _Server, monkeypatch: pytest.MonkeyPatch, permission: str
) -> ModelExposureReadingOverlay:
    """The reading overlay with the GitHub source against the local fixture server."""
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    fields = _overlay_fields()
    fields["source"] = {
        "backend": "github",
        "api_base": f"http://127.0.0.1:{server.server_address[1]}",
        "org": "example-org",
        "group_id": 7,
        "group_labels": ["example-private-group"],
        "token_env": TOKEN_ENV,
        "token_mint": None,
        "org_read_permission": permission,
        "max_calls": 60,
        "timeout_seconds": 5,
    }
    return ModelExposureReadingOverlay.model_validate(fields)


def test_the_written_row_names_the_declared_permission_in_its_reasons(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _reading_overlay(server, monkeypatch, PERMISSION)
    server.scenario = Scenario(group_status=403)
    writer = HandlerExposureReadingWriter(
        overlay=overlay,
        host="example-host",
        probe=HandlerExposureProbe(overlay=overlay),
        appender=_Appender(),
    )
    outcome = writer.handle(_command())
    assert outcome.verdict is EnumReadingVerdict.UNKNOWN
    assert outcome.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert outcome.ledger == "written"
    assert outcome.row is not None
    needs = reason_token("needs-" + PERMISSION)
    assert outcome.reasons == ("org-read-denied", needs)
    assert needs in outcome.row
