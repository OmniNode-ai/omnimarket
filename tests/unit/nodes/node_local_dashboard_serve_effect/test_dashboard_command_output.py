# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26226 AC3, AC4 and the "names ``onex dashboard open``" half of AC1: the foreground ``onex dashboard`` prints no token and no tenant id.

``cli_local.dashboard_command`` (the serve path, handler stubbed) is the command that
used to print the start URL with its session token and the tenant id. The other
dashboard tests drive ``dashboard status`` and ``start``, so nothing else read this
output.

AC1's other clause, that the bare loopback address is never presented as the thing to
open, is covered for this path in test_dashboard_open_instruction.py.

Failure modes covered:
- the serve path prints the request's session token, in a start URL or otherwise
- the serve path prints the tenant id (full, its first block) or the word "tenant"
- the serve path stops telling the user to use ``onex dashboard open``
"""

from __future__ import annotations

import types
from typing import Any
from uuid import UUID

import pytest
from click.testing import CliRunner

import omnimarket.cli.cli_local as cli_local

pytestmark = pytest.mark.unit

_TENANT = "5f0e1d2c-3b4a-4c5d-8e6f-7a8b9c0d1e2f"


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> list[Any]:
    """The requests the stubbed handler received; nothing is served or bound."""
    captured: list[Any] = []

    class _StubHandler:
        def __init__(self, pages: object) -> None:
            self.pages = pages

        async def handle(self, request: object) -> None:
            captured.append(request)

    monkeypatch.setattr(
        cli_local, "resolve_dashboard_bind", lambda _overlay: ("127.0.0.1", 18080)
    )
    monkeypatch.setattr(cli_local, "ensure_bundle", lambda: tmp_path)
    monkeypatch.setattr(
        cli_local,
        "read_local_tenant_identity",
        lambda: types.SimpleNamespace(tenant_uuid=UUID(_TENANT)),
    )
    monkeypatch.setattr(cli_local, "HandlerLocalDashboardServe", _StubHandler)
    return captured


def test_the_serve_output_shows_no_token_and_no_tenant_id(
    served: list[Any],
) -> None:
    result = CliRunner().invoke(cli_local.dashboard_command, [])
    assert result.exit_code == 0, result.output
    assert len(served) == 1, "the serve path did not hand a request to the handler"
    token = served[0].access_token.get_secret_value()
    assert token, "the request carries no token, so this test would prove nothing"
    assert token not in result.output, "the session token is printed"
    assert "token=" not in result.output, "a token query is printed"
    assert _TENANT not in result.output, "the tenant id is printed"
    assert _TENANT.split("-")[0] not in result.output, (
        "part of the tenant id is printed"
    )
    assert "tenant" not in result.output.lower(), "the output names a tenant"


def test_the_serve_output_tells_the_user_to_open_with_the_command(
    served: list[Any],
) -> None:
    result = CliRunner().invoke(cli_local.dashboard_command, [])
    assert result.exit_code == 0, result.output
    assert "onex dashboard open" in result.output
